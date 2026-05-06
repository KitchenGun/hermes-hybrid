# USER — advisor_ops가 알아야 할 사용자 컨텍스트

이 파일은 advisor_ops가 추천을 만들 때 참고하는 사용자 프로필이다.
char_limit 1375 (Hermes 공식 한도). 사실만 적고 추측 금지.

## 직무 / 환경
- Unreal Engine / Unity 기반 게임 엔진 개발자
- 호스트: Windows 11 + WSL2. Ollama는 Windows 호스트에서 실행
- 레포: `hermes-hybrid` (git, 메인 브랜치 `main`)

## 사용 중 프로파일
- `kk_job`: 게임 프로그래머 채용 리서치 + 이력 관리
- `journal_ops`: Discord #일기 채널 → 24필드 활동 로그 → Google Sheets
- `calendar_ops`: 일정 관리
- `mail_ops`: 메일 처리

## 추천 우선순위 (사용자 선호)
1. **게임 개발 워크플로 도구**가 일반 개발 도구보다 우선
2. **품질·안전 > 비용 절감** — 저가 모델로 다운그레이드 추천은 명확한 근거 있을 때만
3. **로컬 우선 (Ollama)** — cloud는 정밀 분석에만
4. **MCP는 검증된 것만** — stars 100+ 또는 공식 maintainer만 신규 추천

## 거절 패턴 (추천 금지)
- 자동 설치/설정 변경을 요구하는 도구
- 광범위 권한(전체 파일시스템, 네트워크 무제한) 요구 도구
- 게임 실행 중 GPU·VRAM을 점유하는 백그라운드 도구
- 단순 GPU 휴리스틱(GPU 사용량으로 게임 감지) 기반 도구 — 에디터·빌드·플레이테스트도 GPU 사용함

## 알림 채널
- Discord webhook: `DISCORD_BRIEFING_WEBHOOK_URL`
- 주간 보고: 일요일 04:00 KST
- on_demand 결과: 호출자에게 즉시 응답 + Discord 요약
