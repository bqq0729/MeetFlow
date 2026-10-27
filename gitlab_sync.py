"""Jira 일감을 GitLab 이슈로 옮긴다.

  Jira 읽기:   로컬 Claude Code가 claude.ai Atlassian 커넥터로 읽는다 (읽기 전용 지시).
  GitLab 쓰기: GitLab REST API를 직접 부른다. 토큰은 Windows 자격 증명 관리자(keyring)에 저장한다.

중복 방지: GitLab 이슈 제목을 "[JIRA-KEY] 요약"으로 만들고, 같은 키로 시작하는 이슈가 이미 있으면 건너뛴다.

사용 예 (보통은 화면 ④에서 실행):
    python gitlab_sync.py set-token http://내-gitlab-서버/그룹/프로젝트.git   (토큰을 물어본다)
    python gitlab_sync.py check http://내-gitlab-서버/그룹/프로젝트.git
    python gitlab_sync.py fetch PROJ --scope open --out jira_issues.json
    python gitlab_sync.py push jira_issues.json http://내-gitlab-서버/그룹/프로젝트.git --labels jira
"""

import argparse
import getpass
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KEYRING_SERVICE = "whisper-stt-gitlab"
SCOPES = {
    "open": "statusCategory != Done",
    "all": "",
    "recent": "created >= -7d",
}


# ---------------------------------------------------------------- 저장소 주소 · 토큰
def parse_repo(url: str) -> tuple[str, str]:
    """'http://host:port/group/proj.git' → ('http://host:port', 'group/proj')"""
    url = url.strip().strip('"')
    m = re.match(r"^(https?://[^/]+)/(.+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError("GitLab 저장소 주소는 http(s)://서버/그룹/프로젝트 형태여야 해요.")
    path = re.sub(r"/-/.*$", "", m.group(2))  # 웹 주소의 /-/issues 같은 뒷부분은 뗀다
    return m.group(1), path


def get_token(base: str) -> str | None:
    import keyring
    return keyring.get_password(KEYRING_SERVICE, base)


def set_token(base: str, token: str) -> None:
    import keyring
    if token:
        keyring.set_password(KEYRING_SERVICE, base, token.strip())
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, base)
        except Exception:
            pass


# ---------------------------------------------------------------- GitLab API
class GitLab:
    def __init__(self, base: str, token: str):
        self.base, self.token = base.rstrip("/"), token

    def call(self, method: str, path: str, params: dict | None = None, data: dict | None = None):
        url = f"{self.base}/api/v4{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            msg = {401: "GitLab 토큰이 잘못됐거나 만료됐어요.",
                   403: "이 토큰으로는 권한이 없어요. 토큰 범위(scope)에 'api'가 있는지, 프로젝트에 Reporter 이상 권한이 있는지 확인하세요.",
                   404: "GitLab 프로젝트를 찾을 수 없어요. 주소가 맞는지, 이 계정이 접근할 수 있는지 확인하세요."}.get(e.code)
            raise RuntimeError(f"{msg or 'GitLab 오류'} (HTTP {e.code}) {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitLab 서버에 연결하지 못했어요: {e.reason}") from None

    def project(self, path: str) -> dict:
        return self.call("GET", f"/projects/{urllib.parse.quote(path, safe='')}")

    def me(self) -> dict:
        return self.call("GET", "/user")

    def find_by_jira_key(self, project_id: int, key: str) -> dict | None:
        found = self.call("GET", f"/projects/{project_id}/issues",
                          {"search": key, "in": "title", "state": "all", "per_page": 50})
        return next((i for i in found or [] if i.get("title", "").startswith(f"[{key}]")), None)

    def find_by_title(self, project_id: int, title: str) -> dict | None:
        found = self.call("GET", f"/projects/{project_id}/issues",
                          {"search": title[:100], "in": "title", "state": "all", "per_page": 50})
        return next((i for i in found or [] if i.get("title", "").strip() == title), None)

    def create_issue(self, project_id: int, fields: dict) -> dict:
        return self.call("POST", f"/projects/{project_id}/issues", data=fields)


def connect(repo_url: str) -> tuple[GitLab, dict]:
    base, path = parse_repo(repo_url)
    token = get_token(base)
    if not token:
        raise RuntimeError("GitLab 토큰이 저장돼 있지 않아요. 화면 ④에서 토큰을 입력하고 [저장]을 눌러 주세요.")
    gl = GitLab(base, token)
    return gl, gl.project(path)


def check(repo_url: str) -> dict:
    gl, proj = connect(repo_url)
    me = gl.me()
    return {"project": proj.get("path_with_namespace"), "name": proj.get("name"), "web_url": proj.get("web_url"),
            "user": me.get("username"), "issues_enabled": proj.get("issues_enabled", True)}


# ---------------------------------------------------------------- Jira 읽기 (Claude + Atlassian 커넥터)
def fetch_jira(project_key: str, scope: str, limit: int, progress, status, log,
               config_dir: str | None = None) -> list[dict]:
    from integrations import run_with_tools

    cond = SCOPES.get(scope, SCOPES["open"])
    jql = f"project = {project_key}" + (f" AND {cond}" if cond else "") + " ORDER BY created DESC"
    prompt = (
        "너는 Jira 조회 담당이다. 읽기 전용이다: 어떤 것도 만들거나, 고치거나, 댓글을 달거나, 상태를 바꾸지 않는다.\n"
        f"Atlassian 도구로 JQL `{jql}` 에 맞는 일감을 최대 {limit}개 조회해라.\n"
        "끝나면 설명 없이 아래 JSON 하나만 출력한다:\n"
        '{"issues": [{"key": "ABC-1", "summary": "요약", "type": "이슈 유형", "status": "상태", '
        '"assignee": "담당자 이름 또는 null", "priority": "우선순위 또는 null", "due": "YYYY-MM-DD 또는 null", '
        '"url": "일감 웹 링크", "description": "설명을 일반 텍스트로, 2000자 이내 (없으면 빈 문자열)"}], '
        '"note": "조회 중 문제가 있었으면 적고, 없으면 빈 문자열"}'
    )
    status(f"Claude가 Jira '{project_key}' 일감을 읽는 중…")
    result = run_with_tools(prompt, "(입력 없음)", ["mcp__claude_ai_Atlassian"], 3, progress, status, log,
                            config_dir=config_dir)
    if "raw" in result:
        raise RuntimeError("Jira 조회 결과를 읽지 못했어요:\n" + result["raw"][:500])
    if result.get("note"):
        log(f"메모: {result['note']}")
    return result.get("issues") or []


# ---------------------------------------------------------------- GitLab 쓰기
def title_of(issue: dict) -> str:
    """Jira 번호가 있으면 "[KEY] 요약", 없으면(회의록 할 일 그대로) "[회의록] 요약"."""
    tag = issue.get("key") or "회의록"
    return f"[{tag}] {issue.get('summary') or ''}".strip()


def issue_fields(issue: dict, labels: list[str]) -> dict:
    if not issue.get("key"):  # Jira 없이 회의록 할 일을 바로 옮기는 경우
        rows = [("출처", issue.get("source") or "회의록"), ("담당자", issue.get("assignee")), ("기한", issue.get("due"))]
        table = "| 항목 | 값 |\n| --- | --- |\n" + "\n".join(f"| {k} | {v} |" for k, v in rows if v)
        fields = {"title": title_of(issue), "description": f"> 회의록에서 나온 할 일이에요.\n\n{table}\n",
                  "labels": ",".join(l for l in labels if l)}
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(issue.get("due") or "")):
            fields["due_date"] = issue["due"]
        return fields
    rows = [("Jira", f"[{issue['key']}]({issue.get('url') or ''})"), ("유형", issue.get("type")),
            ("상태", issue.get("status")), ("담당자", issue.get("assignee")), ("우선순위", issue.get("priority")),
            ("기한", issue.get("due"))]
    table = "| 항목 | 값 |\n| --- | --- |\n" + "\n".join(f"| {k} | {v} |" for k, v in rows if v)
    desc = (issue.get("description") or "").strip()
    body = f"> Jira 일감 **{issue['key']}** 에서 가져온 이슈예요.\n\n{table}\n" + (f"\n---\n\n{desc}\n" if desc else "")
    fields = {"title": title_of(issue), "description": body,
              "labels": ",".join(l for l in labels if l)}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(issue.get("due") or "")):
        fields["due_date"] = issue["due"]
    return fields


def push(issues: list[dict], repo_url: str, labels: list[str], progress, status, log) -> list[dict]:
    gl, proj = connect(repo_url)
    pid = proj["id"]
    results = []
    for n, issue in enumerate(issues, 1):
        key = issue.get("key") or (issue.get("summary") or "")[:30]
        status(f"GitLab에 등록 중… ({n}/{len(issues)}) {key}")
        try:
            existing = (gl.find_by_jira_key(pid, issue["key"]) if issue.get("key")
                        else gl.find_by_title(pid, title_of(issue)))
            if existing:
                results.append({"key": key, "status": "exists", "iid": existing["iid"], "url": existing["web_url"]})
                log(f"= {key}: 이미 있음 → #{existing['iid']}")
            else:
                made = gl.create_issue(pid, issue_fields(issue, labels))
                results.append({"key": key, "status": "created", "iid": made["iid"], "url": made["web_url"]})
                log(f"+ {key}: 만듦 → #{made['iid']} {made['web_url']}")
        except RuntimeError as e:
            results.append({"key": key, "status": "error", "error": str(e)})
            log(f"! {key}: {e}")
        progress(n / len(issues))
    return results


def summarize(results: list[dict]) -> list[str]:
    word = {"created": "새로 만듦", "exists": "이미 있어서 건너뜀", "error": "실패"}
    lines = []
    for r in results:
        tail = f"#{r['iid']}  {r['url']}" if r.get("url") else r.get("error", "")
        lines.append(f"{r['key']}: {word.get(r['status'], r['status'])}  {tail}")
    made = sum(r["status"] == "created" for r in results)
    return [f"새 이슈 {made}개 · 건너뜀 {sum(r['status'] == 'exists' for r in results)}개 · "
            f"실패 {sum(r['status'] == 'error' for r in results)}개", ""] + lines


# ---------------------------------------------------------------- CLI
def main() -> None:
    parser = argparse.ArgumentParser(description="Jira 일감 → GitLab 이슈")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("set-token")
    p.add_argument("repo")
    p = sub.add_parser("check")
    p.add_argument("repo")
    p = sub.add_parser("fetch")
    p.add_argument("project")
    p.add_argument("--scope", choices=list(SCOPES), default="open")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--progress", action="store_true")
    p.add_argument("--config-dir", default=None, help="이 사람 전용 Claude 로그인 폴더 (생략하면 공용 로그인)")
    p = sub.add_parser("push")
    p.add_argument("issues", type=Path, help="fetch가 만든 JSON (GUI에서 고른 일감만 담긴 파일)")
    p.add_argument("repo")
    p.add_argument("--labels", default="jira")
    p.add_argument("--progress", action="store_true")
    args = parser.parse_args()

    progress_on = getattr(args, "progress", False)
    status = lambda m: print(f"@@STATUS {m}" if progress_on else m, flush=True)  # noqa: E731
    progress = lambda x: progress_on and print(f"@@PROGRESS {x:.4f}", flush=True)  # noqa: E731
    log = lambda m: print(m, flush=True)  # noqa: E731
    try:
        if args.cmd == "set-token":
            base, _ = parse_repo(args.repo)
            set_token(base, getpass.getpass(f"{base} 의 GitLab 토큰 (화면에 안 보여요): "))
            print("저장했어요.")
        elif args.cmd == "check":
            print(json.dumps(check(args.repo), ensure_ascii=False))
        elif args.cmd == "fetch":
            issues = fetch_jira(args.project, args.scope, args.limit, progress, status, log,
                               config_dir=args.config_dir)
            args.out.write_text(json.dumps({"project": args.project, "issues": issues}, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            log(f"Jira 일감 {len(issues)}개를 읽었어요.")
            print(f"@@OUTPUT {args.out}", flush=True)
        elif args.cmd == "push":
            issues = json.loads(args.issues.read_text(encoding="utf-8"))["issues"]
            labels = [l.strip() for l in args.labels.split(",")]
            results = push(issues, args.repo, labels, progress, status, log)
            log("\n".join(["── 결과 ──"] + summarize(results)))
            print("@@RESULT " + json.dumps(results, ensure_ascii=False), flush=True)
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
