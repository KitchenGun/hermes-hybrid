# Job Factory v2 — 점진 롤아웃 가이드

이 문서는 Phase 1~6에서 만든 empirical bandit dispatcher를 실제 운영에
투입하는 방법과 안전 장치를 정리합니다. **Phase 7 완료 시점**(이 PR)에
모든 코드는 들어가 있고 기본값으로 꺼져 있습니다 (`USE_NEW_JOB_FACTORY=false`).

---

## 0. 사전 점검

배포 전 확인:

```bash
# 1. 모든 단위 테스트 통과
python -m pytest tests/ \
  --ignore=tests/test_ollama_live.py \
  --ignore=tests/test_mcp_server.py -q

# 2. 설정 파일 4종 존재 + 로드 가능
ls config/job_factory.yaml config/model_registry.yaml config/cloud_policy.yaml config/benchmark_prompts.yaml
python -c "from src.job_factory.builder import build_job_factory_dispatcher; from src.config import get_settings; build_job_factory_dispatcher(get_settings())"

# 3. ScoreMatrix 디렉토리 쓰기 가능
ls -la data/job_factory/ data/benchmarks/
```

기대: 461+ tests passed (Phase 1~8 누적), 위 import 무에러.

---

## 1. 단계별 롤아웃

### 1단계: 운영자 1인 (1일)

```bash
# .env에 추가
USE_NEW_JOB_FACTORY=true
```

봇 재시작 후 본인 Discord 계정으로만 테스트:
- 일반 chat ("안녕") → simple_chat 분류 → ollama로 응답
- 코드 요청 ("Python으로 fizzbuzz") → code_generation → claude_cli (Max OAuth)
- 활동 기록 ("방금 운동했어") → schedule_logging → ollama (24-필드 JSON)
- 캘린더 키워드 ("내일 일정") → **CalendarSkill이 먼저 잡힘** (v2가 아닌 calendar_ops profile)

### 2단계: 백그라운드 벤치 (자동, 0일)

봇이 처음 v2 모드로 부팅하면 `BenchScheduler`가 미벤치 모델을 감지해서
백그라운드 bench를 트리거합니다. 콘솔 로그에서 다음을 확인:

```
[INFO] scheduler.startup.unbenched models=['qwen2.5:7b-instruct', ...]
[INFO] scheduler.bench_starting models=...
[INFO] scheduler.bench_done models=...
```

bench가 끝나면 `data/benchmarks/<timestamp>.json`에 결과가 생기고
`data/job_factory/score_matrix.json`이 채워집니다.

### 3단계: 라이브 트래픽 학습 (1주)

운영 중 매 응답마다 validator score가 매트릭에 누적됩니다. 1주 정도
지나면 (job_type, model) 매트릭이 안정됩니다. 다음 명령으로 모니터링:

```bash
# 매트릭 통계 확인 (간단 jq)
cat data/job_factory/score_matrix.json | python -m json.tool | head -50
```

체크포인트:
- ✅ 자주 쓰는 job_type의 cell이 n>=10에 도달
- ✅ 같은 job_type 내 mean score 차이가 모델별로 보임 (≥10점)
- ⚠️ 한 모델이 모든 job_type에서 0점 → adapter 문제 가능성, 로그 확인
- ⚠️ Claude 자동 호출 카운터가 매시간 cap에 닿음 → cap 늘리거나 cloud_allowed 검토

### 4단계: 전체 사용자 (cap 안정화 후, 1일)

`.env`에 그대로 `USE_NEW_JOB_FACTORY=true` 두고 모든 Discord allowlist
사용자에게 적용. 메트릭 모니터링은 다음 한 주 더.

### 5단계: legacy 코드 제거 (안정화 2주 후)

이 단계는 운영자 결정 사항이며, 자동화하지 않습니다. 제거 절차:

1. `src/orchestrator/orchestrator.py:_handle_locked` — v1 분기(`if self.settings.job_factory_enabled`)와 그 아래 Router/dispatch 부분 제거
2. `src/job_factory/factory.py` (v1) 삭제
3. `src/router/`, `src/validator/`, `src/orchestrator/bump.py` 등 v1 의존 코드 정리
4. Settings에서 v1 플래그 (`job_factory_enabled`, `allow_profile_creation`, 6개 phase 플래그) 제거
5. 관련 테스트 (`tests/test_orchestrator.py`, `tests/test_router.py` 등) 정리

⚠️ **레거시 제거 전 반드시**:
- `git tag pre-v2-cleanup` 으로 백업
- 1~2주 v2 운영하면서 사고 0건 확인
- 운영자가 명시적으로 결정

---

## 2. 롤백 절차

문제 발생 시 즉시:

```bash
# .env 한 줄만 변경
USE_NEW_JOB_FACTORY=false

# 봇 재시작
python scripts/run_bot.py
```

이 한 줄로 v1 path가 살아납니다. 코드 변경 / re-deploy 불필요.

좀 더 본격적인 롤백:

```bash
git revert <phase-7-merge-commit>   # 이 PR을 통째로 되돌림
```

ScoreMatrix는 `data/job_factory/`에 남아있어도 무해 (v1 모드에서 안 봄).
필요하면 `rm data/job_factory/score_matrix.json`으로 깨끗이.

---

## 3. 모니터링 — 체크해야 할 4가지 메트릭

### 3.1 매트릭 발효 정도 (`score_matrix.json` 셀 수)

```python
import json
m = json.load(open("data/job_factory/score_matrix.json"))
print(f"cells: {len(m['cells'])}")
# 정상: 1주 후 ≥ (job_type 수 × local 모델 수) ≈ 50개 이상
```

### 3.2 cloud 호출 빈도 (CloudPolicy stats)

봇 콘솔에 주기적으로 다음 같은 로그가 보여야 합니다:

```
INFO: dispatcher.cloud_denied matrix_key=openai/gpt-4o reason=...
```

이게 매분 떨어지면 `claude_auto_calls_per_hour` cap이 너무 낮을 수 있음.

### 3.3 Validator score 분포

`tests/test_jf_validator.py`의 케이스를 통과해야 하지만 운영에서:
- 평균 score가 50 이하 → validator 너무 엄격 (가중치 조정 필요)
- 평균 score가 90+ → validator 너무 관대 (모델 차이 안 드러남)
- 정상: 평균 65~85, 모델별 표준편차 10 이상

### 3.4 Latency

v1 평균 응답 latency vs v2:
- v1 (ollama L2 단일 호출): 2~4초
- v2 (classifier + selector + 1 attempt + validator): 3~6초
- v2 + cloud escalation: 5~12초

10초 넘으면:
1. classifier LLM (`qwen2.5:3b`)이 느린지 확인 — Ollama 모델이 메모리에서 unload 됐을 수도
2. validator의 LLM judge가 매 호출마다 GPT-4o-mini를 또 부르고 있는지 확인
3. cloud step이 자주 트리거되는지 — 3.2의 cloud_denied 로그 확인

---

## 4. 알려진 한계 (v1 → v2 이전 시 알아야 할 것)

| 항목 | v1 | v2 |
|------|----|----|
| 모델 선택 | 사람이 정한 tier 매핑 | 매트릭 + epsilon-greedy |
| Claude 자동 사용 | `!heavy`만 | 정책에 따라 자동 (cloud_allowed/claude_allowed boolean) |
| Validator | 단순 length/timeout 체크 | 4-axis composite (length + structural + JSON + LLM judge) |
| Cloud rate limit | hard-coded session/day 카운터 | 시간/일/USD cap (CloudPolicy) |
| Approval gate | 잡 YAML의 `requires_confirmation` | JobType.requires_user_approval + 비용 임계 |
| 메모리 사용량 | ~50MB | +~10MB (ScoreMatrix in-memory + adapter pool) |
| Cold-start 첫 응답 | 즉시 | bench 안 돌렸으면 round-robin → 약간 느림 |

---

## 5. 트러블슈팅 FAQ

### Q. v2 켜자마자 모든 응답이 "init failed"
→ `config/job_factory.yaml` 또는 `config/model_registry.yaml` 파일이 없거나 문법 오류. `pytest tests/test_jf_builder.py::test_build_dispatcher_end_to_end_with_real_configs` 실행해서 어떤 config가 깨졌는지 확인.

### Q. 모든 응답이 "사용 가능한 로컬 모델이 없습니다"
→ Ollama 서버 다운 또는 `model_registry.yaml`의 local 항목이 실제로 설치 안 됨. `ollama list`로 확인.

### Q. cloud 호출이 한 번도 안 일어남
→ `cloud_policy.yaml`의 cap이 0으로 설정됐을 수 있음. 또는 OpenAI key가 비어 있어서 builder가 OpenAI adapter를 등록하지 않았을 수 있음. 봇 시작 로그의 `jf.builder.openai_skipped` 확인.

### Q. needs_approval만 뜨고 실제 호출 안 됨
→ Phase 7 시점에 HITL Discord 버튼 통합은 미완. v2의 needs_approval은 일단 degraded 메시지로 fallback. 향후 phase에서 [기존 ConfirmView](../src/gateway/confirm_view.py)와 연동 예정.

### Q. 매트릭이 텅 비어있음
→ `data/job_factory/score_matrix.json`이 없거나 corrupt. `python scripts/bench_local_models.py`로 수동 bench 한 번 돌리면 채워짐. 또는 운영하면 자동으로 채워짐 (online learning).

---

## 6. 핵심 파일 목록 (참고용)

### 코어 (Phase 1~6)
- `src/job_factory/score_matrix.py` (Welford 누적)
- `src/job_factory/selector.py` (epsilon-greedy bandit)
- `src/job_factory/registry.py` (JobType + ModelRegistry)
- `src/job_factory/classifier.py` (메시지 → job_type)
- `src/job_factory/dispatcher.py` (메인 dispatch loop)
- `src/job_factory/runner.py` (action JSON → tool execution)
- `src/job_factory/validator.py` (4-axis composite)
- `src/job_factory/policy.py` (CloudPolicy)
- `src/job_factory/builder.py` (all-in-one assembly)

### 통합 (Phase 7)
- `src/config.py` — `use_new_job_factory` 플래그 추가
- `src/orchestrator/orchestrator.py:_handle_via_job_factory_v2`

### Bench (Phase 3~4)
- `src/job_factory/bench/` — 5종 scorers + runner + scheduler
- `scripts/bench_local_models.py` — CLI

### Configs
- `config/job_factory.yaml` — job_type 정의 (정책만)
- `config/model_registry.yaml` — 모델 메타
- `config/cloud_policy.yaml` — cap / approval thresholds
- `config/benchmark_prompts.yaml` — 측정 차원 + dimension→job_type 가중치
- `data/bench/*.yaml` — 차원별 prompt 셋

### 데이터 (gitignored, runtime)
- `data/job_factory/score_matrix.json`
- `data/benchmarks/<timestamp>.json`

---

## 7. 다음 단계 (legacy 제거 후 검토할 것들)

- **Cloud step도 EMA 적용** — 모델 업그레이드 시 옛 점수가 새 성능을 가리는 문제 (Code review 4.3)
- **System prompt per job_type** — 현재는 dispatcher 인스턴스 단위 (Code review 4.5)
- **Classifier 캐시** — 같은 사용자의 follow-up은 같은 job일 가능성 (Code review 4.4)
- **HITL Discord 버튼 통합** — `needs_approval` outcome을 기존 `ConfirmView`로 surface
- **UCB1 algorithm** — epsilon-greedy의 더 정교한 대체 (variance 활용)
- **Per-user score matrix** — 사용자별 선호 모델 학습 (현재는 전역)
