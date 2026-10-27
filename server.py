"""업무 자동화 — 로컬 전용 웹 버전 (독립형, 실제 연결판).

받아쓰기(faster-whisper)·화자 구분(sherpa-onnx)·Claude 정리·Confluence/Jira/GitLab/Slack
연동 엔진 코드(app.py 및 transcribe.py 등)를 이 폴더 안에 그대로 갖고 있어서, 다른 whisper-stt
설치 없이 이 폴더 하나만으로 동작한다 (whisper-stt 데스크톱 프로젝트와는 독립된 복사본).
app.py의 Api 클래스를 그대로 상속해서 쓰고, 바뀌는 건 "화면에 알리는 방법"뿐: pywebview 창의
evaluate_js 대신 HTTP 이벤트 큐를, 파일 선택 대화상자 대신 tkinter를 쓴다.

실행:
    python -m venv .venv
    .venv\\Scripts\\pip install -r requirements.txt   (GPU면 requirements-gpu.txt도)
    .venv\\Scripts\\python server.py

또는 run.bat 더블클릭. http://localhost:8787 이 자동으로 열린다.
"""
import itertools
import json
import mimetypes
import os
import re
import sys
import threading
import tkinter as tk
import uuid
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog
from urllib.parse import unquote, urlsplit, parse_qs, quote

sys.stdout.reconfigure(encoding="utf-8")

WEB = Path(__file__).resolve().parent
UI = WEB / "ui"
UPLOADS = WEB / "uploads"
# 접속한 브라우저(쿠키 wsid)마다 Claude 로그인·진행 알림을 따로 쓰게, 이 메서드들은 호출 끝에 그 ID를 끼워서 부른다.
USER_SCOPED_METHODS = {"init", "check_claude", "login", "logout", "check_codex", "login_codex", "logout_codex",
                       "check_connectors", "mcp_list",
                       "start_transcribe", "start_minutes", "start_import", "start_publish", "start_list_targets",
                       "start_jira_fetch", "start_gitlab_push", "submit_login_code", "get_stats"}
HOST = os.environ.get("WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_PORT", "8787"))

sys.path.insert(0, str(WEB))
import app as orig_app  # noqa: E402  (이 폴더 안의 엔진 코드)

# ---------- 파이썬 → 화면 이벤트 큐 (App.onEvent 흉내) ----------
_events: list[dict] = []
_event_id = itertools.count(1)
_elock = threading.Lock()


def push_event(event: str, data: dict) -> None:
    uid = data.pop("_uid", None) or None  # 있으면 그 사람 화면에만 보낸다
    with _elock:
        _events.append({"id": next(_event_id), "event": event, "data": data, "uid": uid})


def events_since(since: int, uid: str):
    with _elock:
        new = [e for e in _events if e["id"] > since]
    nxt = new[-1]["id"] if new else since
    mine = [{k: e[k] for k in ("id", "event", "data")} for e in new if e["uid"] in (None, uid)]
    return mine, nxt


_dlock = threading.Lock()


class WebApi(orig_app.Api):
    """pywebview 창 대신 HTTP로 이어주는 얇은 서브클래스. 실제 로직은 원본 Api를 그대로 쓴다."""

    def emit(self, event: str, **data) -> None:
        push_event(event, data)

    def _pick(self, what: str, start: str = ""):
        with _dlock:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                if what == "folder":
                    return filedialog.askdirectory(initialdir=start or None, parent=root) or ""
                ft = {
                    "media": [("녹음·영상·대본", "*.mp3 *.wav *.m4a *.flac *.ogg *.aac *.wma "
                                              "*.mp4 *.mkv *.mov *.avi *.webm *.txt"), ("모든 파일", "*.*")],
                    "txt": [("대본", "*.txt"), ("모든 파일", "*.*")],
                    "template": [("회의록 양식", "*.hwpx *.docx *.pdf *.md *.txt"), ("모든 파일", "*.*")],
                    "doc": [("회의록", "*.json *.pdf *.docx *.hwpx *.md *.txt"), ("모든 파일", "*.*")],
                    "glossary": [("용어집 텍스트", "*.txt *.md *.csv"), ("모든 파일", "*.*")],
                }.get(what, [("모든 파일", "*.*")])
                if what == "media":
                    return list(filedialog.askopenfilenames(initialdir=start or None, filetypes=ft, parent=root))
                return filedialog.askopenfilename(initialdir=start or None, filetypes=ft, parent=root) or ""
            finally:
                root.destroy()


API_OBJ = WebApi()

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass  # 콘솔을 조용히

    def _session_id(self) -> str:
        """브라우저마다 고유 ID(쿠키 wsid). 없으면 새로 만들어 응답에 심는다.
        같은 공유기(같은 공인 IP) 뒤에 있는 사람들도 각자 따로 구분된다."""
        if getattr(self, "_sid", None):
            return self._sid
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        sid = jar["wsid"].value if "wsid" in jar else ""
        if not re.fullmatch(r"[0-9a-f]{32}", sid):
            sid = uuid.uuid4().hex
            self._new_sid = sid
        self._sid = sid
        return sid

    def end_headers(self):
        if getattr(self, "_new_sid", None):
            self.send_header("Set-Cookie", f"wsid={self._new_sid}; Path=/; Max-Age=315360000; HttpOnly; SameSite=Lax")
            self._new_sid = None
        super().end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        sid = self._session_id()
        if self.path.startswith("/events"):
            since = int(self.path.split("since=", 1)[1].split("&")[0]) if "since=" in self.path else 0
            new, nxt = events_since(since, sid)
            self._json({"events": new, "next": nxt})
            return
        if self.path.startswith("/download"):
            self._download()
            return
        path = self.path.split("?", 1)[0]
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        f = UI / rel
        if not f.is_file() or UI.resolve() not in f.resolve().parents:
            self.send_error(404)
            return
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path.startswith("/upload"):
            self._upload()
            return
        if not self.path.startswith("/api/"):
            self.send_error(404)
            return
        method = self.path[len("/api/"):]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"[]"
        try:
            args = json.loads(body or b"[]")
        except ValueError:
            args = []
        fn = getattr(API_OBJ, method, None) if not method.startswith("_") else None
        if not callable(fn):
            self._json({"error": f"{method}은 지원하지 않는 기능이에요."})
            return
        if method in USER_SCOPED_METHODS:
            args = [*args, self._session_id()]
        try:
            result = fn(*args)
        except Exception as e:  # noqa: BLE001
            result = {"error": f"{type(e).__name__}: {e}"}
        self._json(result if result is not None else {})

    def _upload(self) -> None:
        """드래그앤드롭으로 들어온 파일을 로컬에 저장하고 경로를 돌려준다 (브라우저는 절대경로를 안 줌)."""
        qs = parse_qs(urlsplit(self.path).query)
        name = unquote((qs.get("name") or ["파일"])[0])
        name = re.sub(r'[\\/:*?"<>|]', "_", Path(name).name) or "파일"
        length = int(self.headers.get("Content-Length") or 0)
        UPLOADS.mkdir(parents=True, exist_ok=True)
        dest = UPLOADS / name
        stem, suffix, n = dest.stem, dest.suffix, 1
        while dest.exists():
            dest = UPLOADS / f"{stem}_{n}{suffix}"
            n += 1
        remaining = length
        with dest.open("wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        self._json({"path": str(dest)})

    def _download(self) -> None:
        """만들어진 결과 파일을 접속한 컴퓨터(클라이언트)로 내려받게 해 준다."""
        qs = parse_qs(urlsplit(self.path).query)
        raw = (qs.get("path") or [""])[0]
        if not raw:
            self.send_error(400)
            return
        try:
            f = Path(raw).resolve()
        except (OSError, ValueError):
            self.send_error(400)
            return
        # 결과 폴더(output/) 안의 파일만 내려받을 수 있다. 홈 전체를 열어 두면 다른 사람의 Claude 토큰이나 SSH 키까지 받아갈 수 있다.
        if not f.is_file() or orig_app.OUTPUT_DIR.resolve() not in f.parents:
            self.send_error(404)
            return
        data = f.read_bytes()
        self.send_response(200)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(f.name)}")
        self.end_headers()
        self.wfile.write(data)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"업무 자동화 (웹, 실제 연결) - {url}")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
