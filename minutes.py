"""화자별 대본(.txt)을 로컬 Claude Code(`claude -p`)로 정리해 회의록(.md / .pdf / .hwpx)을 만든다.

양식 파일(.hwpx / .docx / .pdf / .md / .txt)을 주면 그 양식의 항목·순서·표에 맞춰 만들고,
없으면 기본 양식(개요 → 참석자 → 요약 → 논의 → 결정 → 할 일 → 미결)으로 만든다.

사용 예:
    python minutes.py "회의 영상.txt" --out-dir 바탕화면 --formats md,pdf,hwpx
    python minutes.py "회의 영상.txt" --template "우리팀 회의록 양식.hwpx"
    python minutes.py "회의 영상.txt" --title "영상팀·개발팀 회의" --date "2026-10-16 14:00"

`claude` 명령이 Claude 구독 계정으로 로그인돼 있어야 한다. (GUI의 [Claude 연결하기] 또는 `claude auth login`)
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

import codex_cli

ENGINES = ("claude", "codex")
FORMATS = ("md", "pdf", "hwpx", "docx")
TEMPLATE_TYPES = (".hwpx", ".docx", ".pdf", ".md", ".txt")
# 양식을 따로 고르지 않으면 쓰는 회사 회의록 양식 (tools/make_default_template.py로 만든 빈 양식)
DEFAULT_TEMPLATE = Path(__file__).with_name("templates") / "기본_회의록_양식.docx"
WEEKDAYS = "월화수목금토일"


def today_text() -> str:
    """회사 양식의 날짜 표기: 2026.10.16.(금)"""
    t = date.today()
    return f"{t.year}.{t.month}.{t.day}.({WEEKDAYS[t.weekday()]})"


def norm_label(s) -> str:
    return str(s).replace(" ", "")

RULES = """규칙:
- 대본에 있는 내용만 쓴다. 이름, 날짜, 장소, 기한을 지어내지 않는다. 화자 이름을 모르면 "화자N"을 그대로 쓴다.
- 음성 인식 오류로 보이는 단어는 문맥상 맞는 말로 고쳐 쓴다.
- [참고 용어집]이 주어지면 그 표기와 뜻풀이를 우선해서 쓰고, 잘못 받아적힌 전문용어·줄임말은 용어집 기준으로 고친다.
- 화자 구분은 자동이라 경계가 틀릴 수 있다. 문맥상 명백하면 바로잡아 정리한다.
- 문장은 짧고 명확하게, 한국어 '~다' 체로 쓴다.
- 설명, 마크다운, 코드블록 없이 JSON 하나만 출력한다."""

DEFAULT_PROMPT = """너는 회의록 작성자다. 표준입력으로 받은 회의 대본(자동 음성 인식 + 자동 화자 구분 결과)을 읽고
아래 JSON을 채워라.

{
  "title": "회의 주제를 담은 회의명",
  "overview": "회의 목적과 결론을 담은 1~2문장",
  "participants": [{"speaker": "화자1", "role": "발언으로 추정한 역할", "note": "주요 발언 한 줄"}],
  "summary": ["핵심 요약 3~5개"],
  "discussions": [{"topic": "논의 주제", "points": ["화자N: 발언 요지"]}],
  "decisions": ["합의된 결정 사항"],
  "actions": [{"owner": "담당(화자N 또는 역할)", "task": "할 일", "due": "언급된 기한, 없으면 null"}],
  "open_issues": ["결론이 안 난 것, 다음에 논의할 것"],
  "corrections": [{"heard": "잘못 인식된 것으로 보이는 말", "fixed": "회의록에 고쳐 적은 말"}]
}

없는 항목은 빈 배열로 둔다. 고친 단어는 corrections에 남긴다.
""" + RULES

TEMPLATE_PROMPT = """너는 회의록 작성자다. 표준입력에 [회의록 양식]과 [회의 대본](자동 음성 인식 + 자동 화자 구분 결과)이
차례로 들어 있다. 대본 내용을 양식에 맞춰 정리해 아래 구조의 JSON을 출력해라.

{
  "title": "양식의 제목 (양식에 회의명 칸이 있으면 회의명)",
  "subtitle": "양식에 사업명·프로젝트명 줄이 있으면 그 값 (대본에서 알 수 없으면 빈 문자열)",
  "meta": [["양식 머리 표의 항목명 (띄어쓰기까지 그대로)", "채운 값"]],
  "sections": [
    {"heading": "양식의 항목 제목 그대로",
     "blocks": [
       {"type": "text", "text": "문단"},
       {"type": "bullets", "items": ["글머리 항목"]},
       {"type": "numbered", "items": ["번호 항목"]},
       {"type": "table", "headers": ["양식의 열 이름"], "rows": [["칸 값"]]},
       {"type": "subheading", "text": "작은 제목"}
     ]}
  ],
  "actions": [{"owner": "담당(화자N 또는 역할)", "task": "할 일 한 문장", "due": "언급된 기한, 없으면 null"}]
}

actions는 문서에는 들어가지 않고 Jira 일감 등록에 쓰인다. 회의에서 누군가 하기로 한 일을 빠짐없이 넣는다.

양식 따르기:
- 양식의 항목 제목, 순서, 표의 열 구성을 그대로 따른다. 양식에 없는 항목은 새로 만들지 않는다.
- 양식 맨 위의 기본 정보 표(회의명, 일시, 장소, 참석자, 작성자 등)는 meta에 같은 항목명으로 넣는다.
- 회의명·안건처럼 대본 내용으로 정할 수 있는 칸은 내용에 맞게 채운다. 장소·작성자·날짜처럼 대본으로
  알 수 없는 칸만 "(미기재)"로 둔다. 양식의 안내 문구(예: "~를 적으시오")는 지우고 내용으로 채운다.
- 표로 된 항목은 table 블록으로, 줄글 항목은 text/bullets로 채운다.
- 양식의 "(…)" 표시는 채울 자리라는 뜻이다. 예를 들어 "▣ (대주제)"는 대주제마다 section을 하나씩 만들고,
  "1. (세부 주제)"는 subheading, "(OO팀 의견)"은 발언한 쪽을 밝히는 text("(개발팀 의견)" 등),
  "- (내용)"은 bullets로 채운다. 화자의 소속을 모르면 "(화자1 의견)"처럼 쓴다.
- heading에는 "▣" 같은 앞 기호를 붙이지 않는다. 양식 끝의 비고 같은 고정 항목은 마지막 section으로 둔다.
""" + RULES


IMPORT_PROMPT = """표준입력은 이미 다 작성된 회의록 문서에서 뽑은 텍스트다(표는 마크다운 표로 되어 있을 수 있다).
내용을 바꾸거나 요약하지 말고, 문서에 있는 그대로 아래 JSON 구조로 옮겨라.

{
  "title": "문서 제목 (회의 제목이 따로 있으면 회의 제목)",
  "subtitle": "사업명·프로젝트명 줄이 있으면 그 값, 없으면 빈 문자열",
  "meta": [["머리 표의 항목명 (띄어쓰기까지 그대로)", "값"]],
  "sections": [{"heading": "항목 제목 (▣ 같은 앞 기호는 빼고)",
                "blocks": [{"type": "text", "text": "문단"}, {"type": "bullets", "items": ["항목"]},
                           {"type": "numbered", "items": ["항목"]}, {"type": "subheading", "text": "작은 제목"},
                           {"type": "table", "headers": ["열"], "rows": [["칸"]]}]}],
  "actions": [{"owner": "담당", "task": "할 일 한 문장", "due": "기한, 없으면 null"}]
}

- actions에는 문서에 나온 할 일·조치 계획·향후 일정·요청 사항처럼 누군가 해야 하는 일을 모두 넣는다.
  담당이나 기한이 문서에 없으면 null로 둔다. 지어내지 않는다.
- 설명, 마크다운, 코드블록 없이 JSON 하나만 출력한다."""


def looks_like_transcript(text: str) -> bool:
    """받아쓰기 대본("화자1: …" 줄이 대부분)이면 True. 이미 정리된 회의록과 구분한다."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return False
    spoken = sum(1 for l in lines if re.match(r"^\s*(화자\s*\d+|Speaker\s*\d+|SPEAKER_\d+)\s*[:：]", l))
    return spoken / len(lines) >= 0.5


def import_document(path: Path, on_progress=None, on_status=None, log=print, config_dir: str | None = None,
                    engine: str = "claude") -> dict:
    """③ 탭이 쓰는 회의록 데이터(JSON)를 만든다.
    - 받아쓰기 대본(.txt, "화자N: …")이면 ②처럼 회사 기본 양식 회의록으로 '정리'한다.
    - 이미 작성된 회의록(.pdf/.docx/.hwpx/.md)이면 내용을 바꾸지 않고 그대로 옮긴다."""
    text = read_template(path, limit=40000)
    if path.suffix.lower() == ".txt" and looks_like_transcript(text):
        if on_status:
            on_status("받아쓰기 대본이라 회의록으로 정리하는 중…")
        doc = build_doc(path.read_text(encoding="utf-8-sig"), None, None, None, on_progress, on_status, log,
                        config_dir=config_dir, engine=engine)
        doc.setdefault("actions", [])
        doc["source"] = str(path)
        return doc
    doc = ask_engine(engine, IMPORT_PROMPT, text, min(max(len(text) * 1.2, 1500), 12000), on_progress, on_status,
                     config_dir=config_dir)
    doc.setdefault("actions", [])
    doc["source"] = str(path)
    return doc


# ---------------------------------------------------------------- Claude 호출
def find_claude() -> str:
    local_bin = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    path = shutil.which("claude") or str(local_bin)
    if not Path(path).exists():
        raise RuntimeError("claude 명령을 찾을 수 없어요. Claude Code가 설치돼 있는지 확인하세요.")
    return path


def claude_env(config_dir: str | None = None) -> dict:
    """접속한 사람별로 로그인을 분리하고 싶을 때, 그 사람 전용 설정 폴더를 CLAUDE_CONFIG_DIR로 준다.
    안 주면(None) 이 PC의 기본(공용) 로그인을 그대로 쓴다."""
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def auth_status(config_dir: str | None = None) -> dict:
    """`claude auth status` 결과. 로그인돼 있으면 {"loggedIn": True, ...}."""
    try:
        proc = subprocess.run([find_claude(), "auth", "status", "--json"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30, env=claude_env(config_dir),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(proc.stdout)
    except Exception as e:  # claude가 없거나 응답이 이상해도 GUI는 계속 떠야 한다
        return {"loggedIn": False, "error": str(e)}


def start_login(config_dir: str | None = None) -> subprocess.Popen:
    """Claude 구독 계정 로그인을 시작한다. 표준입출력을 파이프로 받아서(원격 서버라 콘솔 창을 못 띄운다)
    호출한 쪽이 로그인 주소(URL)를 화면에 보여 주고, 사용자가 받은 인증 코드를 표준입력으로 되돌려줄 수 있게 한다."""
    return subprocess.Popen([find_claude(), "auth", "login", "--claudeai"], env=claude_env(config_dir),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def logout(config_dir: str | None = None) -> None:
    """이 로그인(공용 또는 그 사람 전용)을 지운다."""
    subprocess.run([find_claude(), "auth", "logout"], capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=30, env=claude_env(config_dir),
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ask_claude(prompt: str, stdin_text: str, expected_chars: float, on_progress=None, on_status=None,
               timeout: int = 900, model: str | None = None, config_dir: str | None = None) -> dict:
    """claude -p 로 JSON 답을 받는다.

    stream-json으로 받으면서 진행률(0~1)을 알린다. Claude가 생각하는 동안은 글자가 안 나오므로
    시간에 따라 천천히 올리고(0~0.25), 답을 쓰기 시작하면 쓴 글자 수 ÷ 예상 길이로 올린다(0.25~1).
    """
    progress = on_progress or (lambda p: None)
    status = on_status or (lambda s: None)
    state = {"phase": "think", "chars": 0, "last": 0.0, "done": False}
    started = time.time()

    def report(p: float) -> None:
        if p > state["last"]:  # 진행 막대가 뒤로 가지 않게
            state["last"] = p
            progress(p)

    def ticker() -> None:  # 생각하는 동안 시간에 따라 0 → 0.25로 점점 느리게
        while not state["done"]:
            if state["phase"] == "think":
                report(0.25 * (1 - math.exp(-(time.time() - started) / 25)))
            time.sleep(0.5)

    status("Claude가 회의 내용을 파악하는 중…")
    final = None
    # 빈 임시 폴더에서 실행해 프로젝트 파일이나 설정이 섞이지 않게 한다
    with tempfile.TemporaryDirectory() as tmp:
        args = [find_claude(), "-p", prompt, "--output-format", "stream-json", "--verbose",
               "--include-partial-messages"]
        if model:
            args += ["--model", model]
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=tmp, env=claude_env(config_dir),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=ticker, daemon=True).start()
        killer = threading.Timer(timeout, proc.kill)
        killer.start()
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    final = ev
                elif ev.get("type") == "stream_event":
                    delta = ev.get("event", {}).get("delta", {})
                    if delta.get("type") == "text_delta":
                        if state["phase"] == "think":
                            state["phase"] = "write"
                            status("Claude가 회의록을 쓰는 중…")
                        state["chars"] += len(delta.get("text", ""))
                        report(0.25 + 0.75 * min(state["chars"] / expected_chars, 0.97))
            proc.wait()
        finally:
            state["done"] = True
            killer.cancel()
        err = proc.stderr.read()

    if final is None:
        raise RuntimeError(f"claude 응답을 읽지 못했어요.\n{err[-500:]}")
    if final.get("is_error"):
        msg = str(final.get("result", ""))
        if "authenticate" in msg or "login" in msg.lower() or "OAuth" in msg:
            raise RuntimeError("Claude 연결(로그인)이 풀려 있어요. ② 탭의 [Claude 연결하기]를 눌러 다시 "
                               f"연결한 뒤 시도하세요.\n({msg})")
        raise RuntimeError(f"claude 오류: {msg}")

    report_usage(final)
    text = str(final.get("result", ""))
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"claude가 JSON을 돌려주지 않았어요:\n{text[:500]}")
    return json.loads(text[start:end + 1])


def ask_engine(engine: str, prompt: str, stdin_text: str, expected_chars: float, on_progress=None,
              on_status=None, model: str | None = None, config_dir: str | None = None) -> dict:
    """ask_claude()/codex_cli.ask_codex() 중 하나를 engine("claude"/"codex")에 따라 부른다."""
    if engine == "codex":
        return codex_cli.ask_codex(prompt, stdin_text, expected_chars, on_progress, on_status,
                                   model=model, config_dir=config_dir)
    return ask_claude(prompt, stdin_text, expected_chars, on_progress, on_status, model=model,
                      config_dir=config_dir)


def engine_total_usage(engine: str) -> dict:
    return codex_cli.total_usage() if engine == "codex" else total_usage()


USAGE: list[dict] = []  # 이 프로세스에서 claude를 부른 횟수만큼 쌓인다


def metric(**kv) -> None:
    """통계 화면용: 단계별 걸린 시간을 @@METRIC 줄로 알린다 (작업을 띄운 서버가 모은다)."""
    print("@@METRIC " + json.dumps({k: round(v, 1) if isinstance(v, float) else v for k, v in kv.items()},
                                   ensure_ascii=False), flush=True)


def report_usage(final: dict, stage: str = "Claude 정리") -> dict:
    """claude -p 결과의 토큰 사용량을 @@USAGE 줄로 알린다 (작업을 띄운 서버가 모아서 화면에 보여 준다)."""
    if final.get("duration_ms"):
        metric(stage=stage, seconds=final["duration_ms"] / 1000)
    u = final.get("usage") or {}
    usage = {"input": sum(int(u.get(k) or 0) for k in
                          ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
             "output": int(u.get("output_tokens") or 0),
             "cost_usd": float(final.get("total_cost_usd") or 0)}
    USAGE.append(usage)
    print("@@USAGE " + json.dumps(usage), flush=True)
    return usage


def total_usage() -> dict:
    return {"input": sum(u["input"] for u in USAGE), "output": sum(u["output"] for u in USAGE),
            "cost_usd": round(sum(u["cost_usd"] for u in USAGE), 4)}


# ---------------------------------------------------------------- 양식 읽기
def read_template(path: Path, limit: int = 15000) -> str:
    """양식 파일을 Claude가 읽을 수 있는 텍스트(표는 마크다운 표)로 바꾼다."""
    ext = path.suffix.lower()
    if ext == ".hwp":
        raise RuntimeError("예전 한글 형식(.hwp)은 읽을 수 없어요. 한글에서 '다른 이름으로 저장 → HWPX'로 "
                           "저장한 파일을 양식으로 지정해 주세요.")
    if ext == ".hwpx":
        from hwpx import HwpxDocument

        text = HwpxDocument.open(str(path)).text.markdown()
    elif ext == ".docx":
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        d = docx.Document(str(path))
        lines = []
        for child in d.element.body.iterchildren():  # 문단과 표를 문서 순서대로
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = Paragraph(child, d)
                style = (p.style.name or "") if p.style is not None else ""
                if p.text.strip():
                    level = next((int(c) for c in style if c.isdigit()), 0) if style.startswith(("Heading", "제목")) else 0
                    lines.append(("#" * level + " " if level else "") + p.text.strip())
            elif tag == "tbl":
                rows = [[c.text.strip().replace("\n", " ") for c in r.cells] for r in Table(child, d).rows]
                if rows:
                    lines.append("| " + " | ".join(rows[0]) + " |")
                    lines.append("|" + " --- |" * len(rows[0]))
                    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                lines.append("")
        text = "\n".join(lines)
    elif ext == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        text = "\n\n".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
    elif ext in (".md", ".txt"):
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    else:
        raise RuntimeError(f"지원하지 않는 양식 형식이에요: {ext} (가능: {', '.join(TEMPLATE_TYPES)})")
    text = text.strip()
    if not text:
        raise RuntimeError("양식 파일에서 글자를 읽지 못했어요. (스캔한 이미지 PDF는 읽을 수 없어요)")
    return text[:limit]


# ---------------------------------------------------------------- 문서 구조
# 모든 출력(md/pdf/hwpx)은 아래 구조 하나로 그린다.
#   {"title": str, "meta": [[항목, 값]], "sections": [{"heading": str, "blocks": [블록]}]}
#   블록: text / bullets / numbered / table / subheading
def default_doc(data: dict) -> dict:
    """기본 양식: Claude가 채운 DEFAULT_PROMPT JSON을 문서 구조로 바꾼다."""
    people = " / ".join(f"{p.get('speaker', '')}: {p.get('role', '')}" for p in data.get("participants", []))
    sections = []

    def add(heading, *blocks):
        blocks = [b for b in blocks if b]
        if blocks:
            sections.append({"heading": heading, "blocks": blocks})

    if data.get("overview"):
        add("개요", {"type": "text", "text": data["overview"]})
    if data.get("participants"):
        add("참석자", {"type": "table", "headers": ["화자", "추정 역할", "주요 발언"],
                    "rows": [[p.get("speaker", ""), p.get("role", ""), p.get("note", "")]
                             for p in data["participants"]]})
    if data.get("summary"):
        add("핵심 요약", {"type": "bullets", "items": data["summary"]})
    if data.get("discussions"):
        blocks = []
        for i, d in enumerate(data["discussions"], 1):
            blocks += [{"type": "subheading", "text": f"{i}. {d.get('topic', '')}"},
                       {"type": "bullets", "items": d.get("points", [])}]
        add("주요 논의 내용", *blocks)
    if data.get("decisions"):
        add("결정 사항", {"type": "numbered", "items": data["decisions"]})
    if data.get("actions"):
        add("액션 아이템", {"type": "table", "headers": ["담당", "할 일", "기한"],
                        "rows": [[a.get("owner") or "", a.get("task") or "", a.get("due") or "미정"]
                                 for a in data["actions"]]})
    if data.get("open_issues"):
        add("미결 사항", {"type": "bullets", "items": data["open_issues"]})
    if data.get("corrections"):
        add("참고: 음성 인식 보정", {"type": "table", "headers": ["받아쓰기 결과", "고쳐 적은 말"],
                               "rows": [[c.get("heard", ""), c.get("fixed", "")] for c in data["corrections"]]})
    return {
        "title": data.get("title") or "회의록",
        "meta": [["회의명", data.get("title") or "회의록"], ["일시", "(미기재)"],
                 ["참석자", people or "(미기재)"],
                 ["작성", f"{date.today():%Y-%m-%d} 자동 작성 (Whisper 받아쓰기 + Claude 정리)"]],
        "sections": sections,
        "actions": data.get("actions", []),
    }


def apply_overrides(doc: dict, title: str | None, when: str | None) -> dict:
    """사용자가 입력한 회의명·일시를 메타 표의 알맞은 칸에 넣는다 (없으면 맨 위에 추가)."""
    meta = [list(row) for row in doc.get("meta", [])]
    for value, keys in ((when, ("일시", "일자", "날짜", "회의일", "시간")), (title, ("회의명", "제목", "안건"))):
        if not value:
            continue
        row = next((r for r in meta if any(k in norm_label(r[0]) for k in keys)), None)
        if row:
            row[1] = value
        else:
            meta.insert(0, [keys[0], value])
    if title:
        doc["title"] = title
    doc["meta"] = meta
    return doc


def iter_blocks(doc: dict):
    company = doc.get("style") == "company"
    for sec in doc.get("sections", []):
        heading = str(sec.get("heading", "")).lstrip("▣■□◆●○ ").strip()
        yield "heading", f"▣ {heading}" if company else heading
        for b in sec.get("blocks", []):
            yield b.get("type", "text"), b


FULL_ROW = {"회의제목", "회의명", "참석자", "안건"}  # 회사 양식 4열 표에서 한 줄을 다 쓰는 항목


def meta_grid(doc: dict) -> list[list[str]]:
    """회사 양식의 4열 기본 정보 표. 한 줄 전체 항목은 [항목, 값] 2칸, 나머지는 두 쌍씩 4칸."""
    rows, pending = [], []
    for label, value in doc.get("meta", []):
        if norm_label(label) in FULL_ROW:
            rows.append([str(label), str(value)])
        else:
            pending += [str(label), str(value)]
            if len(pending) == 4:
                rows.append(pending)
                pending = []
    if pending:
        rows.append(pending + ["", ""])
    # 참석자는 표 맨 아래로 (원본 양식 순서)
    rows.sort(key=lambda r: norm_label(r[0]) == "참석자")
    return rows


def title_lines(doc: dict) -> tuple[str, str]:
    if doc.get("style") == "company":
        return "회  의  록", str(doc.get("subtitle") or "")
    return str(doc.get("title") or "회의록"), str(doc.get("subtitle") or "")


def norm_table(b: dict) -> tuple[list[str], list[list[str]]]:
    headers = [str(h) for h in b.get("headers") or []]
    rows = [[str(c) for c in r] for r in b.get("rows") or []]
    width = max([len(headers)] + [len(r) for r in rows]) if (headers or rows) else 0
    headers = headers + [""] * (width - len(headers)) if headers else []
    rows = [r + [""] * (width - len(r)) for r in rows]
    return headers, rows


# ---------------------------------------------------------------- Markdown
def to_markdown(doc: dict) -> str:
    md_cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")  # noqa: E731
    title, subtitle = title_lines(doc)
    out = [f"# {title}", ""] + ([f"**{subtitle}**", ""] if subtitle else [])
    if doc.get("style") == "company" and doc.get("meta"):
        out += ["| 항목 | 내용 | 항목 | 내용 |", "| --- | --- | --- | --- |"]
        out += ["| " + " | ".join(md_cell(c) for c in (r if len(r) == 4 else r + ["", ""])) + " |"
                for r in meta_grid(doc)] + [""]
    elif doc.get("meta"):
        out += ["| 항목 | 내용 |", "| --- | --- |"]
        out += [f"| {md_cell(k)} | {md_cell(v)} |" for k, v in doc["meta"]] + [""]
    for kind, b in iter_blocks(doc):
        if kind == "heading":
            out += [f"## {b}", ""]
        elif kind == "subheading":
            out += [f"### {b.get('text', '')}", ""]
        elif kind == "bullets":
            out += [f"- {s}" for s in b.get("items", [])] + [""]
        elif kind == "numbered":
            out += [f"{i}. {s}" for i, s in enumerate(b.get("items", []), 1)] + [""]
        elif kind == "table":
            headers, rows = norm_table(b)
            if headers:
                out += ["| " + " | ".join(map(md_cell, headers)) + " |", "|" + " --- |" * len(headers)]
            out += ["| " + " | ".join(map(md_cell, r)) + " |" for r in rows] + [""]
        else:
            out += [str(b.get("text", "")), ""]
    return "\n".join(out)


# ---------------------------------------------------------------- PDF
def to_pdf(doc: dict, path: Path) -> None:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    # Bundle Korean fonts so PDF export works without OS-specific fonts or network access.
    fonts = Path(__file__).resolve().parent / "fonts"
    regular = fonts / "NanumGothic-Regular.ttf"
    bold = fonts / "NanumGothic-Bold.ttf"
    missing = [p.name for p in (regular, bold) if not p.is_file()]
    if missing:
        raise FileNotFoundError(
            "PDF 생성용 글꼴이 없어요. 프로젝트의 fonts 폴더를 함께 복사해 주세요: "
            + ", ".join(missing)
        )
    pdfmetrics.registerFont(TTFont("NanumGothic", str(regular)))
    pdfmetrics.registerFont(TTFont("NanumGothicBold", str(bold)))
    pdfmetrics.registerFontFamily(
        "NanumGothic", normal="NanumGothic", bold="NanumGothicBold",
        italic="NanumGothic", boldItalic="NanumGothicBold",
    )

    base = dict(fontName="NanumGothic", fontSize=10, leading=15, wordWrap="CJK")
    st_title = ParagraphStyle("t", **{**base, "fontName": "NanumGothicBold", "fontSize": 18, "leading": 24,
                                      "alignment": TA_CENTER, "spaceAfter": 6 * mm})
    st_h2 = ParagraphStyle("h2", **{**base, "fontName": "NanumGothicBold", "fontSize": 12.5, "leading": 18,
                                    "spaceBefore": 5 * mm, "spaceAfter": 2 * mm,
                                    "textColor": colors.HexColor("#1F3A5F")})
    st_h3 = ParagraphStyle("h3", **{**base, "fontName": "NanumGothicBold", "fontSize": 10.5, "spaceBefore": 2 * mm})
    st_body = ParagraphStyle("b", **{**base, "spaceAfter": 1.5 * mm})
    st_item = ParagraphStyle("li", **{**base, "leftIndent": 5 * mm, "bulletIndent": 1 * mm})
    st_cell = ParagraphStyle("c", **{**base, "fontSize": 9.5, "leading": 13})
    st_head = ParagraphStyle("ch", **{**base, "fontName": "NanumGothicBold", "fontSize": 9.5, "leading": 13})

    P = lambda text, style=st_body: Paragraph(escape(str(text)).replace("\n", "<br/>"), style)  # noqa: E731
    width = A4[0] - 40 * mm

    def table(headers, rows, label_col=False):
        data = ([headers] if headers else []) + rows
        if not data:
            return None
        cols = len(data[0])
        if label_col:
            widths = [width * 0.18, width * 0.82]
        else:  # 글자가 많은 열을 넓게
            weight = [max(4, max(len(str(r[c])) for r in data)) for c in range(cols)]
            weight = [min(w, 40) for w in weight]
            widths = [width * w / sum(weight) for w in weight]
        cells = [[P(c, st_head if (headers and r == 0) else st_cell) for c in row] for r, row in enumerate(data)]
        t = Table(cells, colWidths=widths, repeatRows=1 if headers else 0)
        style = [("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9AA5B1")),
                 ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
        if headers:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")))
        if label_col:
            style.append(("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#E8EEF5")))
        t.setStyle(TableStyle(style))
        return t

    company = doc.get("style") == "company"
    title, subtitle = title_lines(doc)
    story = [P(title, st_title)]
    if subtitle:
        story.append(P(subtitle, ParagraphStyle("st", **{**base, "fontName": "NanumGothicBold", "fontSize": 13,
                                                          "leading": 18, "alignment": TA_CENTER,
                                                          "spaceAfter": 4 * mm})))
    if company and doc.get("meta"):
        grid = meta_grid(doc)
        cells = [[P(c, st_head if i % 2 == 0 else st_cell) for i, c in enumerate(r)] + [""] * (4 - len(r))
                 for r in grid]
        t = Table(cells, colWidths=[width * w for w in (0.15, 0.35, 0.15, 0.35)])
        style = [("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                 ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
        for ri, r in enumerate(grid):
            style.append(("BACKGROUND", (0, ri), (0, ri), colors.HexColor("#F2F2F2")))
            if len(r) == 2:
                style.append(("SPAN", (1, ri), (3, ri)))
            else:
                style.append(("BACKGROUND", (2, ri), (2, ri), colors.HexColor("#F2F2F2")))
        t.setStyle(TableStyle(style))
        story.append(t)
    elif doc.get("meta"):
        story.append(table([], [[str(k), str(v)] for k, v in doc["meta"]], label_col=True))
    if company:  # 회사 양식: ▣ 제목은 검정, 글머리는 "-"
        st_h2 = ParagraphStyle("h2c", **{**base, "fontName": "NanumGothicBold", "fontSize": 12, "leading": 17,
                                         "spaceBefore": 5 * mm, "spaceAfter": 1.5 * mm})
    for kind, b in iter_blocks(doc):
        if kind == "heading":
            story.append(P(b, st_h2))
        elif kind == "subheading":
            story.append(P(b.get("text", ""), st_h3))
        elif kind in ("bullets", "numbered"):
            mark = "-" if company else "•"
            story += [Paragraph(escape(str(s)), st_item, bulletText=f"{i}." if kind == "numbered" else mark)
                      for i, s in enumerate(b.get("items", []), 1)]
        elif kind == "table":
            t = table(*norm_table(b))
            if t:
                story.append(t)
        else:
            story.append(P(b.get("text", "")))

    def footer(canvas, d):
        canvas.setFont("NanumGothic", 8)
        canvas.setFillColor(colors.HexColor("#6B7785"))
        canvas.drawCentredString(A4[0] / 2, 10 * mm, f"- {d.page} -")

    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                      topMargin=18 * mm, bottomMargin=18 * mm,
                      title=title).build(story, onFirstPage=footer, onLaterPages=footer)


# ---------------------------------------------------------------- 한글(HWPX)
def to_hwpx(doc: dict, path: Path) -> None:
    from hwpx import HwpxDocument

    h = HwpxDocument.new()
    title_style = h.styles.ensure_run(bold=True, size=18)
    h2_style = h.styles.ensure_run(bold=True, size=13, color="#1F3A5F")
    h3_style = h.styles.ensure_run(bold=True, size=11)

    def para(text="", style=None):
        return h.add_paragraph(str(text), char_pr_id_ref=style)

    def table(headers, rows, label_col=False):
        data = ([headers] if headers else []) + rows
        if not data:
            return
        t = h.add_table(len(data), len(data[0]))
        for r, row in enumerate(data):
            for c, val in enumerate(row):
                t.set_cell_text(r, c, str(val))
        if headers:
            for c in range(len(data[0])):
                t.set_cell_shading(0, c, "#E8EEF5")
        if label_col:
            for r in range(len(data)):
                t.set_cell_shading(r, 0, "#E8EEF5")
        para()

    company = doc.get("style") == "company"
    title, subtitle = title_lines(doc)

    def centered(text, style):
        para(text, style)
        try:
            h.set_paragraph_format(paragraph_index=len(h.paragraphs) - 1, alignment="CENTER")
        except Exception:  # 정렬을 못 해도 내용은 남긴다
            pass

    centered(title, title_style)
    if subtitle:
        centered(subtitle, h.styles.ensure_run(bold=True, size=13))
    para()
    if company and doc.get("meta"):
        grid = meta_grid(doc)
        t = h.add_table(len(grid), 4)
        try:
            t.set_column_widths([15, 35, 15, 35])
        except Exception:
            pass
        for ri, r in enumerate(grid):
            t.set_cell_text(ri, 0, r[0])
            t.set_cell_shading(ri, 0, "#F2F2F2")
            if len(r) == 2:
                t.set_cell_text(ri, 1, r[1])
                t.merge_cells(ri, 1, ri, 3)
            else:
                t.set_cell_text(ri, 1, r[1])
                t.set_cell_text(ri, 2, r[2])
                t.set_cell_text(ri, 3, r[3])
                t.set_cell_shading(ri, 2, "#F2F2F2")
        para()
    elif doc.get("meta"):
        table([], [[str(k), str(v)] for k, v in doc["meta"]], label_col=True)
    if company:
        h2_style = h.styles.ensure_run(bold=True, size=12)
    prev = None
    for kind, b in iter_blocks(doc):
        if kind == "heading":
            if prev not in (None, "table"):
                para()
            para(b, h2_style)
        elif kind == "subheading":
            para(b.get("text", ""), h3_style)
        elif kind in ("bullets", "numbered"):
            mark = "-" if company else "•"
            for i, s in enumerate(b.get("items", []), 1):
                para(f"  {i}. {s}" if kind == "numbered" else f"  {mark} {s}")
        elif kind == "table":
            table(*norm_table(b))
        else:
            para(b.get("text", ""))
        prev = kind
    h.save_to_path(str(path))


# ---------------------------------------------------------------- 워드(DOCX)
def to_docx(doc: dict, path: Path) -> None:
    """회사 양식이면 빈 양식 파일에 직접 채워 원본 서식(글꼴·표 테두리·여백)을 그대로 살린다."""
    import copy

    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    from docx.text.paragraph import Paragraph

    company = doc.get("style") == "company" and Path(doc.get("template", "")).is_file()
    title, subtitle = title_lines(doc)

    def set_text(cell_or_par, text: str) -> None:
        if hasattr(cell_or_par, "paragraphs"):  # 셀: 원본의 여러 줄(문단) 중 첫 줄만 남긴다
            for extra in cell_or_par.paragraphs[1:]:
                extra._p.getparent().remove(extra._p)
        par = cell_or_par.paragraphs[0] if hasattr(cell_or_par, "paragraphs") else cell_or_par
        if par.runs:
            par.runs[0].text = text
            for r in par.runs[1:]:
                r.text = ""
        else:
            par.add_run(text)

    def grid_borders(table) -> None:
        tbl_pr = table._tbl.tblPr
        borders = OxmlElement("w:tblBorders")
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
            el = OxmlElement(f"w:{edge}")
            for k, v in (("val", "single"), ("sz", "4"), ("space", "0"), ("color", "000000")):
                el.set(qn(f"w:{k}"), v)
            borders.append(el)
        tbl_pr.append(borders)

    if company:
        d = docx.Document(doc["template"])
        body = d.element.body
        children = list(body.iterchildren())
        table_el = next(c for c in children if c.tag.endswith("}tbl"))
        set_text(d.paragraphs[1], subtitle)
        # 기본 정보 표: 항목명 칸 오른쪽(병합이면 그 병합 칸)에 값을 넣는다
        values = {norm_label(k): str(v) for k, v in doc.get("meta", [])}

        def lookup(key: str):  # "회의 일시" ↔ "일시"처럼 이름이 조금 달라도 찾는다
            if key in values:
                return values[key]
            return next((v for k, v in values.items() if k and (k in key or key in k)), None)

        for row in d.tables[0].rows:
            cells = row.cells
            labels = [c for i, c in enumerate(cells) if i % 2 == 0]
            for ci, cell in enumerate(cells):
                key = norm_label(cell.text)
                if not key or cell not in labels or ci + 1 >= len(cells) or cells[ci + 1]._tc is cell._tc:
                    continue
                value = lookup(key)
                if value is not None:
                    set_text(cells[ci + 1], value)
        # 본문: 양식의 자리 표시 문단을 서식 견본으로 쓰고 지운 뒤 다시 쓴다
        after = [c for c in children[children.index(table_el) + 1:] if not c.tag.endswith("}sectPr")]
        heading_proto = next(c for c in after if "▣" in "".join(c.itertext()))
        plain_proto = next(c for c in after if "".join(c.itertext()).startswith("- "))
        for c in after:
            body.remove(c)
        sect = body[-1]

        def add(text="", proto=None, bold=None):
            el = copy.deepcopy(proto if proto is not None else plain_proto)
            sect.addprevious(el)
            p = Paragraph(el, d)
            for r in p.runs[1:]:
                r._r.getparent().remove(r._r)
            set_text(p, text)
            if bold is not None and p.runs:
                p.runs[0].bold = bold
            return p

        def add_table(headers, rows):
            data = ([headers] if headers else []) + rows
            t = d.add_table(rows=len(data), cols=len(data[0]))
            grid_borders(t)
            for ri, r in enumerate(data):
                for ci, v in enumerate(r):
                    t.cell(ri, ci).text = v
                    if headers and ri == 0:
                        for run in t.cell(ri, ci).paragraphs[0].runs:
                            run.bold = True
            sect.addprevious(t._tbl)

        add()
        prev = None
        for kind, b in iter_blocks(doc):
            if kind == "heading":
                if prev is not None:
                    add()
                add(b, heading_proto)
            elif kind == "subheading":
                add(b.get("text", ""), bold=True)
            elif kind in ("bullets", "numbered"):
                for i, s in enumerate(b.get("items", []), 1):
                    add(f"{i}. {s}" if kind == "numbered" else f"- {s}", bold=False)
            elif kind == "table":
                headers, rows = norm_table(b)
                if headers or rows:
                    add_table(headers, rows)
            else:
                add(str(b.get("text", "")), bold=False)
            prev = kind
        d.save(str(path))
        return

    # 그 밖의 양식: 새 워드 문서로
    d = docx.Document()
    style = d.styles["Normal"]
    style.font.name = "맑은 고딕"
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "맑은 고딕")
    style.font.size = Pt(10)
    p = d.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(title)
    r.bold, r.font.size = True, Pt(18)
    if subtitle:
        p = d.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(subtitle)
        r.bold, r.font.size = True, Pt(13)
    if doc.get("meta"):
        t = d.add_table(rows=len(doc["meta"]), cols=2)
        grid_borders(t)
        for ri, (k, v) in enumerate(doc["meta"]):
            t.cell(ri, 0).text, t.cell(ri, 1).text = str(k), str(v)
    for kind, b in iter_blocks(doc):
        if kind == "heading":
            r = d.add_paragraph().add_run(b)
            r.bold, r.font.size = True, Pt(13)
        elif kind == "subheading":
            d.add_paragraph().add_run(b.get("text", "")).bold = True
        elif kind in ("bullets", "numbered"):
            for i, s in enumerate(b.get("items", []), 1):
                d.add_paragraph(f"{i}. {s}" if kind == "numbered" else f"• {s}")
        elif kind == "table":
            headers, rows = norm_table(b)
            data = ([headers] if headers else []) + rows
            if data:
                t = d.add_table(rows=len(data), cols=len(data[0]))
                grid_borders(t)
                for ri, row in enumerate(data):
                    for ci, v in enumerate(row):
                        t.cell(ri, ci).text = v
        else:
            d.add_paragraph(str(b.get("text", "")))
    d.save(str(path))


# ---------------------------------------------------------------- 실행
def build_doc(transcript: str, template: Path | None = None, title: str | None = None, when: str | None = None,
              on_progress=None, on_status=None, log=print, glossary: str | None = None,
              model: str | None = None, config_dir: str | None = None, engine: str = "claude") -> dict:
    """화자별 대본 → 정리된 회의록 데이터(문서 구조). 양식을 안 고르면 회사 기본 양식, 일시를 안 적으면 오늘.

    glossary: 회의에서 쓰는 전문용어·줄임말을 적어 두면 정리에 참고해서 바로잡는다.
    model: claude -p / codex exec -m 에 줄 모델. 생략하면 그 엔진의 기본값을 쓴다.
    engine: "claude"(기본) 또는 "codex" — 정리에 쓸 CLI."""
    if template is None and DEFAULT_TEMPLATE.is_file():
        template = DEFAULT_TEMPLATE
    company = template is not None and template.resolve() == DEFAULT_TEMPLATE.resolve()
    expected = min(max(len(transcript) * 1.1, 1500), 9000)  # 회의록 JSON은 대략 대본 길이만큼 나온다
    glossary = (glossary or "").strip()
    glossary_block = f"[참고 용어집]\n{glossary}\n\n" if glossary else ""
    engine_name = "Codex" if engine == "codex" else "Claude"
    if template:
        form = read_template(template)
        log(f"양식 '{template.name}'({len(form):,}자)에 맞춰 대본 {len(transcript):,}자를 {engine_name}로 정리합니다.")
        doc = ask_engine(engine, TEMPLATE_PROMPT, f"{glossary_block}[회의록 양식]\n{form}\n\n[회의 대본]\n{transcript}",
                         expected * 1.2, on_progress, on_status, model=model, config_dir=config_dir)
    else:
        log(f"기본 양식으로 대본 {len(transcript):,}자를 {engine_name}로 정리합니다. (보통 1분 안팎)")
        doc = default_doc(ask_engine(engine, DEFAULT_PROMPT, f"{glossary_block}{transcript}", expected,
                                     on_progress, on_status, model=model, config_dir=config_dir))
    doc = apply_overrides(doc, title, when or today_text())
    if company:
        doc["style"], doc["template"] = "company", str(template)
    return doc


def make_minutes(txt: Path, out_dir: Path, formats=FORMATS, title: str | None = None,
                 when: str | None = None, template: Path | None = None, from_json: Path | None = None,
                 log=print, on_progress=None, on_status=None, glossary: str | None = None,
                 model: str | None = None, config_dir: str | None = None, engine: str = "claude") -> list[Path]:
    """전체 진행률: Claude/Codex 정리 0~90%, 파일 저장 90~100%."""
    progress = on_progress or (lambda p: None)
    if from_json:  # 테스트용: Claude/Codex 없이 저장된 JSON으로 양식만 만든다
        data = json.loads(from_json.read_text(encoding="utf-8"))
        doc = data if "sections" in data else default_doc(data)
        tpl = template or (DEFAULT_TEMPLATE if DEFAULT_TEMPLATE.is_file() else None)
        doc = apply_overrides(doc, title, when or today_text())
        if tpl is not None and tpl.resolve() == DEFAULT_TEMPLATE.resolve():
            doc["style"], doc["template"] = "company", str(tpl)
    else:
        doc = build_doc(txt.read_text(encoding="utf-8-sig"), template, title, when,
                        lambda p: progress(0.9 * p), on_status, log, glossary=glossary, model=model,
                        config_dir=config_dir, engine=engine)
    progress(0.9)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{txt.stem}_회의록"
    # 회의록 내용 데이터: ③ 탭(Jira·Confluence·Slack 등록)이 읽는다
    doc.setdefault("actions", [])
    doc["source"] = str(txt)
    usage = engine_total_usage(engine)
    if usage["input"] or usage["output"]:
        doc["usage"] = usage  # 이 회의록을 만드는 데 쓴 토큰
    data_path = out_dir / f"{stem}.json"
    data_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    written = []
    t_files = time.time()
    for i, fmt in enumerate(formats):
        path = out_dir / f"{stem}.{fmt}"
        (on_status or log)(f"{fmt.upper()} 만드는 중…")
        if fmt == "md":
            path.write_text(to_markdown(doc), encoding="utf-8")
        elif fmt == "pdf":
            to_pdf(doc, path)
        elif fmt == "hwpx":
            to_hwpx(doc, path)
        elif fmt == "docx":
            to_docx(doc, path)
        written.append(path)
        log(f"  저장: {path}")
        progress(0.9 + 0.1 * (i + 1) / len(formats))
    metric(stage="문서 파일 만들기", seconds=time.time() - t_files)
    return written + [data_path]


def rename_in_minutes(data_path: Path, mapping: dict[str, str]) -> list[Path]:
    """회의록 데이터(JSON)의 화자 이름을 바꾸고, 옆에 있는 회의록 파일(md/pdf/hwpx/docx)을 다시 만든다.
    Claude를 다시 부르지 않는다. 바뀐 파일 경로를 돌려준다."""
    mapping = {k: v for k, v in (mapping or {}).items() if k and v and k != v}
    if not mapping or not data_path.is_file():
        return []
    # '화자1'이 '화자10'의 앞부분과 겹치지 않게 긴 이름부터, 뒤에 숫자가 이어지지 않을 때만 바꾼다
    pat = re.compile("|".join(re.escape(k) + r"(?!\d)" for k in sorted(mapping, key=len, reverse=True)))

    def fix(v):
        if isinstance(v, str):
            return pat.sub(lambda m: mapping[m.group(0)], v)
        if isinstance(v, list):
            return [fix(x) for x in v]
        if isinstance(v, dict):
            return {k: fix(x) for k, x in v.items()}
        return v

    doc = fix(json.loads(data_path.read_text(encoding="utf-8")))
    data_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    changed = [data_path]
    stem = data_path.with_suffix("")
    for fmt, make in (("md", lambda p: p.write_text(to_markdown(doc), encoding="utf-8")),
                      ("pdf", lambda p: to_pdf(doc, p)), ("hwpx", lambda p: to_hwpx(doc, p)),
                      ("docx", lambda p: to_docx(doc, p))):
        path = stem.with_suffix("." + fmt)
        if path.is_file():
            make(path)
            changed.append(path)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="화자별 대본 → 회의록 (md / pdf / hwpx)")
    parser.add_argument("txt", type=Path, help="transcribe.py가 만든 화자별 대본 .txt")
    parser.add_argument("--out-dir", type=Path, default=None, help="저장 폴더 (기본: 대본과 같은 폴더)")
    parser.add_argument("--formats", default="md,pdf,hwpx", help="쉼표로 구분: md, pdf, hwpx, docx")
    parser.add_argument("--template", type=Path, default=None,
                        help="회의록 양식 파일 (.hwpx/.docx/.pdf/.md/.txt). 생략하면 기본 양식")
    parser.add_argument("--title", default=None, help="회의명 (생략하면 Claude가 정함)")
    parser.add_argument("--date", default=None, help="회의 일시 (예: 2026-10-16 14:00)")
    parser.add_argument("--engine", choices=ENGINES, default="claude", help="정리에 쓸 CLI: claude(기본) 또는 codex")
    parser.add_argument("--model", default=None,
                        help="claude -p / codex exec -m 에 줄 모델 (예: sonnet, opus, haiku / gpt-5-codex). 생략하면 기본값")
    parser.add_argument("--glossary-file", type=Path, default=None,
                        help="참고 용어집 텍스트 파일 (회의록을 만들 때 그 표기를 참고해서 바로잡는다)")
    parser.add_argument("--from-json", type=Path, default=None, help="Claude/Codex 대신 이 JSON으로 양식만 만든다 (테스트용)")
    parser.add_argument("--config-dir", default=None,
                        help="이 사람 전용 Claude/Codex 로그인 폴더 (--engine에 따라 CLAUDE_CONFIG_DIR 또는 "
                             "CODEX_HOME으로 쓴다. 생략하면 공용 로그인)")
    parser.add_argument("--progress", action="store_true", help="GUI용 @@STATUS/@@PROGRESS/@@OUTPUT 줄 출력")
    args = parser.parse_args()

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        parser.error(f"지원하지 않는 형식: {', '.join(bad)}")

    def status(msg: str) -> None:
        print(f"@@STATUS {msg}" if args.progress else msg, flush=True)

    def progress(p: float) -> None:
        if args.progress:
            print(f"@@PROGRESS {p:.4f}", flush=True)

    try:
        out_dir = args.out_dir or args.txt.parent
        glossary = args.glossary_file.read_text(encoding="utf-8-sig") if args.glossary_file else None
        paths = make_minutes(args.txt, out_dir, formats, args.title, args.date, args.template, args.from_json,
                             log=lambda m: print(m, flush=True), on_progress=progress, on_status=status,
                             glossary=glossary, model=args.model, config_dir=args.config_dir, engine=args.engine)
    except Exception as e:  # GUI와 터미널 모두에서 읽기 쉬운 오류로
        print(f"오류: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    for p in paths:
        if args.progress:
            print(f"@@OUTPUT {p}", flush=True)
    print("완료!", flush=True)


if __name__ == "__main__":
    main()
