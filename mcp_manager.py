"""Claude Code에 붙은 MCP 서버(커넥터)를 보고, 추가하고, 지운다. 연결 관리 화면이 쓴다.

두 종류가 있다:
  - claude.ai 커넥터 (이름이 "claude.ai …"): claude.ai 계정에 붙어 있다. 추가·해제는 claude.ai 설정에서,
    인증(OAuth)은 claude.ai 설정 또는 대화형 claude의 /mcp에서 한다. 여기서는 명령으로 지울 수 없다.
  - 직접 추가한 MCP 서버: `claude mcp add/remove` 로 이 PC의 Claude Code 설정(user 범위)에 넣고 뺀다.

주의: 비대화형 실행(claude -p)은 ~/.claude/mcp-needs-auth-cache.json 에 남은 "인증 필요" 기록을 믿고
그 커넥터를 아예 안 붙인다. 인증을 마친 뒤에도 이 기록이 남아 있으면 도구가 안 보이므로, 목록에서
"Connected"로 확인된 서버의 기록은 지운다.
"""

import os
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from minutes import find_claude

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
AUTH_CACHE = Path.home() / ".claude" / "mcp-needs-auth-cache.json"
SETTINGS_URL = "https://claude.ai/settings/connectors"


def _claude(*args, timeout=180, config_dir: str | None = None) -> subprocess.CompletedProcess:
    from minutes import claude_env
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.run([find_claude(), *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=tmp, env=claude_env(config_dir),
                              creationflags=NO_WINDOW)


def list_servers(config_dir: str | None = None) -> list[dict]:
    """[{"name", "endpoint", "status": connected|needs_auth|failed|unknown, "kind": claudeai|local}]"""
    out = _claude("mcp", "list", config_dir=config_dir).stdout
    servers = []
    for line in out.splitlines():
        m = re.match(r"^(.+?): (.+) - (.+)$", line.strip())
        if not m:
            continue
        name, endpoint, state = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        low = state.lower()
        status = ("connected" if "connected" in low and "not" not in low else
                  "needs_auth" if "auth" in low else
                  "failed" if "fail" in low or "error" in low else "unknown")
        servers.append({"name": name, "endpoint": endpoint, "status": status, "state": state,
                        "kind": "claudeai" if name.startswith("claude.ai ") else "local"})
    clear_stale_auth(servers, config_dir)
    return servers


def clear_stale_auth(servers: list[dict], config_dir: str | None = None) -> list[str]:
    """연결됨으로 확인된 서버의 '인증 필요' 캐시 기록을 지운다. 지운 이름 목록을 돌려준다."""
    auth_cache = Path(config_dir) / "mcp-needs-auth-cache.json" if config_dir else AUTH_CACHE
    try:
        cache = json.loads(auth_cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    stale = [s["name"] for s in servers if s["status"] == "connected" and s["name"] in cache]
    if stale:
        backup = auth_cache.with_suffix(".json.bak")
        if not backup.exists():
            shutil.copy2(auth_cache, backup)
        for name in stale:
            cache.pop(name, None)
        auth_cache.write_text(json.dumps(cache), encoding="utf-8")
    return stale


def add_server(name: str, url: str, transport: str = "http", headers: list[str] | None = None) -> str:
    name, url = name.strip(), url.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,40}", name):
        raise ValueError("이름은 영문·숫자·-·_ 로 40자 이내로 지어 주세요. (예: sentry, my-notion)")
    if not re.match(r"^https?://", url):
        raise ValueError("주소는 http:// 또는 https:// 로 시작해야 해요.")
    if transport not in ("http", "sse"):
        raise ValueError("전송 방식은 http 또는 sse 예요.")
    args = ["mcp", "add", "--transport", transport, "--scope", "user", name, url]
    for h in headers or []:
        if h.strip():
            args += ["--header", h.strip()]
    r = _claude(*args)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:400] or "추가하지 못했어요.")
    return (r.stdout or "").strip()


def remove_server(name: str) -> str:
    if name.startswith("claude.ai "):
        raise RuntimeError("claude.ai 커넥터는 claude.ai 설정 → 커넥터에서 연결을 해제해야 해요.")
    r = _claude("mcp", "remove", name)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:400] or "지우지 못했어요.")
    return (r.stdout or "").strip()


def open_auth_console(workdir: Path) -> None:
    """대화형 claude를 새 창으로 띄운다. 사용자가 /mcp → 서버 선택 → Authenticate 로 인증한다.
    이 서버 PC의 화면에 뜨는 거라, 원격 웹 버전(headless 리눅스 서버)에서는 의미가 없다."""
    if os.name != "nt":
        raise RuntimeError("이 기능은 이 서버 PC의 화면이 있어야 해요. 직접 서버에서 "
                           "`claude` 를 실행해 /mcp 로 인증해 주세요.")
    subprocess.Popen(["cmd", "/k", "title MCP 인증 - /mcp 를 입력하세요 && " + f'"{find_claude()}"'],
                     cwd=str(workdir), creationflags=NEW_CONSOLE)
