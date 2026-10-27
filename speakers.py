"""받아쓰기 대본의 화자 이름 바꾸기 ("화자1" → "김철수").

transcribe.py가 만든 .txt("이름: 말" 한 줄씩)와 옆의 .srt를 함께 고친다.
두 화자를 같은 이름으로 바꾸면(화자 구분이 한 사람을 둘로 나눈 경우) .txt에서 이어지는 줄을 한 줄로 합친다.
"""

import re
from pathlib import Path

LINE = re.compile(r"^([^:\n]{1,30}): (.*)$")


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def list_speakers(txt: Path) -> list[dict]:
    """[{"name": "화자1", "count": 줄 수, "samples": [긴 발언 두 개]}] — 처음 말한 순서대로."""
    found: dict[str, list[str]] = {}
    lines = [l for l in _read(txt) if l.strip()]
    for line in lines:
        m = LINE.match(line)
        if m:
            found.setdefault(m[1].strip(), []).append(m[2].strip())
    # 화자 구분 없이 받아쓴 대본은 "이름: " 줄이 거의 없다 → 말 속의 ':'를 화자로 착각하지 않게 빈 목록
    if sum(len(v) for v in found.values()) < 0.8 * len(lines):
        return []
    out = []
    for name, said in found.items():
        # 누군지 알아보기 쉽게 긴 발언부터 두 개 (너무 길면 자른다)
        samples = [s if len(s) <= 90 else s[:90] + "…" for s in sorted(said, key=len, reverse=True)[:2]]
        out.append({"name": name, "count": len(said), "samples": samples})
    return out


def rename_speakers(txt: Path, mapping: dict[str, str]) -> list[Path]:
    """mapping {"화자1": "김철수", ...}로 .txt와 같은 이름의 .srt를 고친다. 고친 파일 목록을 돌려준다."""
    mapping = {k: v.strip().replace(":", " ") for k, v in mapping.items() if v and v.strip() and v.strip() != k}
    if not mapping:
        return []

    lines: list[list[str]] = []  # [이름, 말]
    plain: list[str] = []
    for line in _read(txt):
        m = LINE.match(line)
        if not m:
            plain.append(line)
            continue
        name = mapping.get(m[1].strip(), m[1].strip())
        if lines and lines[-1][0] == name:
            lines[-1][1] += " " + m[2].strip()
        else:
            lines.append([name, m[2].strip()])
    text = "\n".join(f"{n}: {s}" for n, s in lines) if lines else "\n".join(plain)
    txt.write_text(text + "\n", encoding="utf-8")
    changed = [txt]

    srt = txt.with_suffix(".srt")
    if srt.is_file():
        def fix(m: re.Match) -> str:
            return f"{mapping.get(m[1].strip(), m[1].strip())}: {m[2]}"
        new = [LINE.sub(fix, l) if not l.strip().isdigit() and "-->" not in l else l for l in _read(srt)]
        srt.write_text("\n".join(new) + "\n", encoding="utf-8")
        changed.append(srt)
    return changed
