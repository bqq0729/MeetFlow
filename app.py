"""업무 자동화 웹 서버의 엔진 코드. 화면은 ui/index.html(브라우저), 실제 작업은 기존 스크립트를
별도 프로세스로 실행한다.

  ① 음성 → 텍스트      transcribe.py
  ② 회의록 만들기       minutes.py
  ③ Jira·Confluence·Slack  integrations.py

화면(JS)은 web_shim.js를 거쳐 아래 Api를 부르고(server.py의 WebApi), 파이썬은 App.onEvent(...)로
진행 상황을 알린다. 작업을 별도 프로세스로 돌리는 이유: 화자 구분(sherpa-onnx)이 계산 중 파이썬을
붙잡아 서버가 멈추기 때문.

(데스크톱 pywebview 창으로 띄우는 실행 모드·화면 캡처 점검 도구는 이 웹 버전엔 없다.)
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

import webview

import codex_cli
import gitlab_sync
import integrations
import mcp_manager
import minutes
import speakers

HERE = Path(__file__).resolve().parent
SETTINGS_PATH = HERE / "gui_settings.json"
TEMPLATES_DIR = HERE / "templates"
GLOSSARY_DIR = HERE / "glossary"
OUTPUT_DIR = HERE / "output"
PICK_DIRS = {"template": TEMPLATES_DIR, "glossary": GLOSSARY_DIR, "doc": OUTPUT_DIR, "output": OUTPUT_DIR}
PICK_EXTS = {"template": {".hwpx", ".docx", ".pdf", ".md", ".txt"},
             "glossary": {".txt", ".md", ".csv"},
             "doc": {".json", ".pdf", ".docx", ".hwpx", ".md", ".txt"}}
PYTHON = (Path(sys.executable).with_name("python.exe") if os.name == "nt"
          else Path(sys.executable))  # Windows: pythonw로 떠도 자식은 출력을 읽을 수 있게 python.exe로
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LANGUAGES = {"자동 감지": None, "한국어": "ko", "영어": "en", "일본어": "ja", "중국어": "zh"}
MEDIA_EXT = "*.mp3;*.wav;*.m4a;*.flac;*.ogg;*.aac;*.wma;*.mp4;*.mkv;*.mov;*.avi;*.webm"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(update: dict) -> None:
    data = {**load_settings(), **update}
    try:
        SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def clean(p) -> Path:
    return Path(str(p or "").strip().strip('"'))


STATS_PATH = HERE / "stats.jsonl"
_stats_lock = threading.Lock()


def record_stat(rec: dict) -> None:
    """작업 한 건의 걸린 시간·토큰·음성 길이를 한 줄씩 쌓는다 (통계 화면이 읽는다)."""
    try:
        with _stats_lock, STATS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_stats(uid: str, limit: int = 300) -> list[dict]:
    try:
        lines = STATS_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("uid") == uid:
            rec.pop("uid", None)
            out.append(rec)
            if len(out) >= limit:
                break
    return out


def _open_native(path) -> None:
    """이 서버 PC 화면에서 파일·URL을 열어 준다 (데스크톱 모드용).
    원격 브라우저로 접속한 웹 버전에서는 서버 PC에 화면이 없을 수 있어 조용히 건너뛴다."""
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 (Windows 전용 API)


class Job:
    """자식 프로세스 하나. @@PROGRESS/@@STATUS/@@OUTPUT/@@RESULT 줄을 읽어 화면에 알린다."""

    def __init__(self, api: "Api", name: str):
        self.api, self.name = api, name
        self.proc: subprocess.Popen | None = None
        self.stop_requested = False
        self.running = False
        self.uid = ""  # 이 작업을 시작한 사람 (진행 알림은 그 사람 화면에만 간다)

    def start(self, cmd: list[str], start_text: str, on_done=None, uid: str = "", meta: dict | None = None) -> bool:
        """이미 돌고 있으면(다른 사람이 같은 작업 중) 시작하지 않고 False를 돌려준다.
        meta: 통계에 같이 남길 정보 (task: 무슨 작업인지, file: 어떤 파일인지)."""
        if self.running:
            return False
        self.stop_requested, self.running, self.uid = False, True, uid
        self.api.emit("job", _uid=self.uid, job=self.name, kind="start", text=start_text)
        threading.Thread(target=self._work, args=(cmd, on_done, meta or {}), daemon=True).start()
        return True

    def _work(self, cmd: list[str], on_done, meta: dict) -> None:
        tail: deque[str] = deque(maxlen=15)
        outputs: list[str] = []
        usage = {"input": 0, "output": 0, "cost_usd": 0.0}
        calls = 0
        metrics: list[dict] = []
        result = "error"
        started = time.time()
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONWARNINGS": "ignore"},
                creationflags=NO_WINDOW)
            for line in self.proc.stdout:
                line = line.rstrip()
                if line.startswith("@@PROGRESS "):
                    self.api.emit("job", _uid=self.uid, job=self.name, kind="progress", value=float(line.split()[1]))
                elif line.startswith("@@STATUS "):
                    self.api.emit("job", _uid=self.uid, job=self.name, kind="status", text=line[9:])
                elif line.startswith(("@@OUTPUT ", "@@RESULT ")):
                    outputs.append(line.split(" ", 1)[1])
                elif line.startswith("@@USAGE "):
                    try:
                        u = json.loads(line.split(" ", 1)[1])
                    except ValueError:
                        continue
                    calls += 1
                    for k in usage:
                        usage[k] += u.get(k) or 0
                elif line.startswith("@@METRIC "):
                    try:
                        metrics.append(json.loads(line.split(" ", 1)[1]))
                    except ValueError:
                        pass
                elif line:
                    tail.append(line)
                    self.api.emit("job", _uid=self.uid, job=self.name, kind="log", text=line)
            code = self.proc.wait()
            usage["cost_usd"] = round(usage["cost_usd"], 4)
            if self.stop_requested:
                result = "stopped"
                self.api.emit("job", _uid=self.uid, job=self.name, kind="stopped")
            elif code == 0:
                result = "done"
                extra = on_done(outputs) if on_done else {}
                self.api.emit("job", _uid=self.uid, job=self.name, kind="done", outputs=outputs,
                              elapsed=round(time.time() - started), usage=usage if calls else None, **(extra or {}))
            else:
                self.api.emit("job", _uid=self.uid, job=self.name, kind="error",
                              text=f"작업이 실패했어요 (종료 코드 {code}).\n\n" + "\n".join(tail))
        except Exception as e:
            self.api.emit("job", _uid=self.uid, job=self.name, kind="error", text=f"{type(e).__name__}: {e}")
        finally:
            self.proc, self.running = None, False
            record_stat({"ts": round(started), "uid": self.uid, "job": self.name, "result": result,
                         "elapsed": round(time.time() - started, 1), "usage": usage if calls else None,
                         "stages": [m for m in metrics if "stage" in m],
                         "media": [m for m in metrics if "media" in m],
                         "outputs": [Path(o).name for o in outputs if len(o) < 500], **meta})

    def kill(self) -> None:
        # venv의 python.exe는 실제 파이썬을 자식으로 띄우는 런처라서 프로세스 트리째 끝낸다
        if self.proc and self.proc.poll() is None:
            if os.name != "nt":
                self.proc.kill()
                return
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                           creationflags=NO_WINDOW)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.stop_requested = True
            self.kill()


JOB_LABEL = {"t": "받아쓰기", "m": "회의록 만들기", "p": "공유", "g": "Jira 연동"}


class Api:
    def __init__(self):
        self._window = None
        self._jobs = {k: Job(self, k) for k in ("t", "m", "p", "g")}
        self._claude_ok = False
        self._codex_ok = False
        self._connectors: dict = {}
        self._logins: dict[str, subprocess.Popen] = {}  # uid → 로그인 중인 'claude auth login' 프로세스
        self._codex_logins: dict[str, subprocess.Popen] = {}  # uid → 로그인 중인 'codex login --device-auth' 프로세스

    # ---------- 파이썬 → 화면 ----------
    def emit(self, event: str, **data) -> None:
        data.pop("_uid", None)  # 데스크톱 모드는 사용자가 한 명뿐
        if self._window:
            self._window.evaluate_js(f"window.App && App.onEvent({json.dumps(event)}, "
                                     f"{json.dumps(data, ensure_ascii=False)})")

    # ---------- 접속한 사람별 Claude 로그인 ----------
    def _config_dir_for(self, uid: str) -> str | None:
        """접속한 브라우저(uid, 쿠키로 받은 고유 ID)마다 Claude 로그인을 따로 쓰게, 그 사람 전용 설정 폴더를 돌려준다.
        uid가 없으면(데스크톱 모드 등) None → 이 PC의 공용 로그인을 그대로 쓴다."""
        uid = (uid or "").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", uid):
            return None
        d = Path.home() / ".claude-profiles" / uid
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    # ---------- 접속한 사람별 Codex 로그인 ----------
    def _codex_config_dir_for(self, uid: str) -> str | None:
        """_config_dir_for()와 같은 구실이지만 Codex CLI용(CODEX_HOME) 폴더."""
        uid = (uid or "").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", uid):
            return None
        d = Path.home() / ".codex-profiles" / uid
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    # ---------- 공통 ----------
    def init(self, uid: str = "") -> dict:
        s = load_settings()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self.check_claude, args=(uid,), daemon=True).start()
        threading.Thread(target=self.check_codex, args=(uid,), daemon=True).start()
        threading.Thread(target=self.check_connectors, args=(uid,), daemon=True).start()
        return {"settings": s, "languages": list(LANGUAGES), "home": str(Path.home()),
                "output_dir": str(OUTPUT_DIR), "engines": list(minutes.ENGINES),
                "default_template": minutes.DEFAULT_TEMPLATE.name if minutes.DEFAULT_TEMPLATE.is_file() else ""}

    def save_settings(self, update: dict) -> None:
        save_settings(update)

    def get_stats(self, uid: str = "") -> dict:
        """통계 화면: 이 사람이 돌린 작업들의 걸린 시간·토큰·음성 길이 (최근 것부터)."""
        return {"records": read_stats(uid)}

    def _busy_error(self, job: str) -> dict:
        return {"error": f"지금 다른 사람이 '{JOB_LABEL.get(job, job)}' 작업을 하고 있어요. 끝난 뒤에 다시 시도해주세요."}

    def list_files(self, kind: str) -> dict:
        """templates/·glossary/·output/ 안의 파일 목록을 돌려준다 (회의록 양식·용어집·회의록 파일 고르기용)."""
        d = PICK_DIRS.get(kind)
        if not d:
            return {"files": []}
        d.mkdir(parents=True, exist_ok=True)
        exts = PICK_EXTS.get(kind, set())
        files = sorted((p for p in d.iterdir() if p.is_file() and p.suffix.lower() in exts), key=lambda p: p.name)
        return {"dir": str(d), "files": [{"name": p.name, "path": str(p)} for p in files]}

    def _dialog(self, kind: str, title: str = "", types=(), multiple=False, start=""):
        ft = webview.FileDialog if hasattr(webview, "FileDialog") else None
        mode = {"open": ft.OPEN if ft else webview.OPEN_DIALOG,
                "folder": ft.FOLDER if ft else webview.FOLDER_DIALOG}[kind]
        res = self._window.create_file_dialog(mode, allow_multiple=multiple, file_types=types,
                                              directory=start if start and Path(start).is_dir() else "")
        if not res:
            return [] if multiple else ""
        return list(res) if multiple else res[0]

    def pick(self, what: str, start: str = ""):
        try:
            return self._pick(what, start)
        except Exception as e:  # 대화상자를 못 열면 화면에 이유를 보여 준다
            return {"error": f"파일 선택 창을 열지 못했어요: {e}"}

    # 파일 형식 설명에는 글자·숫자·공백만 쓸 수 있다 (pywebview 규칙: '/' 같은 기호가 있으면 창이 안 뜬다)
    def _pick(self, what: str, start: str = ""):
        if what == "media":
            return self._dialog("open", types=(f"녹음 영상 대본 ({MEDIA_EXT};*.txt)", f"오디오 영상 ({MEDIA_EXT})",
                                               "대본 (*.txt)", "모든 파일 (*.*)"), multiple=True)
        if what == "folder":
            return self._dialog("folder", start=start)
        if what == "txt":
            return self._dialog("open", types=("대본 (*.txt)", "모든 파일 (*.*)"))
        if what == "template":
            return self._dialog("open", types=("회의록 양식 (*.hwpx;*.docx;*.pdf;*.md;*.txt)", "모든 파일 (*.*)"))
        if what == "doc":
            return self._dialog("open", types=("회의록 (*.json;*.pdf;*.docx;*.hwpx;*.md;*.txt)", "모든 파일 (*.*)"))
        if what == "glossary":
            return self._dialog("open", types=("용어집 텍스트 (*.txt;*.md;*.csv)", "모든 파일 (*.*)"))
        return ""

    def open_path(self, path: str) -> None:
        p = clean(path)
        if p.exists():
            _open_native(p)

    def open_url(self, url: str) -> None:
        _open_native(url)

    def stop(self, job: str) -> None:
        self._jobs[job].stop()

    # ---------- Claude 연결 ----------
    def check_claude(self, uid: str = "") -> dict:
        st = minutes.auth_status(self._config_dir_for(uid))
        self._claude_ok = bool(st.get("loggedIn"))
        info = {"ok": self._claude_ok, "email": st.get("email") or "", "plan": st.get("subscriptionType") or "",
                "missing": bool(st.get("error"))}
        self.emit("claude", _uid=uid, **info)
        return info

    def login(self, uid: str = "") -> None:
        config_dir = self._config_dir_for(uid)
        proc = minutes.start_login(config_dir)
        self._logins[uid] = proc
        self.emit("claude", _uid=uid, ok=False, pending=True)

        def read_output():
            # claude auth login이 찍는 로그인 주소(URL)를 찾아서 화면에 보여 준다 (원격 서버라 직접 브라우저를 못 연다)
            if not proc.stdout:
                return
            for line in proc.stdout:
                m = re.search(r"https?://\S+", line)
                if m:
                    self.emit("claude", _uid=uid, ok=False, pending=True, login_url=m.group(0))
        threading.Thread(target=read_output, daemon=True).start()

        deadline = time.time() + 600

        def poll():
            # 기다리는 동안은 화면에 알리지 않는다 (알리면 로그인 주소·코드 입력칸이 '연결 안 됨'으로 덮인다)
            while time.time() < deadline:
                time.sleep(3)
                if minutes.auth_status(config_dir).get("loggedIn") or proc.poll() is not None:
                    break
            self._logins.pop(uid, None)
            self.check_claude(uid)
        threading.Thread(target=poll, daemon=True).start()

    def submit_login_code(self, code: str, uid: str = "") -> dict:
        """로그인 주소에서 받은 인증 코드를, 기다리고 있는 'claude auth login' 프로세스에 되돌려준다."""
        proc = self._logins.get(uid)
        if not proc or proc.poll() is not None or not proc.stdin:
            return {"error": "로그인이 이미 끝났거나 시간이 지났어요. [연결]을 다시 눌러 주세요."}
        try:
            proc.stdin.write((code or "").strip() + "\n")
            proc.stdin.flush()
        except OSError as e:
            return {"error": f"코드를 전달하지 못했어요: {e}"}
        return {}

    def logout(self, uid: str = "") -> dict:
        try:
            minutes.logout(self._config_dir_for(uid))
        except Exception as e:  # noqa: BLE001
            return {"error": f"로그아웃하지 못했어요: {e}"}
        return self.check_claude(uid)

    # ---------- Codex 연결 ----------
    def check_codex(self, uid: str = "") -> dict:
        st = codex_cli.auth_status(self._codex_config_dir_for(uid))
        self._codex_ok = bool(st.get("loggedIn"))
        info = {"ok": self._codex_ok, "missing": bool(st.get("error"))}
        self.emit("codex", _uid=uid, **info)
        return info

    def login_codex(self, uid: str = "") -> None:
        config_dir = self._codex_config_dir_for(uid)
        proc = codex_cli.start_login(config_dir)
        self._codex_logins[uid] = proc
        self.emit("codex", _uid=uid, ok=False, pending=True)

        def read_output():
            # codex login --device-auth가 찍는 주소(고정 URL)와 1회용 코드를 찾아서 화면에 보여 준다.
            # claude와 달리 코드를 입력칸으로 되돌려줄 필요가 없다 — 그 주소에서 직접 코드를 입력하면
            # 이 프로세스가 스스로 로그인을 감지한다.
            if not proc.stdout:
                return
            seen = ""
            for line in proc.stdout:
                seen += line
                info = codex_cli.parse_device_login(seen)
                if info.get("login_url") and info.get("login_code"):
                    self.emit("codex", _uid=uid, ok=False, pending=True, **info)
                    break
        threading.Thread(target=read_output, daemon=True).start()

        deadline = time.time() + 900  # 코드 유효시간(15분) + 여유

        def poll():
            while time.time() < deadline:
                time.sleep(3)
                if codex_cli.auth_status(config_dir).get("loggedIn") or proc.poll() is not None:
                    break
            self._codex_logins.pop(uid, None)
            self.check_codex(uid)
        threading.Thread(target=poll, daemon=True).start()

    def logout_codex(self, uid: str = "") -> dict:
        try:
            codex_cli.logout(self._codex_config_dir_for(uid))
        except Exception as e:  # noqa: BLE001
            return {"error": f"로그아웃하지 못했어요: {e}"}
        return self.check_codex(uid)

    def check_connectors(self, uid: str = "") -> dict:
        self.emit("connectors", _uid=uid, checking=True)
        st = integrations.connector_status(self._config_dir_for(uid))
        self._connectors = st
        self.emit("connectors", _uid=uid, **st)
        return st

    # ---------- ① 음성 → 텍스트 ----------
    def start_transcribe(self, p: dict, uid: str = "") -> dict:
        files = [clean(f) for f in p.get("files") or [] if str(f).strip()]
        out_dir = clean(p.get("out_dir"))
        if not files:
            return {"error": "변환할 음성·영상 파일을 고르세요."}
        missing = [str(f) for f in files if not f.is_file()]
        if missing:
            return {"error": "파일을 찾을 수 없어요:\n" + "\n".join(missing)}
        if not str(p.get("out_dir") or "").strip():
            return {"error": "저장할 폴더를 고르세요."}
        save_settings({"output_dir": str(out_dir), "language": p.get("language"), "model": p.get("model"),
                       "open_after": p.get("open_after"), "diarize": p.get("diarize"),
                       "speakers": p.get("speakers")})
        cmd = [str(PYTHON), "-u", str(HERE / "transcribe.py"), *map(str, files), "--out-dir", str(out_dir),
               "--model", p.get("model") or "large-v3-turbo", "--progress"]
        if LANGUAGES.get(p.get("language")):
            cmd += ["--language", LANGUAGES[p["language"]]]
        if p.get("diarize"):
            cmd += ["--speakers", "0" if p.get("speakers") in (None, "", "자동") else str(p["speakers"])]

        def done(outputs):
            if p.get("open_after"):
                _open_native(out_dir)
            return {"out_dir": str(out_dir)}
        if not self._jobs["t"].start(cmd, "시작하는 중… 모델을 불러오는 데 몇 초 걸려요", done, uid=uid,
                                     meta={"task": "받아쓰기", "file": ", ".join(f.name for f in files)}):
            return self._busy_error("t")
        return {}

    def list_speakers(self, txt: str) -> dict:
        path = clean(txt)
        if not path.is_file():
            return {"error": "대본 파일을 찾을 수 없어요."}
        return {"speakers": speakers.list_speakers(path)}

    def rename_speakers(self, txt: str, mapping: dict) -> dict:
        try:
            changed = speakers.rename_speakers(clean(txt), mapping or {})
        except OSError as e:
            return {"error": f"대본을 고치지 못했어요 (다른 프로그램에서 열려 있나요?): {e}"}
        return {"changed": [str(p) for p in changed], "speakers": speakers.list_speakers(clean(txt))}

    def apply_speaker_names(self, txt: str, mapping: dict, doc: str = "") -> dict:
        """화자 이름을 대본(.txt·.srt)과 이미 만든 회의록(JSON·문서 파일)에 함께 적용한다."""
        try:
            changed = speakers.rename_speakers(clean(txt), mapping or {})
            if str(doc or "").strip():
                changed += minutes.rename_in_minutes(clean(doc), mapping or {})
        except OSError as e:
            return {"error": f"파일을 고치지 못했어요 (다른 프로그램에서 열려 있나요?): {e}"}
        return {"changed": [str(p) for p in changed], "speakers": speakers.list_speakers(clean(txt))}

    # ---------- ② 회의록 만들기 ----------
    def start_minutes(self, p: dict, uid: str = "") -> dict:
        engine = str(p.get("engine") or "").strip() or "claude"
        if engine not in minutes.ENGINES:
            return {"error": f"알 수 없는 엔진이에요: {engine}"}
        if engine == "codex":
            config_dir = self._codex_config_dir_for(uid)
            if not codex_cli.auth_status(config_dir).get("loggedIn"):
                return {"error": "Codex가 연결돼 있지 않아요. 왼쪽 아래 [Codex 연결]을 눌러 주세요.", "need_login": True}
        else:
            config_dir = self._config_dir_for(uid)
            if not minutes.auth_status(config_dir).get("loggedIn"):
                return {"error": "Claude가 연결돼 있지 않아요. 왼쪽 아래 [Claude 연결]을 눌러 주세요.", "need_login": True}
        txt = clean(p.get("txt"))
        if not str(p.get("txt") or "").strip() or not txt.is_file():
            return {"error": "대본(.txt) 파일을 고르세요."}
        formats = [f for f in p.get("formats") or [] if f in minutes.FORMATS]
        if not formats:
            return {"error": "만들 형식을 하나 이상 고르세요."}
        out_dir = clean(p.get("out_dir")) if str(p.get("out_dir") or "").strip() else txt.parent
        template = clean(p.get("template")) if str(p.get("template") or "").strip() else None
        if template:
            if template.suffix.lower() == ".hwp":
                return {"error": "예전 한글 형식(.hwp)은 읽을 수 없어요. 한글에서 '다른 이름으로 저장 → HWPX'로 저장해 주세요."}
            if not template.is_file():
                return {"error": f"양식 파일을 찾을 수 없어요: {template}"}
        model = str(p.get("model") or "").strip()
        glossary = clean(p.get("glossary")) if str(p.get("glossary") or "").strip() else None
        if glossary and not glossary.is_file():
            return {"error": f"용어집 파일을 찾을 수 없어요: {glossary}"}
        save_settings({"minutes_output_dir": str(out_dir), "minutes_formats": formats,
                       "minutes_open_after": p.get("open_after"), "minutes_template": str(template or ""),
                       "minutes_model": model, "minutes_glossary": str(glossary or ""), "minutes_engine": engine})
        cmd = [str(PYTHON), "-u", str(HERE / "minutes.py"), str(txt), "--out-dir", str(out_dir),
               "--formats", ",".join(formats), "--engine", engine, "--progress"]
        if config_dir:
            cmd += ["--config-dir", config_dir]
        if template:
            cmd += ["--template", str(template)]
        if str(p.get("title") or "").strip():
            cmd += ["--title", p["title"].strip()]
        if str(p.get("date") or "").strip():
            cmd += ["--date", p["date"].strip()]
        if model:
            cmd += ["--model", model]
        if glossary:
            cmd += ["--glossary-file", str(glossary)]

        def done(outputs):
            if p.get("open_after"):
                _open_native(out_dir)
            return {"out_dir": str(out_dir)}
        label = f"양식: {template.name}" if template else "회사 기본 양식"
        engine_name = "Codex" if engine == "codex" else "Claude"
        if not self._jobs["m"].start(cmd, f"{engine_name}로 회의 내용을 정리하는 중… ({label})", done, uid=uid,
                                     meta={"task": "회의록 만들기", "file": txt.name}):
            return self._busy_error("m")
        return {}

    # ---------- ③ Jira·Confluence·Slack ----------
    def load_doc(self, path: str) -> dict:
        p = clean(path)
        if not p.is_file():
            return {"error": "파일을 찾을 수 없어요."}
        if p.suffix.lower() != ".json":
            return {"import": True}
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"error": f"회의록 데이터를 읽지 못했어요: {e}"}
        title = next((v for k, v in doc.get("meta", []) if "제목" in str(k) or "회의명" in str(k)), doc.get("title", ""))
        when = next((v for k, v in doc.get("meta", []) if "일시" in str(k)), "")
        return {"doc": {"title": title, "when": when, "actions": doc.get("actions") or [],
                        "sections": len(doc.get("sections") or [])}}

    def start_import(self, path: str, uid: str = "") -> dict:
        config_dir = self._config_dir_for(uid)
        if not minutes.auth_status(config_dir).get("loggedIn"):
            return {"error": "PDF 등 문서를 읽으려면 Claude 연결이 필요해요.", "need_login": True}
        cmd = [str(PYTHON), "-u", str(HERE / "integrations.py"), "import", str(clean(path)), "--progress"]
        if config_dir:
            cmd += ["--config-dir", config_dir]
        if not self._jobs["p"].start(cmd, f"'{clean(path).name}'을 Claude가 읽고 있어요… (30초~1분)",
                                     lambda outputs: {"imported": outputs[-1] if outputs else ""}, uid=uid,
                                     meta={"task": "문서 읽어오기", "file": clean(path).name}):
            return self._busy_error("p")
        return {}

    def start_publish(self, p: dict, uid: str = "") -> dict:
        config_dir = self._config_dir_for(uid)
        if not minutes.auth_status(config_dir).get("loggedIn"):
            return {"error": "Claude 연결이 필요해요.", "need_login": True}
        connectors = integrations.connector_status(config_dir)
        plan = p.get("plan") or {}
        need = []
        if (plan.get("confluence") or plan.get("jira")) and connectors.get("atlassian") != "connected":
            need.append("Atlassian")
        if plan.get("slack") and connectors.get("slack") != "connected":
            need.append("Slack")
        if need:
            return {"error": f"{', '.join(need)} 커넥터가 연결돼 있지 않아요. 왼쪽 아래 [연결 관리]에서 연결해 주세요."}
        save_settings(p.get("settings") or {})
        plan_path = Path(tempfile.gettempdir()) / "whisper_publish_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        cmd = [str(PYTHON), "-u", str(HERE / "integrations.py"), "publish", str(clean(p.get("doc"))),
               "--plan", str(plan_path), "--progress"]
        if config_dir:
            cmd += ["--config-dir", config_dir]

        def done(outputs):
            try:
                result = json.loads(outputs[-1]) if outputs else {}
            except ValueError:
                result = {}
            return {"summary": integrations.summarize_result(result) if result else ["결과를 읽지 못했어요. 로그를 확인하세요."]}
        if not self._jobs["p"].start(cmd, "Claude가 등록을 준비하는 중…", done, uid=uid,
                                     meta={"task": "공유", "file": clean(p.get("doc")).name}):
            return self._busy_error("p")
        return {}

    def start_list_targets(self, uid: str = "") -> dict:
        config_dir = self._config_dir_for(uid)
        if not minutes.auth_status(config_dir).get("loggedIn"):
            return {"error": "Claude 연결이 필요해요.", "need_login": True}
        connectors = integrations.connector_status(config_dir)
        if connectors.get("atlassian") not in (None, "connected"):
            return {"error": "Atlassian 커넥터가 연결돼 있지 않아요. 연결 관리에서 연결해 주세요."}
        out = Path(tempfile.gettempdir()) / "atlassian_targets.json"
        cmd = [str(PYTHON), "-u", str(HERE / "integrations.py"), "targets", "--out", str(out), "--progress"]
        if config_dir:
            cmd += ["--config-dir", config_dir]
        if connectors.get("slack") != "connected":
            cmd.append("--no-slack")

        def done(outputs):
            try:
                targets = json.loads(out.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"targets": None}
            save_settings({"atl_targets": targets})  # 다음에 창을 열어도 목록을 바로 쓴다
            return {"targets": targets}
        if not self._jobs["p"].start(cmd, "Confluence · Jira · Slack 목록을 읽는 중… (읽기만 해요, 1분쯤)", done, uid=uid,
                                     meta={"task": "대상 목록 불러오기"}):
            return self._busy_error("p")
        return {}

    def connector_url(self) -> str:
        return integrations.CONNECTOR_SETTINGS_URL

    # ---------- 연결 관리 (MCP) ----------
    def mcp_list(self, uid: str = "") -> dict:
        try:
            servers = mcp_manager.list_servers(self._config_dir_for(uid))
        except Exception as e:
            return {"error": str(e)}
        # 같은 결과로 ③ 화면의 커넥터 표시도 맞춘다
        self._connectors = {k: next((s["status"] for s in servers if s["name"] == f"claude.ai {n}"), "missing")
                            for k, n in integrations.CONNECTORS.items()}
        self.emit("connectors", _uid=uid, **self._connectors)
        return {"servers": servers}

    def mcp_add(self, name: str, url: str, transport: str, header: str = "") -> dict:
        try:
            mcp_manager.add_server(name, url, transport, [header] if header else [])
        except Exception as e:
            return {"error": str(e)}
        return self.mcp_list()

    def mcp_remove(self, name: str) -> dict:
        try:
            mcp_manager.remove_server(name)
        except Exception as e:
            return {"error": str(e)}
        return self.mcp_list()

    def mcp_auth_console(self) -> dict:
        try:
            mcp_manager.open_auth_console(HERE)
        except Exception as e:
            return {"error": str(e)}
        return {}

    def mcp_settings_url(self) -> str:
        return mcp_manager.SETTINGS_URL

    # ---------- ④ Jira → GitLab ----------
    def gitlab_token_state(self, repo: str) -> dict:
        try:
            base, path = gitlab_sync.parse_repo(repo)
        except ValueError as e:
            return {"error": str(e)}
        return {"saved": bool(gitlab_sync.get_token(base)), "base": base, "path": path}

    def gitlab_save_token(self, repo: str, token: str) -> dict:
        """화면에서 입력한 토큰을 Windows 자격 증명 관리자에 저장한다. 토큰은 화면으로 되돌려 보내지 않는다."""
        try:
            base, _ = gitlab_sync.parse_repo(repo)
            gitlab_sync.set_token(base, token or "")
        except Exception as e:
            return {"error": str(e)}
        save_settings({"gitlab_repo": repo.strip()})
        return {"saved": bool(token)}

    def gitlab_check(self, repo: str) -> dict:
        try:
            save_settings({"gitlab_repo": repo.strip()})
            return gitlab_sync.check(repo)
        except Exception as e:
            return {"error": str(e)}

    def start_jira_fetch(self, project: str, scope: str, uid: str = "") -> dict:
        config_dir = self._config_dir_for(uid)
        if not minutes.auth_status(config_dir).get("loggedIn"):
            return {"error": "Jira를 읽으려면 Claude 연결이 필요해요.", "need_login": True}
        if integrations.connector_status(config_dir).get("atlassian") not in (None, "connected"):
            return {"error": "Atlassian 커넥터가 연결돼 있지 않아요. 왼쪽 아래 [연결 관리]에서 Atlassian을 연결해 주세요."}
        project = (project or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", project):
            return {"error": "Jira 프로젝트 키를 확인하세요 (예: ANALYZE)."}
        save_settings({"jira_project": project, "gitlab_scope": scope})
        out = Path(tempfile.gettempdir()) / f"jira_{project}_issues.json"
        cmd = [str(PYTHON), "-u", str(HERE / "gitlab_sync.py"), "fetch", project, "--scope", scope,
               "--out", str(out), "--progress"]
        if config_dir:
            cmd += ["--config-dir", config_dir]

        def done(outputs):
            try:
                return {"issues": json.loads(out.read_text(encoding="utf-8"))["issues"]}
            except (OSError, ValueError, KeyError):
                return {"issues": []}
        if not self._jobs["g"].start(cmd, f"Claude가 Jira '{project}' 일감을 읽는 중… (읽기만 해요)", done, uid=uid,
                                     meta={"task": "Jira 일감 읽기", "file": project}):
            return self._busy_error("g")
        return {}

    def start_gitlab_push(self, repo: str, issues: list, labels: str, uid: str = "") -> dict:
        if not issues:
            return {"error": "GitLab에 올릴 일감을 하나 이상 체크하세요."}
        st = self.gitlab_token_state(repo)
        if st.get("error"):
            return st
        if not st["saved"]:
            return {"error": "GitLab 토큰을 먼저 저장하세요."}
        save_settings({"gitlab_repo": repo.strip(), "gitlab_labels": labels})
        src = Path(tempfile.gettempdir()) / "gitlab_push_issues.json"
        src.write_text(json.dumps({"issues": issues}, ensure_ascii=False), encoding="utf-8")
        cmd = [str(PYTHON), "-u", str(HERE / "gitlab_sync.py"), "push", str(src), repo.strip(),
               "--labels", labels or "", "--progress"]

        def done(outputs):
            try:
                results = json.loads(outputs[-1]) if outputs else []
            except ValueError:
                results = []
            return {"results": results, "summary": gitlab_sync.summarize(results) if results else []}
        if not self._jobs["g"].start(cmd, "GitLab에 이슈를 등록하는 중…", done, uid=uid,
                                     meta={"task": "GitLab 등록"}):
            return self._busy_error("g")
        return {}

    def kill_all(self) -> None:
        for j in self._jobs.values():
            j.kill()
