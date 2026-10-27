"""faster-whisper로 오디오/영상 파일을 텍스트(.txt)와 자막(.srt)으로 변환한다.

사용 예:
    python transcribe.py 회의.mp3
    python transcribe.py a.wav b.mp4 --language ko --model large-v3
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # Windows 캐시 경고 숨김 (동작엔 영향 없음)

def add_cuda_dll_paths() -> None:
    """pip로 설치한 cuBLAS/cuDNN DLL을 ctranslate2가 찾을 수 있게 PATH에 추가한다 (Windows)."""
    if os.name != "nt":
        return
    import site

    for base in site.getsitepackages():
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            d = Path(base) / sub
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ["PATH"]


add_cuda_dll_paths()

from faster_whisper import WhisperModel  # noqa: E402


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


MIN_RUN = 1.0  # 화자가 바뀐 조각으로 인정할 최소 길이(초)


def split_by_speaker(seg, turns, names: dict[int, str]) -> list[tuple[float, float, str, str]]:
    """한 세그먼트를 단어 단위로 화자를 붙여, 화자가 바뀌는 곳에서 나눈다."""
    from diarize import speaker_at

    runs: list[list] = []  # [start, end, speaker_name, text]
    for w in seg.words or []:
        spk = speaker_at(turns, w.start, w.end)
        name = names.setdefault(spk, f"화자{len(names) + 1}") if spk is not None else "화자?"
        if runs and runs[-1][2] == name:
            runs[-1][1] = w.end
            runs[-1][3] += w.word
        else:
            runs.append([w.start, w.end, name, w.word])
    if not runs:  # 단어 정보가 없으면 세그먼트 전체를 한 화자로
        spk = speaker_at(turns, seg.start, seg.end)
        name = names.setdefault(spk, f"화자{len(names) + 1}") if spk is not None else "화자?"
        runs.append([seg.start, seg.end, name, seg.text])

    # 화자 경계의 단어 시간은 부정확해서 "네," 같은 짧은 조각이 엉뚱한 화자에 붙곤 한다.
    # MIN_RUN초보다 짧은 조각은 이웃 조각에 합친다 (맨 앞은 뒤로, 그 외에는 앞으로).
    i = 0
    while len(runs) > 1 and i < len(runs):
        if runs[i][1] - runs[i][0] >= MIN_RUN:
            i += 1
            continue
        if i == 0:
            runs[1][0], runs[1][3] = runs[0][0], runs[0][3] + runs[1][3]
        else:
            runs[i - 1][1], runs[i - 1][3] = runs[i][1], runs[i - 1][3] + runs[i][3]
        del runs[i]
        # 합친 뒤 같은 화자가 연달아 있으면 하나로
        i = 0
        merged = [runs[0]]
        for r in runs[1:]:
            if r[2] == merged[-1][2]:
                merged[-1][1], merged[-1][3] = r[1], merged[-1][3] + r[3]
            else:
                merged.append(r)
        runs = merged
    return [(s, e, n, t.strip()) for s, e, n, t in runs if t.strip()]


def transcribe_file(model: WhisperModel, path: Path, out_dir: Path, language: str | None,
                    log=print, on_progress=None, speakers: int | None = None) -> Path:
    """변환 후 .txt 경로를 돌려준다. log/on_progress(0~1)는 GUI에서 진행 상황을 받을 때 쓴다.

    speakers: None이면 화자 구분 안 함, 0이면 화자 수 자동 추정, 2 이상이면 그 인원으로 구분.
    """
    log(f"\n▶ {path.name}")
    start = time.time()

    # 화자 구분을 하면 진행률 앞 40%는 화자 구분, 뒤 60%는 받아쓰기로 나눠 보여준다
    split = 0.4 if speakers is not None else 0.0
    turns = None
    if speakers is not None:
        from diarize import diarize

        log("  화자 구분 중… (CPU 작업이라 긴 파일은 몇 분 걸려요)")
        turns = diarize(path, num_speakers=speakers,
                        on_progress=(lambda p: on_progress(p * split)) if on_progress else None)
        log(f"  화자 {len({t[2] for t in turns})}명 감지 ({time.time() - start:.1f}초)")
        if on_progress:
            metric(stage="화자 구분", seconds=time.time() - start)

    t_stt = time.time()
    segments, info = model.transcribe(
        str(path),
        language=language,
        beam_size=5,
        vad_filter=True,  # 무음 구간을 건너뛰어 환각(없는 말 생성)을 줄임
        word_timestamps=turns is not None,  # 화자 구분 시 단어별 시간으로 화자를 붙인다
    )
    log(f"  언어: {info.language} (확률 {info.language_probability:.2f}), 길이: {info.duration:.1f}초")

    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / f"{path.stem}.txt"
    srt_path = out_dir / f"{path.stem}.srt"
    names: dict[int, str] = {}  # 처음 말한 순서대로 화자1, 화자2…
    n = 0
    last_name = None  # .txt에서는 같은 화자가 이어 말하면 한 줄로 합친다

    with open(txt_path, "w", encoding="utf-8") as txt, open(srt_path, "w", encoding="utf-8") as srt:
        for seg in segments:  # segments는 제너레이터라 순회할 때 실제 변환이 진행됨
            if turns is not None:
                lines = split_by_speaker(seg, turns, names)
            else:
                lines = [(seg.start, seg.end, None, seg.text.strip())]
            for s, e, name, text in lines:
                n += 1
                shown = f"{name}: {text}" if name else text
                log(f"  [{srt_time(s)} → {srt_time(e)}] {shown}")
                if name is None:
                    txt.write(text + "\n")
                elif name == last_name:
                    txt.write(" " + text)
                else:
                    txt.write(("\n" if last_name else "") + shown)
                last_name = name
                srt.write(f"{n}\n{srt_time(s)} --> {srt_time(e)}\n{shown}\n\n")
            if on_progress and info.duration:
                on_progress(split + (1 - split) * min(seg.end / info.duration, 1.0))
        if last_name:
            txt.write("\n")

    if on_progress:
        on_progress(1.0)
        metric(stage="받아쓰기", seconds=time.time() - t_stt)
        metric(media=path.name, media_seconds=info.duration)
    log(f"  완료 ({time.time() - start:.1f}초) → {txt_path}, {srt_path.name}")
    return txt_path


def metric(**kv) -> None:
    """통계 화면용: 단계별 걸린 시간·음성 길이를 @@METRIC 줄로 알린다 (작업을 띄운 서버가 모은다)."""
    print("@@METRIC " + json.dumps({k: round(v, 1) if isinstance(v, float) else v for k, v in kv.items()},
                                   ensure_ascii=False), flush=True)


def load_model(name: str, device: str = "auto", compute_type: str | None = None) -> WhisperModel:
    """auto면 GPU를 먼저 쓰고, 그래픽카드가 없거나 CUDA DLL이 없으면 CPU로 바꿔 불러온다 (다른 PC용)."""
    if device == "auto":
        import ctranslate2

        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        if device == "cuda":
            try:
                return load_model(name, "cuda", compute_type)
            except Exception as e:  # noqa: BLE001 — 드라이버·DLL 문제면 CPU로라도 돌린다
                print(f"GPU를 쓸 수 없어 CPU로 바꿔요: {e}", flush=True)
                device = "cpu"
    compute_type = compute_type or ("float16" if device == "cuda" else "int8")
    if device == "cpu":
        print("@@STATUS 그래픽카드 없이 CPU로 받아써요 (GPU보다 느려요)", flush=True)
    print(f"모델 로딩: {name} ({device}, {compute_type}) — 처음엔 다운로드로 시간이 걸려요", flush=True)
    return WhisperModel(name, device=device, compute_type=compute_type,
                        cpu_threads=int(os.environ.get("WORK_THREADS") or 0))


def main() -> None:
    parser = argparse.ArgumentParser(description="faster-whisper 음성→텍스트 변환")
    parser.add_argument("files", nargs="+", type=Path, help="변환할 오디오/영상 파일")
    parser.add_argument("--model", default="large-v3-turbo",
                        help="tiny, base, small, medium, large-v3, large-v3-turbo (기본값)")
    parser.add_argument("--language", default=None, help="언어 코드 (예: ko, en). 생략 시 자동 감지")
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"],
                        help="auto(기본): NVIDIA 그래픽카드가 있으면 cuda, 없거나 실패하면 cpu")
    parser.add_argument("--compute-type", default=None,
                        help="float16(GPU 기본), int8_float16, int8(CPU 기본)")
    parser.add_argument("--out-dir", type=Path, default=Path("output"))
    parser.add_argument("--speakers", type=int, default=None, metavar="N",
                        help="화자 구분: 0이면 인원 자동 추정, 2 이상이면 그 인원으로 구분 (생략 시 구분 안 함)")
    parser.add_argument("--progress", action="store_true",
                        help="GUI용: '@@PROGRESS 0.42', '@@STATUS …' 줄을 출력한다")
    args = parser.parse_args()

    t0 = time.time()
    model = load_model(args.model, args.device, args.compute_type)
    if args.progress:
        metric(stage="모델 불러오기", seconds=time.time() - t0)

    for idx, f in enumerate(args.files):
        if not f.exists():
            print(f"파일 없음: {f}", file=sys.stderr)
            continue
        on_progress = None
        if args.progress:
            print(f"@@STATUS 변환 중 ({idx + 1}/{len(args.files)}): {f.name}", flush=True)
            on_progress = lambda p, i=idx: print(f"@@PROGRESS {(i + p) / len(args.files):.4f}",  # noqa: E731
                                                 flush=True)
        txt = transcribe_file(model, f, args.out_dir, args.language, speakers=args.speakers,
                              on_progress=on_progress)
        if args.progress:
            print(f"@@OUTPUT {txt.resolve()}", flush=True)


if __name__ == "__main__":
    main()
