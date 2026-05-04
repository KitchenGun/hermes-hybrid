# USER — installer_ops가 알아야 할 사용자 컨텍스트

이 파일은 installer_ops가 install plan을 만들 때 참고하는 사용자 프로필이다.
char_limit 1375. 사실만 적고 추측 금지.

## 직무 / 환경
- Unreal Engine / Unity 기반 게임 엔진 개발자
- 호스트: Windows 11 + WSL2. Ollama는 Windows 호스트에서 실행
- 레포: `hermes-hybrid` (git, 메인 브랜치 `main`)

## 사용 중 프로파일 (변경 대상이 될 수 있는 곳)
- `kk_job`: 게임 프로그래머 채용 리서치 + 이력 관리
- `journal_ops`: Discord #일기 채널 → 24필드 활동 로그 → Google Sheets
- `calendar_ops`: 일정 관리
- `mail_ops`: 메일 처리
- `advisor_ops`: 도구 추천 (이 워커에 task 보내는 쪽 — 자기 자신 변경은 안 함)

## install plan 우선순위 (사용자 선호)
1. **명시적 사용자 실행 명령** — plan에 즉시 복붙 가능한 형태
2. **롤백 계획** — 변경 실패 시 되돌릴 수 있는 명령 또는 git diff 절차
3. **충돌 사전 검사** — 기존 잡/스킬과 중복은 ⚠️ 표시
4. **영향 범위 명시** — "이 변경이 영향 주는 다른 잡 N개" 1줄

## 거절 패턴 (plan 작성 금지)
- 자동 설치/설정 변경 명령을 plan에서 직접 실행 (Phase 1)
- 광범위 권한 요구 도구 (전체 파일시스템, 네트워크 무제한)
- 출처 검증 안 된 패키지 (GitHub stars 미명시 등)

## 알림 채널
- 결과는 Kanban task comment + `runtime/install_plan_*.md`
- 별도 Discord 알림은 Phase 1에서는 보내지 않음 (advisor_ops가 이미 송신)
