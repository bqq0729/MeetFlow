"""sherpa-onnx로 '누가 언제 말했는지'(화자 구분)를 찾는다. Hugging Face 토큰이 필요 없다.

models/segmentation.onnx      : pyannote segmentation 3.0 (말소리 구간 찾기)
models/speaker_embedding.onnx : 3D-Speaker ERes2Net (목소리 특징 추출)
"""

import os
from pathlib import Path

import numpy as np
import sherpa_onnx
from faster_whisper import decode_audio

MODELS = Path(__file__).with_name("models")


def diarize(path: Path, num_speakers: int = 0, threshold: float = 0.9,
            on_progress=None) -> list[tuple[float, float, int]]:
    """(시작초, 끝초, 화자번호) 목록을 돌려준다. num_speakers=0이면 화자 수를 자동으로 추정한다.

    threshold: 자동 추정 시 기준. 클수록 화자를 적게 나눈다. 0.5는 배경음악·웃음이 있는 영상에서
    같은 사람을 여럿으로 쪼개는 경우가 많아 0.9를 기본으로 한다.
    """
    # 기본값(1)은 매우 느리다. WORK_THREADS로 상한을 둘 수 있다 (리눅스 서버는 풀로드에서 전원이 꺼져서 run.sh가 줄여 둔다)
    threads = int(os.environ.get("WORK_THREADS") or 0) or max((os.cpu_count() or 4) - 2, 1)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(MODELS / "segmentation.onnx")),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(MODELS / "speaker_embedding.onnx"), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers > 0 else -1, threshold=threshold),
        min_duration_on=0.2,
        min_duration_off=0.3,
    )
    if not config.validate():
        raise RuntimeError("화자 구분 모델 설정이 잘못됐어요. models 폴더를 확인하세요.")
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    audio = decode_audio(str(path), sampling_rate=sd.sample_rate).astype(np.float32)
    if on_progress:
        def callback(done: int, total: int) -> int:
            on_progress(done / total if total else 0.0)
            return 0  # 0이 아니면 sherpa-onnx가 처리를 중단한다

        result = sd.process(audio, callback=callback)
    else:
        result = sd.process(audio)
    result = result.sort_by_start_time()
    return [(r.start, r.end, r.speaker) for r in result]


def speaker_at(turns: list[tuple[float, float, int]], start: float, end: float) -> int | None:
    """[start, end] 구간과 가장 많이 겹치는 화자. 겹치는 게 없으면 가장 가까운 화자."""
    best, best_overlap = None, 0.0
    for s, e, spk in turns:
        overlap = min(e, end) - max(s, start)
        if overlap > best_overlap:
            best, best_overlap = spk, overlap
    if best is None and turns:
        mid = (start + end) / 2
        best = min(turns, key=lambda t: min(abs(t[0] - mid), abs(t[1] - mid)))[2]
    return best
