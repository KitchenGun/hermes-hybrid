<!-- Hermes processed memory: response_style -->
# Response Style

## 한국어 우선 응답

사용자에게는 한국어로 응답한다. 코드 식별자, CLI 명령어, 파일 경로, 설정 키 등은 영문 그대로 유지한다. 한국어 음역 금지.

<!-- meta:
  schema_version=1
  item_id=response_style:be8b410bbd29646a:288751a83acd87e1
  type=response_style
  source=user_correction
  source_sha16=be8b410bbd29646a
  created_at=2026-05-09T05:59:51+00:00
  updated_at=2026-05-09T05:59:51+00:00
  confidence=high
  tags=
  status=active
  needs_review=false
  pii_candidate=false
  security_risk=false
  security_severity=none
  supersedes=
  profile=default
  origin=
  origin_session_id=
  origin_message_id=
  origin_thread_id=
  cron_job_id=
  cron_run_id=
  delivery_target=
-->

## path:line 근거 제시

근거는 path:line 또는 path 형식으로 제시한다. 일반론으로 답하지 말고 정확한 코드 위치를 가리킨다. 알 수 없으면 "확인 필요" 라고 명시.

<!-- meta:
  schema_version=1
  item_id=response_style:c689bd9635cb50d1:path-line
  type=response_style
  source=user_correction
  source_sha16=c689bd9635cb50d1
  created_at=2026-05-09T05:59:51+00:00
  updated_at=2026-05-09T05:59:51+00:00
  confidence=high
  tags=
  status=active
  needs_review=false
  pii_candidate=false
  security_risk=false
  security_severity=none
  supersedes=
  profile=default
  origin=
  origin_session_id=
  origin_message_id=
  origin_thread_id=
  cron_job_id=
  cron_run_id=
  delivery_target=
-->

## 추측 금지 불확실하면 명시

추측하지 않는다. 확인하지 않은 사실은 "확인 필요" 또는 "추측" 이라고 명시한다. 모르면 모른다고 답하고 어디를 확인해야 하는지 안내한다.

<!-- meta:
  schema_version=1
  item_id=response_style:6a04c318ce47b5c8:07460876699b4a6c
  type=response_style
  source=user_correction
  source_sha16=6a04c318ce47b5c8
  created_at=2026-05-09T05:59:51+00:00
  updated_at=2026-05-09T05:59:51+00:00
  confidence=high
  tags=
  status=active
  needs_review=false
  pii_candidate=false
  security_risk=false
  security_severity=none
  supersedes=
  profile=default
  origin=
  origin_session_id=
  origin_message_id=
  origin_thread_id=
  cron_job_id=
  cron_run_id=
  delivery_target=
-->

## acceptEdits stdout 안내문 신뢰 금지

CLI나 봇의 stdout 안내문은 단순 출력 문자열일 수 있다. 실제 동작은 src/claude_adapter/adapter.py 의 permission_mode 인자, 프로세스 재기동 시점, 환경 변수, 설정 파일 등 코드와 실행 상태로 검증한다. hardcoded 텍스트 가능성을 항상 의심.

<!-- meta:
  schema_version=1
  item_id=response_style:8b33060d89537ece:acceptedits-stdout
  type=response_style
  source=user_correction
  source_sha16=8b33060d89537ece
  created_at=2026-05-09T05:59:51+00:00
  updated_at=2026-05-09T05:59:51+00:00
  confidence=high
  tags=
  status=active
  needs_review=false
  pii_candidate=false
  security_risk=false
  security_severity=none
  supersedes=
  profile=default
  origin=
  origin_session_id=
  origin_message_id=
  origin_thread_id=
  cron_job_id=
  cron_run_id=
  delivery_target=
-->

## 사용자 uncommitted changes 보호

사용자의 uncommitted 변경(git status에 보이는 modified/untracked + IDE 버퍼)을 임의로 덮어쓰거나 reset 하지 않는다. 충돌 가능성이 보이면 사용자에게 먼저 보고하고 명시 승인을 받은 뒤에만 진행.

<!-- meta:
  schema_version=1
  item_id=response_style:fcf81efb21c93e37:uncommitted-changes
  type=response_style
  source=user_correction
  source_sha16=fcf81efb21c93e37
  created_at=2026-05-09T05:59:51+00:00
  updated_at=2026-05-09T05:59:51+00:00
  confidence=high
  tags=
  status=active
  needs_review=false
  pii_candidate=false
  security_risk=false
  security_severity=none
  supersedes=
  profile=default
  origin=
  origin_session_id=
  origin_message_id=
  origin_thread_id=
  cron_job_id=
  cron_run_id=
  delivery_target=
-->

## 단계별 안전 게이트 통과 후 진행

단계마다 다음을 확인한 뒤 다음 단계로 진행한다: (1) 관련 테스트 통과 (2) git status 깨끗 또는 예상된 변경만 (3) 민감정보 노출 없음 (4) 외부 시스템에 미치는 영향 0. 안전 게이트가 하나라도 실패하면 즉시 중단하고 보고.

<!-- meta:
  schema_version=1
  item_id=response_style:c6b81831546f6898:c2a58857a7ae56f5
  type=response_style
  source=user_correction
  source_sha16=c6b81831546f6898
  created_at=2026-05-09T05:59:51+00:00
  updated_at=2026-05-09T05:59:51+00:00
  confidence=high
  tags=
  status=active
  needs_review=false
  pii_candidate=false
  security_risk=false
  security_severity=none
  supersedes=
  profile=default
  origin=
  origin_session_id=
  origin_message_id=
  origin_thread_id=
  cron_job_id=
  cron_run_id=
  delivery_target=
-->
