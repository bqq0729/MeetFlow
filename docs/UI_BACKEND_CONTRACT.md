# UI ↔ 백엔드 연동 문서 (personal 버전 — Codex 엔진 추가)

이 문서는 UI(`ui/` 폴더, Codex 담당)가 백엔드(`app.py`/`server.py`/`minutes.py`/`codex_cli.py`)에
새로 추가되거나 바뀐 부분을 연결할 때 참고하는 계약입니다. **백엔드 쪽에서 `ui/` 파일을 직접
고치지 않았습니다** — 아래 내용을 보고 UI에서 연결해 주세요.

호출 방식은 기존과 동일합니다: `api.<메서드>(...)` → `POST /api/<메서드>` (본문은 JSON 배열의
각 인자). 서버 알림은 `window.App.onEvent(event, data)`로 옵니다 (`server.py`의 `/events` 롱폴).

---

## 1. 포트 변경

이 personal 버전의 기본 포트가 **8788**로 바뀌었습니다 (원본 whisper-stt-web은 8787을 그대로
씀 — 같은 PC에서 둘 다 띄워도 충돌하지 않게 분리). UI 쪽에서 포트를 하드코딩한 곳은 없었지만,
문서·스크립트를 참고하던 곳이 있다면 8788로 맞춰 주세요. (`run.bat`/`run.sh` 모두 기본값
8788, `WEB_PORT` 환경변수로 덮어쓸 수 있음.)

## 2. `init()` 응답에 필드 추가

기존 필드(`settings`, `languages`, `home`, `output_dir`, `default_template`)에 아래가
추가됐습니다:

```json
{ "engines": ["claude", "codex"], ... }
```

회의록 정리에 고를 수 있는 엔진 목록입니다(현재는 고정값이지만, 나중에 서버에서 설치 여부로
걸러줄 수도 있으니 하드코딩 대신 이 값을 쓰는 걸 권장합니다).

## 3. Codex 연결 상태·로그인·로그아웃 (신규)

Claude 연결(②번 탭 아래 Claude 연결 카드)과 **완전히 같은 모양**으로 Codex용이 추가됐습니다.
메서드 이름과 이벤트 이름만 `claude` → `codex`로 바뀝니다.

| 메서드 | 설명 |
|---|---|
| `api.check_codex()` | 로그인 상태를 다시 확인 (응답은 안 쓰고 `"codex"` 이벤트로 옴) |
| `api.login_codex()` | 로그인 시작 |
| `api.logout_codex()` | 로그인 해제 |

`"codex"` 이벤트 데이터 모양:

```ts
// 확인됨(로그인 안 돼 있음)
{ ok: false, missing: boolean }   // missing=true면 이 PC에 codex 명령 자체가 없다는 뜻
// 확인됨(로그인 돼 있음)
{ ok: true }                       // Claude 쪽과 달리 email/plan 정보는 주지 않음 (codex login status가 안 줌)
// 로그인 진행 중 (주소/코드를 아직 모를 때)
{ ok: false, pending: true }
// 로그인 진행 중 (주소/코드를 알아낸 뒤)
{ ok: false, pending: true, login_url: "https://auth.openai.com/codex/device", login_code: "ABCD-12345" }
```

### Claude 로그인과의 중요한 차이 (UI에서 꼭 다르게 처리해야 하는 부분)

Claude는 `login_url`이 **그 사람만을 위한 1회용 주소**라서 새 탭을 그 주소로 바로 열고, 사용자가
그 페이지에서 받은 **코드를 화면 입력칸에 붙여넣어** `api.submit_login_code(code)`로 돌려줘야
로그인이 끝납니다.

Codex는 디바이스 코드 방식이라 다릅니다:

- `login_url`은 **고정 주소**(`https://auth.openai.com/codex/device`)입니다. 사람마다 다르지 않습니다.
- `login_code`가 **그 사람만을 위한 값**입니다 (예: `ABCD-12345`, 15분 유효). 사용자는 그 주소를
  열어서(새 탭으로 띄워도 되고, 그냥 안내만 해도 됩니다) **코드를 그 웹페이지 안에 직접 입력**합니다.
- **`submit_login_code`에 해당하는 메서드가 없습니다.** 화면 입력칸을 만들 필요가 없고, 백엔드가
  3초마다 로그인 완료 여부를 스스로 확인해서 되면 자동으로 `{ok: true}` 이벤트를 보냅니다.

UI 권장 모양: Claude 카드를 복제해서 "Codex 연결" 카드를 만들고, `pending && login_url`일 때
"이 주소를 열고 코드 `{login_code}`를 입력하세요" + 주소 링크만 보여주면 됩니다(코드 입력칸·전송
버튼은 필요 없음).

## 4. 회의록 만들기 — 엔진 선택 (`start_minutes`)

`api.start_minutes(p)`의 `p`에 **`engine`** 필드가 추가됐습니다(선택, 생략하면 `"claude"`로
동작 — 기존 UI가 수정 전이라도 그대로 동작합니다).

```ts
const p = {
  txt, out_dir, template, title, date, formats,
  model, glossary,
  engine: "claude" | "codex",   // 신규. 생략 시 "claude"
};
const r = await api.start_minutes(p);
```

- `model`은 엔진마다 뜻이 다릅니다: `engine:"claude"`면 `sonnet`/`opus`/`haiku` 같은 Claude
  모델명, `engine:"codex"`면 Codex가 받는 모델명(예: `gpt-5-codex`) 또는 빈 값(그 엔진 기본값).
  UI에서 엔진을 바꾸면 모델 입력칸의 placeholder/목록도 바꿔주는 게 좋습니다(필수는 아님 — 빈 값이면
  각 엔진 기본값을 씁니다).
- 에러 응답은 기존과 같은 모양이되 `need_login`이 뜨면 **어떤 엔진이 안 돼 있는지 메시지로
  구분**됩니다 ("Claude가 연결돼 있지 않아요…" / "Codex가 연결돼 있지 않아요…"). 기존 코드는
  `if (r.need_login) modal(...); api.login();` 이렇게 Claude 전용으로 되어 있는데, 이제는
  `engine`에 따라 `api.login()` 또는 `api.login_codex()`를 불러야 합니다:

```js
if (r.need_login) {
  const ok = await modal(
    p.engine === "codex" ? "Codex 연결 필요" : "Claude 연결 필요",
    r.error, [{label: "나중에", value: false}, {label: "지금 연결", value: true, primary: true}]
  );
  if (ok) (p.engine === "codex" ? api.login_codex() : api.login())();
}
```

그 외 진행 흐름(진행률 이벤트, 완료 후 받는 `out_dir`, 작업 중 잠금/"지금 다른 사람이 작업
중" 에러 등)은 엔진과 무관하게 전부 기존과 동일합니다.

## 5. 바뀌지 않은 것

- Jira·Confluence·Slack 연동(`start_publish`/`start_jira_fetch`/`start_list_targets` 등)은
  여전히 Claude(claude.ai 커넥터)만 씁니다. Codex는 **회의록 정리**에만 쓸 수 있습니다.
- `start_import`(문서 가져오기)는 백엔드 내부적으로 `engine` 파라미터를 받을 수 있게 돼
  있지만, 현재 `app.py`의 `start_import`는 항상 Claude를 씁니다(바꾸지 않음) — 필요해지면
  같은 방식으로 `engine`을 추가할 수 있습니다.
- 기존 Claude 카드/로그인/`submit_login_code` 동작은 전혀 바뀌지 않았습니다.

## 6. 검증한 내용 (백엔드 쪽)

- 실제 설치된 Codex CLI로 로그인 상태 확인(`codex login status`) 확인.
- `codex exec --json`으로 받아쓰기 대본 → 회의록 JSON 정리 전체 흐름을 실제로 돌려서 Claude
  엔진과 같은 문서 구조(JSON)가 나오는 것을 확인(기본 양식·회사 양식 모두).
- 기존 Claude 엔진 경로가 그대로 동작하는 것도 다시 확인(회귀 없음).
- 디바이스 코드 로그인 흐름(`codex login --device-auth`의 주소/코드 출력, `CODEX_HOME`으로
  사람별 로그인 분리)은 명령어 동작까지 확인했고, 화면에서의 로그인 성공 end-to-end는 아직
  실제 사용자 승인으로는 검증하지 못했습니다 — 연결 시 문제가 있으면 알려주세요.
