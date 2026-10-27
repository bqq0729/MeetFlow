"""회의록을 Jira·Confluence·Slack에 올린다. 로컬 Claude Code(`claude -p`)가 claude.ai 커넥터(MCP)로 처리한다.

필요한 것: claude.ai 설정 → 커넥터에서 Atlassian(Jira·Confluence)과 Slack을 연결해 두기.
이 PC의 Claude Code가 같은 claude.ai 계정으로 로그인돼 있으면 그 커넥터를 그대로 쓴다.

사용 예 (보통은 GUI ③ 탭에서 실행):
    python integrations.py status
    python integrations.py publish 회의록.json --plan plan.json --progress
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from minutes import find_claude, to_markdown

CONNECTORS = {"atlassian": "Atlassian", "slack": "Slack"}  # 키 → claude.ai 커넥터 이름
DEFAULT_SLACK_NOTICE = "오늘 회의 업무 내용이 공유되었습니다. 각 담당자분들은 일감 및 내용 확인 바랍니다."
CONNECTOR_SETTINGS_URL = "https://claude.ai/settings/connectors"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- 연결 상태
def connector_status(config_dir: str | None = None) -> dict:
    """{"atlassian": "connected" | "needs_auth" | "missing" | "error", "slack": ...}
    (목록을 읽으면서, 연결된 커넥터에 남은 '인증 필요' 캐시도 정리된다 — mcp_manager 참고)"""
    from mcp_manager import list_servers

    try:
        servers = list_servers(config_dir)
    except Exception as e:
        return {k: "error" for k in CONNECTORS} | {"error": str(e)}
    result = {}
    for key, name in CONNECTORS.items():
        s = next((s for s in servers if re.fullmatch(rf"claude\.ai {name}", s["name"], re.I)), None)
        result[key] = "missing" if not s else s["status"] if s["status"] in ("connected", "needs_auth") else "needs_auth"
    return result


# ---------------------------------------------------------------- 등록 지시문
def build_prompt(plan: dict) -> str:
    steps, n = [], 0
    if plan.get("confluence"):
        c = plan["confluence"]
        n += 1
        steps.append(
            f"{n}. Confluence: 스페이스 키 '{c['space']}'에 제목 '{c['title']}'으로 새 페이지를 만든다. "
            + (f"상위 페이지는 제목이 '{c['parent']}'인 페이지다(검색해서 id를 찾는다). " if c.get("parent") else "")
            + "본문은 표준입력의 [회의록 본문 Markdown]을 그대로 쓴다. 같은 제목의 페이지가 이미 있으면 "
              "새로 만들지 말고 그 페이지 링크를 결과에 적고 note에 '이미 있음'이라고 쓴다.")
    if plan.get("jira"):
        j = plan["jira"]
        n += 1
        def who(it):
            if it.get("assignee_id"):
                return f"{it.get('assignee')} (accountId {it['assignee_id']}, 이 id로 바로 지정)"
            return it.get("assignee") or "지정 안 함"
        # 일감마다 프로젝트가 다를 수 있다 (없으면 기본 프로젝트)
        items = "\n".join(
            f"   - 프로젝트: {it.get('project') or j.get('project')} / 요약: {it['task']} / 담당자: {who(it)} / "
            f"기한: {it.get('due') or '없음'}"
            for it in j["items"])
        steps.append(
            f"{n}. Jira: 아래 일감을 하나씩 각자의 프로젝트에 만든다. 이슈 유형은 '{j['issue_type']}'이고, "
            "그 프로젝트에 같은 이름의 유형이 없으면 가장 비슷한 유형(작업/Task)을 쓴다.\n"
            f"{items}\n"
            "   설명에는 '회의록에서 나온 할 일'이라는 문장과 회의 제목, (Confluence 페이지를 만들었다면) 그 링크를 넣는다. "
            "담당자는 이름·이메일로 Jira 계정을 찾아 지정하고, 못 찾으면 비워 두고 note에 적는다. "
            "기한이 YYYY-MM-DD로 바꿀 수 있는 날짜면 마감일로 넣고, 아니면 설명에만 적는다.")
    if plan.get("slack"):
        s = plan["slack"]
        n += 1
        others = [x for x in ("confluence", "jira") if plan.get(x)]
        guard = (" 단, 앞 단계(Confluence·Jira)가 모두 실패했으면 Slack 메시지는 보내지 말고 slack.ok=false, "
                 "note='앞 단계가 모두 실패해서 보내지 않음'으로 기록한다." if others else "")
        notice = (s.get("message") or DEFAULT_SLACK_NOTICE).strip()
        steps.append(
            f"{n}. Slack: 채널 '{s['channel']}'에 짧은 안내 메시지를 하나 보낸다. 회의 내용 요약·결정 사항은 넣지 않는다. "
            "아래 형식 그대로 쓰고, 링크는 실제로 만들어진 것만 넣는다(표준입력에 [앞 단계 결과 JSON]이 있으면 거기서 가져온다):\n"
            "   첫 줄: *회의록 제목* (Confluence 페이지 제목, 없으면 회의 제목)\n"
            f"   둘째 줄: {notice}\n"
            "   • 회의록: Confluence 페이지 링크\n"
            "   • 일감: 만들어진 Jira 일감마다 '번호(링크) 담당자' 를 쉼표로 이어서 한 줄 (담당자 없으면 번호만)\n"
            "   • GitLab: 만들어진 GitLab 이슈가 있으면 '#번호(링크)' 를 쉼표로 이어서 한 줄" + guard)
    return (
        "너는 회의록 등록 담당이다. 표준입력에 [회의록 데이터 JSON]과 [회의록 본문 Markdown]이 있다.\n"
        "아래 작업만 순서대로 하고, 그 밖의 일(다른 문서 수정, 다른 채널 전송 등)은 하지 않는다.\n\n"
        + "\n".join(steps)
        + "\n\n어느 단계가 실패하면 나머지는 계속하고 실패 이유를 기록한다.\n"
        "끝나면 설명 없이 아래 JSON 하나만 출력한다:\n"
        '{"confluence": {"url": "링크 또는 null", "note": "메모"},\n'
        ' "jira": [{"summary": "요약", "key": "ABC-12 또는 null", "url": "링크 또는 null", "note": "메모"}],\n'
        ' "slack": {"channel": "채널", "ok": true, "note": "메모"},\n'
        ' "errors": ["실패한 내용"]}\n'
        "하지 않은 단계의 값은 null로 둔다."
    )


def expected_calls(plan: dict) -> int:
    """진행률 계산용: 도구 호출이 대략 몇 번 일어날지."""
    n = 0
    if plan.get("confluence"):
        n += 3 + (1 if plan["confluence"].get("parent") else 0)  # 스페이스 조회 + 중복 확인 + 생성 (+ 상위 검색)
    if plan.get("jira"):
        items = plan["jira"]["items"]
        n += 1 + len(items) + sum(1 for it in items if it.get("assignee"))  # 프로젝트 확인 + 생성 + 담당자 조회
    if plan.get("slack"):
        n += 2  # 채널 찾기 + 전송
    return max(n, 1)


TOOL_LABELS = [("confluence", "Confluence 작업 중"), ("page", "Confluence 작업 중"), ("jira", "Jira 작업 중"),
               ("issue", "Jira 작업 중"), ("slack", "Slack 작업 중"), ("channel", "Slack 작업 중"),
               ("message", "Slack 작업 중")]


# ---------------------------------------------------------------- 실행
def publish(doc: dict, plan: dict, on_progress=None, on_status=None, log=print, timeout: int = 1200,
            config_dir: str | None = None) -> dict:
    progress = on_progress or (lambda p: None)
    status = on_status or (lambda s: None)
    allowed = []
    if plan.get("confluence") or plan.get("jira"):
        allowed.append("mcp__claude_ai_Atlassian")
    if plan.get("slack"):
        allowed.append("mcp__claude_ai_Slack")
    if not allowed and not plan.get("gitlab"):
        raise RuntimeError("등록할 곳을 하나 이상 고르세요.")

    stdin_text = ("[회의록 데이터 JSON]\n" + json.dumps(doc, ensure_ascii=False)
                  + "\n\n[회의록 본문 Markdown]\n" + to_markdown(doc))
    status("Claude가 등록을 준비하는 중…")
    if not plan.get("gitlab"):
        return run_with_tools(build_prompt(plan), stdin_text, allowed, expected_calls(plan), progress, status, log,
                              timeout, config_dir)

    # GitLab이 있으면 세 단계로 나눈다: ① Confluence·Jira (Claude) → ② GitLab (직접 API) → ③ Slack (Claude)
    # Slack 메시지에 GitLab 이슈 링크까지 넣기 위해서다.
    first = {k: v for k, v in plan.items() if k in ("confluence", "jira")}
    has_slack = bool(plan.get("slack"))
    w1, w2 = (0.6, 0.75) if has_slack else (0.8, 1.0)
    result: dict = {"errors": []}
    if first:
        result = run_with_tools(build_prompt(first), stdin_text, ["mcp__claude_ai_Atlassian"], expected_calls(first),
                                lambda p: progress(w1 * p), status, log, timeout, config_dir)
        result.setdefault("errors", [])
    progress(w1)

    # ② GitLab: 만들어진 Jira 일감 → GitLab 이슈. Jira를 안 켰으면 체크한 할 일을 그대로 옮긴다.
    import gitlab_sync
    g = plan["gitlab"]
    items = (plan.get("jira") or {}).get("items") or g.get("items") or []
    if plan.get("jira"):
        made = [j for j in result.get("jira") or [] if j.get("key")]
        issues = []
        for j in made:
            src = next((it for it in items if it.get("task") == j.get("summary")), {})
            issues.append({"key": j["key"], "summary": j.get("summary"), "url": j.get("url"),
                           "assignee": src.get("assignee"), "due": src.get("due"), "status": "할 일"})
    else:
        title = doc.get("title") or "회의록"
        issues = [{"summary": it["task"], "assignee": it.get("assignee"), "due": it.get("due"),
                   "source": f"회의록: {title}"} for it in items]
    if issues:
        try:
            result["gitlab"] = gitlab_sync.push(issues, g["repo"], g.get("labels") or [],
                                                lambda p: progress(w1 + (w2 - w1) * p), status, log)
        except Exception as e:
            result["gitlab"] = []
            result["errors"].append(f"GitLab: {e}")
    else:
        result["gitlab"] = []
        result["errors"].append("GitLab: 옮길 일감이 없어요 (Jira 일감이 만들어지지 않았어요).")
    progress(w2)

    # ③ Slack: 앞 단계가 하나라도 성공했을 때만, 그 결과(링크)를 넣어 보낸다
    if has_slack:
        ok_before = (bool((result.get("confluence") or {}).get("url"))
                     or any(j.get("key") for j in result.get("jira") or [])
                     or any(x.get("status") in ("created", "exists") for x in result.get("gitlab") or []))
        if not ok_before:
            result["slack"] = {"channel": plan["slack"]["channel"], "ok": False,
                               "note": "앞 단계가 모두 실패해서 보내지 않음"}
        else:
            prev = {k: result.get(k) for k in ("confluence", "jira", "gitlab")}
            slack_in = stdin_text + "\n\n[앞 단계 결과 JSON]\n" + json.dumps(prev, ensure_ascii=False)
            r3 = run_with_tools(build_prompt({"slack": plan["slack"]}), slack_in, ["mcp__claude_ai_Slack"], 2,
                                lambda p: progress(w2 + (1 - w2) * p), status, log, timeout, config_dir)
            result["slack"] = r3.get("slack") or {"channel": plan["slack"]["channel"], "ok": False,
                                                  "note": str(r3.get("raw", ""))[:200]}
            result["errors"] += r3.get("errors") or []
    progress(1.0)
    return result


def run_with_tools(prompt: str, stdin_text: str, allowed: list[str], total_calls: int, progress, status, log,
                   timeout: int = 1200, config_dir: str | None = None) -> dict:
    """claude -p 를 커넥터 도구(allowed)만 허락해 실행하고, 마지막에 출력한 JSON을 돌려준다.
    도구 호출이 끝날 때마다 진행률을 올린다 (예상 호출 수 total_calls 기준)."""
    from minutes import claude_env
    done, final = 0, None
    total = max(total_calls, 1)
    progress(0.03)
    with tempfile.TemporaryDirectory() as tmp:
        proc = subprocess.Popen(
            [find_claude(), "-p", prompt, "--output-format", "stream-json", "--verbose",
             "--allowedTools", *allowed, "--disallowedTools", "Bash", "Edit", "Write", "NotebookEdit", "WebFetch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", cwd=tmp, env=claude_env(config_dir), creationflags=NO_WINDOW)
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
                kind = ev.get("type")
                if kind == "assistant":
                    for c in ev.get("message", {}).get("content", []):
                        if c.get("type") == "tool_use":
                            name = c.get("name", "")
                            short = name.split("__")[-1]
                            label = next((t for k, t in TOOL_LABELS if k in short.lower()), "작업 중")
                            status(f"{label}… ({short})")
                            log(f"→ {short}")
                elif kind == "user":
                    for c in ev.get("message", {}).get("content", []) or []:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            done += 1
                            progress(0.05 + 0.9 * min(done / total, 0.97))
                            if c.get("is_error"):
                                text = c.get("content")
                                log(f"  ! 도구 오류: {str(text)[:200]}")
                elif kind == "result":
                    final = ev
            proc.wait()
        finally:
            killer.cancel()
        err = proc.stderr.read()

    if final is None:
        raise RuntimeError(f"claude 응답을 읽지 못했어요.\n{err[-500:]}")
    if final.get("is_error"):
        raise RuntimeError(f"claude 오류: {final.get('result')}")
    from minutes import report_usage
    report_usage(final, stage="Claude 등록·조회")
    text = str(final.get("result", ""))
    start, end = text.find("{"), text.rfind("}")
    try:
        result = json.loads(text[start:end + 1]) if start >= 0 < end else {"raw": text}
    except json.JSONDecodeError:
        result = {"raw": text}
    if "raw" in result and ("인증" in text or "authenticat" in text.lower()):
        raise RuntimeError("Atlassian 커넥터가 인증되지 않았어요. claude.ai 설정 → 커넥터에서 Atlassian을 연결(인증)한 뒤 "
                           "다시 시도하세요.\n" + text[:300])
    progress(1.0)
    return result


def list_targets(progress, status, log, slack: bool = True, config_dir: str | None = None) -> dict:
    """올릴 수 있는 곳 목록 (읽기 전용): Confluence 스페이스, Jira 프로젝트(+이슈 유형), Slack 채널."""
    allowed = ["mcp__claude_ai_Atlassian"] + (["mcp__claude_ai_Slack"] if slack else [])
    prompt = (
        "읽기 전용이다. 아무것도 만들거나, 보내거나, 고치지 않는다.\n"
        "Atlassian 도구로 (1) 보이는 Confluence 스페이스(보관된 것 제외, 개인 스페이스 포함)의 key와 이름, "
        "(2) 보이는 Jira 프로젝트의 key, 이름, 쓸 수 있는 이슈 유형 이름들, 그리고 담당자 후보를 조회해라. "
        "담당자 후보는 각 프로젝트의 최근 일감 100개(JQL: project = KEY ORDER BY updated DESC)에 나온 담당자·보고자를 "
        "중복 없이 모은 것으로, 표시 이름과 accountId를 적는다. 나 자신(현재 로그인한 사용자)도 넣는다.\n"
        + ("Slack 도구로 (3) 워크스페이스 주소(https://xxx.slack.com)와, 내가 멤버인 채널의 이름과 채널 id를 조회해라.\n"
           if slack else "")
        + "끝나면 설명 없이 아래 JSON 하나만 출력한다:\n"
        '{"site": "사이트 주소", "spaces": [{"key": "KEY", "name": "이름"}], '
        '"projects": [{"key": "KEY", "name": "이름", "issue_types": ["작업"], '
        '"users": [{"name": "표시 이름", "accountId": "id"}]}], '
        '"slack_url": "https://xxx.slack.com", "channels": [{"name": "채널이름", "id": "C123"}]}'
    )
    status("Claude가 Confluence · Jira" + (" · Slack" if slack else "") + " 목록을 읽는 중… (읽기만 해요)")
    result = run_with_tools(prompt, "(입력 없음)", allowed, 7 if slack else 6, progress, status, log,
                            config_dir=config_dir)
    if "raw" in result:
        raise RuntimeError("목록을 읽지 못했어요:\n" + result["raw"][:400])
    # 화면은 채널 이름 목록을 쓰고, '열기' 링크는 이름 → id 표를 쓴다
    names, ids = [], {}
    for c in result.get("channels") or []:
        name = "#" + str(c.get("name") if isinstance(c, dict) else c).lstrip("#")
        names.append(name)
        if isinstance(c, dict) and c.get("id"):
            ids[name] = c["id"]
    result["channels"], result["channel_ids"] = names, ids
    return result


def summarize_result(result: dict) -> list[str]:
    lines = []
    c = result.get("confluence")
    if c:
        lines.append(f"Confluence: {c.get('url') or '실패'}" + (f" ({c['note']})" if c.get("note") else ""))
    for j in result.get("jira") or []:
        lines.append(f"Jira {j.get('key') or '실패'}: {j.get('summary')}" + (f" ({j['note']})" if j.get("note") else ""))
    for gl in result.get("gitlab") or []:
        word = {"created": "새로 만듦", "exists": "이미 있음", "error": "실패"}.get(gl.get("status"), gl.get("status"))
        lines.append(f"GitLab {gl.get('key')}: {word} {('#' + str(gl['iid']) + ' ' + gl['url']) if gl.get('url') else gl.get('error', '')}")
    s = result.get("slack")
    if s:
        lines.append(f"Slack {s.get('channel')}: {'전송됨' if s.get('ok') else '실패'}"
                     + (f" ({s['note']})" if s.get("note") else ""))
    lines += [f"오류: {e}" for e in result.get("errors") or []]
    if "raw" in result:
        lines.append(result["raw"][:500])
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="회의록 → Jira·Confluence·Slack 등록 (claude.ai 커넥터 사용)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="커넥터 연결 상태를 JSON으로 출력")
    tg = sub.add_parser("targets", help="올릴 수 있는 스페이스·프로젝트·채널 목록 (읽기 전용)")
    tg.add_argument("--out", type=Path, required=True)
    tg.add_argument("--no-slack", action="store_true")
    tg.add_argument("--progress", action="store_true")
    imp = sub.add_parser("import", help="작성된 회의록(.pdf/.docx/.hwpx/.md/.txt)을 회의록 데이터(.json)로 변환")
    imp.add_argument("file", type=Path)
    imp.add_argument("--progress", action="store_true")
    p = sub.add_parser("publish", help="회의록을 등록")
    p.add_argument("doc", type=Path, help="minutes.py가 만든 회의록 .json")
    p.add_argument("--plan", type=Path, required=True, help="무엇을 어디에 올릴지 적은 JSON (GUI가 만든다)")
    p.add_argument("--progress", action="store_true", help="GUI용 @@STATUS/@@PROGRESS 줄 출력")
    for sp in (sub.choices["status"], tg, imp, p):
        sp.add_argument("--config-dir", default=None, help="이 사람 전용 Claude 로그인 폴더 (생략하면 공용 로그인)")
    args = parser.parse_args()

    if args.cmd == "status":
        print(json.dumps(connector_status(args.config_dir), ensure_ascii=False))
        return

    def status(msg):
        print(f"@@STATUS {msg}" if args.progress else msg, flush=True)

    def progress(x):
        if args.progress:
            print(f"@@PROGRESS {x:.4f}", flush=True)

    if args.cmd == "targets":
        try:
            t = list_targets(progress, status, lambda m: print(m, flush=True), slack=not args.no_slack,
                             config_dir=args.config_dir)
            args.out.write_text(json.dumps(t, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"오류: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
        progress(1.0)
        print(f"스페이스 {len(t.get('spaces', []))}개 · 프로젝트 {len(t.get('projects', []))}개 · "
              f"채널 {len(t.get('channels', []))}개", flush=True)
        print(f"@@OUTPUT {args.out}", flush=True)
        return

    if args.cmd == "import":
        from minutes import import_document

        try:
            status(f"'{args.file.name}' 읽는 중…")
            doc = import_document(args.file, progress, status, log=lambda m: print(m, flush=True),
                                  config_dir=args.config_dir)
            # 원본 옆에 저장하되, 원본이 이미 _회의록.json 이름과 겹치지 않게 한다
            out = args.file.with_name(f"{args.file.stem}_회의록데이터.json")
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"오류: {e}", file=sys.stderr, flush=True)
            sys.exit(1)
        progress(1.0)
        print(f"변환 완료: {out} (할 일 {len(doc.get('actions', []))}개)", flush=True)
        print(f"@@OUTPUT {out}", flush=True)
        return

    try:
        doc = json.loads(args.doc.read_text(encoding="utf-8"))
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        result = publish(doc, plan, progress, status, log=lambda m: print(m, flush=True), config_dir=args.config_dir)
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    print("\n".join(["── 결과 ──"] + summarize_result(result)), flush=True)
    print("@@RESULT " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
