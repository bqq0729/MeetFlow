# UI 추가 전달 필드

UI는 기존 start_minutes 요청에 engine, model을 백엔드 계약대로 연결했습니다.
추가로 document_type과 document_instructions를 전달합니다. 이 두 필드는 백엔드에서 아직 처리하지 않습니다.

document_type 값: minutes(회의록), interview(인터뷰 정리), lecture(강의 노트), report(업무 보고서), summary(핵심 요약), custom(자유 형식).
document_instructions: 추가 요청 문자열.

백엔드 담당자는 선택된 종류에 맞는 프롬프트/기본 양식을 적용해야 합니다. UI의 ui/ 파일은 수정하지 마세요.
Codex 로그인 코드는 화면에 표시하고 로그인 페이지에서 사용자가 직접 입력합니다. Claude 인증 코드는 기존 submit_login_code로 전달합니다.

## 문서 종류 기능 활성화
UI는 init().document_types 배열에 서버가 반환한 종류만 실제 실행합니다. 배열이 없으면 minutes만 실행합니다.
추가 문서 종류 프롬프트를 구현한 뒤 init 응답에 지원하는 document_type 값 목록을 넣어 주세요.
지원되지 않는 문서 종류는 선택·설명을 확인할 수 있지만 실행 시 준비 중 안내를 표시해 회의록으로 잘못 생성되는 것을 방지합니다.

## 단일 AI 도구 연결
UI는 minutes_engine 선택값에 따라 한 가지 로그인 카드만 보여줍니다.
연결된 AI를 바꾸면 기존 logout/logout_codex API로 이전 연결을 해제합니다.
문서 정리는 start_minutes.engine을 사용하며, Codex 선택 시 업무 연동은 현재 Claude 전용이라는 안내를 표시합니다.
