"""Codex CLI(설치된 OpenAI Codex CLI, 계정 로그인 방식) 연동.

minutes.py의 ask_claude()/auth_status()/start_login()/logout()와 짝을 이루는 Codex판이다.
API 키가 아니라 `codex login`(디바이스 코드 방식)으로 로그인한 계정을 그대로 쓴다.
claude_env(config_dir)의 CLAUDE_CONFIG_DIR처럼, CODEX_HOME 환경변수로 접속한 사람별 로그인을
따로 쓸 수 있다(안 주면 이 PC의 공용 로그인).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def find_codex() -> str:
    """codex 실행 파일을 찾는다. PATH에 없으면(Windows는 보통 앱 설치 폴더에만 있다)
    OpenAI Codex 앱이 깔아 둔 위치를 뒤져서 찾는다."""
    path = shutil.which("codex") or shutil.which("codex.exe")
    if path:
        return path
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        if base.is_dir():
            found = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            if found:
                return str(found[0])
    raise RuntimeError("codex 명령을 찾을 수 없어요. Codex CLI(OpenAI Codex 앱)가 설치돼 있는지 확인하세요.")


def codex_env(config_dir: str | None = None) -> dict:
    """접속한 사람별로 로그인을 분리하고 싶을 때, 그 사람 전용 설정 폴더를 CODEX_HOME으로 준다.
    안 주면(None) 이 PC의 기본(공용) 로그인을 그대로 쓴다."""
    env = dict(os.environ)
    if config_dir:
        env["CODEX_HOME"] = str(config_dir)
    return env


def auth_status(config_dir: str | None = None) -> dict:
    """`codex login status` 결과. 로그인돼 있으면 {"loggedIn": True, "raw": "..."}."""
    try:
        proc = subprocess.run([find_codex(), "login", "status"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30, env=codex_env(config_dir),
                              creationflags=NO_WINDOW)
        out = _strip_ansi((proc.stdout or proc.stderr or "")).strip()
        logged_in = proc.returncode == 0 and "not logged in" not in out.lower()
        return {"loggedIn": logged_in, "raw": out}
    except Exception as e:  # codex가 없거나 응답이 이상해도 GUI는 계속 떠야 한다
        return {"loggedIn": False, "error": str(e)}


def start_login(config_dir: str | None = None) -> subprocess.Popen:
    """Codex 계정 로그인(디바이스 코드 방식)을 시작한다. claude와 달리 코드를 되돌려줄 필요가 없다 —
    표준출력에 뜨는 주소를 열어 화면에 뜨는 코드를 입력하면, 이 프로세스가 스스로 로그인을 감지하고
    끝난다. 호출한 쪽은 표준출력을 읽어서 주소·코드를 화면에 보여 주기만 하면 된다."""
    return subprocess.Popen([find_codex(), "login", "--device-auth"], env=codex_env(config_dir),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                            creationflags=NO_WINDOW)


def parse_device_login(text: str) -> dict:
    """start_login()의 출력(누적된 전체 텍스트)에서 로그인 주소와 1회용 코드를 뽑아낸다."""
    text = _strip_ansi(text)
    url = re.search(r"https?://\S+", text)
    code = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{3,8})\b", text)
    out: dict = {}
    if url:
        out["login_url"] = url.group(0)
    if code:
        out["login_code"] = code.group(1)
    return out


def logout(config_dir: str | None = None) -> None:
    """이 로그인(공용 또는 그 사람 전용)을 지운다."""
    subprocess.run([find_codex(), "logout"], capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=30, env=codex_env(config_dir), creationflags=NO_WINDOW)


def ask_codex(prompt: str, stdin_text: str, expected_chars: float, on_progress=None, on_status=None,
             timeout: int = 900, model: str | None = None, config_dir: str | None = None) -> dict:
    """`codex exec --json`으로 JSON 답을 받는다. ask_claude()와 같은 모양의 결과를 돌려준다.

    codex exec는 claude -p의 부분 스트리밍(글자 단위 진행률)이 없어서, 생각하는 동안은 시간에
    따라 진행률을 천천히 올리고(0~0.6) 답이 오면 바로 뛴다(0.6~1)."""
    progress = on_progress or (lambda p: None)
    status = on_status or (lambda s: None)
    state = {"last": 0.0, "done": False}
    started = time.time()

    def report(p: float) -> None:
        if p > state["last"]:
            state["last"] = p
            progress(p)

    def ticker() -> None:
        while not state["done"]:
            report(0.6 * (1 - math.exp(-(time.time() - started) / 25)))
            time.sleep(0.5)

    status("Codex가 회의 내용을 파악하는 중…")
    final_text: str | None = None
    usage: dict = {}
    turn_error: str | None = None
    # 빈 임시 폴더에서 실행해 이 프로젝트(git 저장소)의 파일이나 설정이 섞이지 않게 한다
    with tempfile.TemporaryDirectory() as tmp:
        args = [find_codex(), "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "-C", tmp]
        if model:
            args += ["-m", model]
        args.append(prompt)
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", cwd=tmp, env=codex_env(config_dir),
            creationflags=NO_WINDOW,
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
                etype = ev.get("type")
                if etype == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
                    final_text = ev["item"].get("text")
                    status("Codex가 회의록을 쓰는 중…")
                    report(0.9)
                elif etype == "turn.completed":
                    usage = ev.get("usage") or {}
                elif etype in ("turn.failed", "error"):
                    err = ev.get("error") if isinstance(ev.get("error"), dict) else {"message": ev.get("message")}
                    turn_error = str(err.get("message") or ev)
            proc.wait()
        finally:
            state["done"] = True
            killer.cancel()
        err_tail = proc.stderr.read()

    if turn_error:
        if "401" in turn_error or "unauthorized" in turn_error.lower() or "login" in turn_error.lower():
            raise RuntimeError("Codex 연결(로그인)이 풀려 있어요. [Codex 연결하기]를 눌러 다시 연결한 뒤 "
                               f"시도하세요.\n({turn_error})")
        raise RuntimeError(f"codex 오류: {turn_error}")
    if final_text is None:
        raise RuntimeError(f"codex 응답을 읽지 못했어요.\n{err_tail[-500:]}")

    report_usage(usage)
    start, end = final_text.find("{"), final_text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"codex가 JSON을 돌려주지 않았어요:\n{final_text[:500]}")
    return json.loads(final_text[start:end + 1])


USAGE: list[dict] = []  # 이 프로세스에서 codex를 부른 횟수만큼 쌓인다


def metric(**kv) -> None:
    print("@@METRIC " + json.dumps({k: round(v, 1) if isinstance(v, float) else v for k, v in kv.items()},
                                   ensure_ascii=False), flush=True)


def report_usage(usage: dict, stage: str = "Codex 정리") -> dict:
    """codex exec 결과의 토큰 사용량을 @@USAGE 줄로 알린다 (claude 쪽과 같은 모양).
    ChatGPT 구독 계정 사용이라 비용(달러)은 따로 안 준다."""
    u = {"input": int((usage or {}).get("input_tokens") or 0),
         "output": int((usage or {}).get("output_tokens") or 0), "cost_usd": 0.0}
    USAGE.append(u)
    print("@@USAGE " + json.dumps(u), flush=True)
    return u


def total_usage() -> dict:
    return {"input": sum(u["input"] for u in USAGE), "output": sum(u["output"] for u in USAGE),
            "cost_usd": round(sum(u["cost_usd"] for u in USAGE), 4)}
