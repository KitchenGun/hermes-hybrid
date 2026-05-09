# Hermes Growth-Agent Operations

운영 가이드. Plan 전문(`refactored-inventing-pony.md`) 보존, 본 문서는 **현재 운영 상태 + 체크리스트 + 명령어 모음**만 담는다.

> 참조: [README §Hermes Growth-Agent Status](../README.md), `docs/apply_plan.generated.md`, `docs/hermes_agent_gap_analysis.generated.md`.

---

## 1. Day-0 현재 상태 (Phase 22)

### Git
- `origin/main` HEAD: **post-PR#2 main** (Memory v4.2 rebase merged 2026-05-09)
- P0a/P0b/P0c/P0e baseline + P1 fix(`12a87c2`) + Memory v4.2 (PR #2, P0-A → P3 + Claude import) 모두 main 반영 완료
- 추적: PR #1 (Growth-Agent Migration), PR #2 (Memory v4.2)

### Memory (post-v4.2 two-layer)

**Layer 1 — legacy A/B (default ON, Phase 21 unchanged)**
- **Recall 코퍼스**: `data/state.db` (settings.state_db_path 기본값) `memos` 테이블
- **Schema**: `id, user_id, text, created_at, source` (P0c.2.0이 source 컬럼 ALTER 추가)
- **현재 row 분포** (사용자 직접 확인):
  - `generated_candidates` 28 rows (P0c.2 W1 ingest, `memory/memory_candidates.generated.yaml` 출처)
  - `user_feedback_style` 5 rows (사용자 직접 ingest)
  - 향후 `claude_import_*` (P4 auto-ingest 시점)
- 봇 wiring: `src/gateway/discord_bot.py:64` `SqliteMemory(settings.state_db_path)` → `Orchestrator(memory=...)` → `HermesMasterOrchestrator(memory=...)` 명시 주입
- 매 master.start마다 `_maybe_inject_memory(user_id, message, history, *, anchor_message=None)` Layer 1 호출 (default ON, top-k=3)

**Layer 2 — P2 retrieval supplement (default OFF)**
- gating: `memory_retrieval_enabled=False` (`src/config.py:225`)
- 코퍼스: `data/processed_memory/*.md` (sanitized derivatives) + `data/memory/{USER,MEMORY}.md` (compile 결과)
- ab key: `memory_retrieval_v1` (Phase 21 `memory_inject`와 **독립** A/B)
- enabled 시 `MemoryInjectionService.compose(query=memory_query, ab_arm="treatment", include_compiled=False)` 호출
- `_compose_prompt`가 `read_prompt_prepend()` 통해 USER.md/MEMORY.md 항상 prepend (이건 Layer 2 flag와 독립적으로 동작)

**memory_query 결정 (Phase 24 + v4.2 통합)**
- `_maybe_inject_memory` 함수 초반에 `memory_query = anchor_message if anchor_message else user_message`
- Layer 1, Layer 2 모두 동일 query 사용 — anchor recovery 일관성

### Memory v4.2 — track 정책 (.gitignore 강제)

| Path | track 여부 | 비고 |
|---|---|---|
| `data/state.db` | ❌ ignored (`data/*.db`) | 봇 SqliteMemory recall 코퍼스 (Layer 1) |
| `data/processed_memory/*.md` | ✅ tracked | sanitized derivatives, **private repo only** (현재 KitchenGun/hermes-hybrid PRIVATE) |
| `data/memory/{USER,MEMORY}.md` | ✅ tracked | `compile_split_memory` 결과 |
| `data/memory/{USER,MEMORY}.manifest.json` | ✅ tracked | manifest sidecar |
| `data/memory/.gitkeep` | ✅ tracked | placeholder |
| `data/memory/memos.db` | ❌ ignored (`data/memory/**` re-ignore + 명시 부재) | runtime DB |
| `data/external_memory/snapshots/**` | ❌ ignored | raw 원문, **never tracked** |
| `data/external_memory/examples/**` | ✅ tracked | 합성 fixture만 |
| `data/source_manifests/*.jsonl` | ✅ tracked | manifest-only (`record_type=schema`/`manifest`, no raw payloads) |
| `data/ingest_staging/` | ❌ ignored | 임시 import staging |
| `data/*.jsonl`, `data/*.log`, `data/*.tmp`, `data/*.yaml`, `data/*.sqlite*` | ❌ ignored | runtime artifacts (W10 detector log, growth_metrics 등) |

### Skills
- `agents/{research,planning,implementation,quality,documentation,infrastructure}/{name}/SKILL.md` 27개 (17 baseline + 10 P0c.4 promoted, score ≥0.85)
- Auto-install 경로 wiring 완료, **`auto_install=False` 유지** (P1 toggle 미적용 → draft/PR flow)
- Self-modify draft 소스 3종: W9 (Sat 23:00 점수 하락 SKILL.md 보강), W10 (real-time recurring detector → 30분 drainer), W11 (Sun 21:00 W7 → `skills/generated_from_self_review/`)

### Cron — Windows schtasks 9/9 active

| TaskName | Schedule (KST) | Script | Loop |
|---|---|---|---|
| HermesSelfReview | Sun 21:00 | `scripts/migration_self_review.py` | 4 (W7) |
| HermesReflection | Sun 22:00 | `scripts/reflection_job.py` | 4 (existing) |
| HermesABReport | Sun 22:30 | `scripts/ab_report_job.py` | A/B (Phase 21) |
| HermesCurator | Sun 23:00 | `scripts/curator_job.py` | 1+4 (W11_curator) |
| HermesPromoter | Sun 23:30 | `scripts/curator_job.py --skill-promote` | 2+4 (W11_promoter) |
| HermesSkillSelfModify | Sat 23:00 | `scripts/skill_self_modify.py` | 2 (W9) |
| HermesDialectic | Mon 06:00 | `scripts/dialectic_user_modeling.py --apply` | 3 (W8) |
| HermesDelegationPattern | Mon 12:00 | `scripts/delegation_pattern_extractor.py --apply` | 5 (W12) |
| HermesSkillDraftQueueDrainer | every 30 min | `scripts/process_skill_draft_queue.py --apply` | 2 (W10) |

### Marker blocks (10 + 1 yaml)
- `src/orchestrator/hermes_master.py` ×3 (W4, W10, W12)
- `src/mcp/server.py` ×2 (W6a, W6b)
- `src/jobs/curator_job.py` ×1 (W11_curator)
- `src/jobs/skill_promoter.py` ×1 (W11_promoter)
- `src/cli/timer_handlers/{windows,linux,darwin}.py` ×1 each (W3)
- `config/job_factory.yaml` ×1 yaml block (`# --- generated job candidates ---`)
- 모두 `HERMES_DISABLE_GROWTH_BLOCKS=true` env 즉시 short-circuit

### v4.2 wrapper note (marker block과 별개)

`_maybe_inject_memory()` 함수는 **two-layer wrapper** 구조 (Memory v4.2):
- Layer 1 — Phase 21 legacy A/B sqlite search (default ON, `memory_inject_enabled` gated)
- Layer 2 — P2 retrieval supplement (default OFF, `memory_retrieval_enabled` gated)
- 두 layer 모두 `memory_query` 사용 (Phase 24 anchor recovery 일관성)
- W4/W10/W12 marker block과 **독립** — `HERMES_DISABLE_GROWTH_BLOCKS`는 wrapper 자체엔 영향 없음. 단, W4 SOUL injection / W10 detector / W12 suggestion log는 같은 파일의 다른 함수에 있어 함께 short-circuit됨.

### Toggle 정책

| Toggle | 현재 | 검토 시점 |
|---|---|---|
| `skill_promoter_auto_install` | **OFF** | D+7~D+14 draft 품질 + revert 빈도 관찰 후 |
| `skill_hot_reload_enabled` | **OFF** | 봇 재시작 흐름 유지 |
| `feedback_keyword_match_enabled` | **OFF** | FP rate 30일 모니터링 후 |
| `memory_retrieval_enabled` | **OFF** | `data/processed_memory/` ≥1 entry + USER.md/MEMORY.md compile 완료 + 1주일 retrieval hit 통계 후 (§7) |
| W12 active dispatch biasing | **P2 (보류)** | `TaskState.suggested_handles` 추가 + dispatch chain audit 필요 |

**Cron timers 영향**: v4.2는 **cron job 추가 없음**. 9 timer 그대로 active. import는 사용자 manual 호출 (§4b) 또는 P4 auto-ingest hook (미배선).

---

## 2. 체크리스트 — W9 / W7 / W8 / W12

### W9 SkillSelfModify — 토요일 23:00 KST

목표: 점수 하락 SKILL.md를 자동으로 `SKILL.draft.md`로 보강안 작성.

**확인**:
1. `agents/**/SKILL.draft.md` 새 파일 생성 여부
2. draft에 `## Auto-modified note (<date>)` 블록 + `recent avg score` / `30d baseline avg` 기록
3. bot_stdout.log에 `skill_self_modify.draft` 정보 라인

**조건 미충족 시 noop 정상**: `--min-recent 5` (5회 이상 호출) + `--decline-delta 0.15` (recent avg가 baseline avg보다 0.15 이상 낮음). 아무 SKILL.md도 그 조건 미충족이면 "no modification needed" 출력.

**dry-run 단독 검증**:
```powershell
cd E:\hermes-hybrid
python scripts\skill_self_modify.py --dry-run
```

### W7 SelfReview — 일요일 21:00 KST

목표: growth_metrics 비교 + 3종 candidate artifact emit (memory/skills/jobs).

**확인**:
1. `data/self_review_<date>.md` (human-readable action list)
2. `memory/candidates_from_self_review_<date>.yaml` (memory candidate)
3. `skills/generated_from_self_review/<date>/*.md` (skill draft)
4. `jobs/candidates_from_self_review_<date>.yaml` (job candidate, P1 — auto-register 안 함)

**dry-run 단독 검증**:
```powershell
cd E:\hermes-hybrid
python scripts\migration_self_review.py --dry-run --window-days 7
```

### W8 Dialectic — 월요일 06:00 KST

목표: `profiles/user_profile.generated.md`의 claim별로 confirmed/weakened/new/retired 분류.

**확인**:
1. `data/user_profile_drift_<date>.yaml` 생성
2. yaml 구조: `confirmed_claims`, `weakened_claims`, `new_claims`, `retired_claims`
3. 각 claim에 `evidence_rows` 카운트 + transcript ts 인용

**dry-run 단독 검증**:
```powershell
cd E:\hermes-hybrid
python scripts\dialectic_user_modeling.py --dry-run --window 7d
```

### W12 DelegationPattern — 월요일 12:00 KST

목표: ExperienceRecord 중 `agent_handles` non-empty rows를 intent_cluster별 best/weak combo로 분류.

**확인**:
1. `data/delegation_patterns_<date>.yaml` 생성
2. yaml 구조: `clusters: [{intent_cluster, best_combos, weak_combos, total_samples}]`
3. multi-agent dispatch가 적었던 주에는 `insufficient data: 0 multi-agent rows` — 이는 정상 신호 (Loop 5는 사용 패턴 누적 필요)

**dry-run 단독 검증**:
```powershell
cd E:\hermes-hybrid
python scripts\delegation_pattern_extractor.py --dry-run --window 30d
```

**중요**: W12 marker block (`src/orchestrator/hermes_master.py:349`)은 P0에서 **observation-only**. `bot_stdout.log`에 `w12.delegation_suggestion` JSON 이벤트만 기록하고 `task` mutate 안 함. Active dispatch biasing은 P2.

---

## 3. PowerShell 확인 명령 모음

### 3.1 봇 프로세스 상태
```powershell
Get-Process -Name python | ForEach-Object {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)"
  [PSCustomObject]@{
    Id          = $_.Id
    StartTime   = $_.StartTime
    CommandLine = $p.CommandLine
  }
} | Where-Object { $_.CommandLine -like '*run_bot.py*' } | Format-List
```

### 3.2 Memory DB 상태
```powershell
cd E:\hermes-hybrid
python -c "import sqlite3; con=sqlite3.connect(r'data\state.db'); print(con.execute('SELECT source, COUNT(*) FROM memos GROUP BY source').fetchall())"

# schema 확인
python -c "import sqlite3; con=sqlite3.connect(r'data\state.db'); print([c[1] for c in con.execute('PRAGMA table_info(memos)')])"
```

### 3.3 schtasks 9/9 등록 확인 (한국어 로케일 grep 회피)
```powershell
$names = @('HermesReflection','HermesABReport','HermesCurator','HermesPromoter',
           'HermesSelfReview','HermesDialectic','HermesSkillSelfModify',
           'HermesDelegationPattern','HermesSkillDraftQueueDrainer')
$ok = 0
foreach ($n in $names) {
  cmd /c "schtasks /Query /TN $n >nul 2>nul"
  if ($LASTEXITCODE -eq 0) { Write-Output "  + $n"; $ok++ }
  else                     { Write-Output "  - $n NOT registered" }
}
Write-Output "registered: $ok / 9"
```

### 3.4 ExperienceRecord memory_inject_count 분포
```powershell
cd E:\hermes-hybrid
@'
import json, glob, os
from collections import Counter
files = sorted(glob.glob(r"logs\experience\*.jsonl"))
rows = []
for f in files:
    with open(f, encoding="utf-8", errors="replace") as fp:
        for line in fp:
            line = line.strip()
            if line:
                try: rows.append(json.loads(line))
                except: pass
recent = rows[-300:]
mic = Counter(r.get("memory_inject_count") for r in recent)
arm = Counter(r.get("experiment_arm") for r in recent)
nz  = [r for r in recent if isinstance(r.get("memory_inject_count"), int) and r.get("memory_inject_count") > 0]
print("recent_rows:", len(recent))
print("memory_inject_count:", dict(mic))
print("experiment_arm:", dict(arm))
print("nonzero_count:", len(nz))
for r in nz[-3:]:
    print(f"  ts={r.get('ts')} mic={r.get('memory_inject_count')} arm={r.get('experiment_arm')!r} hb={r.get('handled_by')!r}")
'@ | python -
```

### 3.5 master.memory_injected 로그 grep
```powershell
Select-String -Path E:\hermes-hybrid\logs\bot*.log -Pattern 'master\.memory_injected|master\.memory_search_failed' |
  Select-Object -Last 20
```

### 3.6 W10 recurring_request_log 상태
```powershell
$p = 'E:\hermes-hybrid\data\recurring_request_log.jsonl'
if (Test-Path $p) {
  python -c "import json; rows=[json.loads(l) for l in open(r'$p',encoding='utf-8') if l.strip()]; print('rows:',len(rows)); print('first_ts:',rows[0].get('ts') if rows else None); print('latest_ts:',rows[-1].get('ts') if rows else None); print('unique_hashes:',len(set(r.get('text_hash','') for r in rows)))"
}
```

### 3.7 W12 delegation_suggestion 로그 grep
```powershell
Select-String -Path E:\hermes-hybrid\logs\bot*.log -Pattern 'w12\.delegation_suggestion' |
  Select-Object -Last 20
```

### 3.8 Skill draft 누적 상태
```powershell
$auto = 'E:\hermes-hybrid\logs\curator\auto_skills'
$sr   = 'E:\hermes-hybrid\skills\generated_from_self_review'
if (Test-Path $auto) { Get-ChildItem $auto -Filter '*.md' | Measure-Object | ForEach-Object { Write-Output "auto_skills drafts: $($_.Count)" } }
if (Test-Path $sr)   { Get-ChildItem $sr   -Recurse -Filter '*.md' | Measure-Object | ForEach-Object { Write-Output "self_review drafts: $($_.Count)" } }
```

---

## 4. SelfReview 이후 memory candidates 수동 ingest 절차

W7가 일요일 21:00 KST에 emit하는 `memory/candidates_from_self_review_<date>.yaml`은 **자동으로 ingest되지 않는다**. CuratorJob의 W11_curator marker block(`src/jobs/curator_job.py:380`)은 read-only marker(`pass`-only) — 실제 ingest는 사용자 또는 별도 cron이 호출해야 한다.

**P1 fix(`12a87c2`) 이후** `scripts/ingest_memory_candidates.py` default DB가 `data/state.db`로 정렬됐으므로 `--db` 명시 불필요.

### 4.1 dry-run으로 schema/내용 확인
```powershell
cd E:\hermes-hybrid
$date = Get-Date -Format 'yyyy-MM-dd'
$yaml = "memory\candidates_from_self_review_$date.yaml"
if (Test-Path $yaml) {
  python scripts\ingest_memory_candidates.py --dry-run --yaml $yaml --user-id 100816750945255424
}
```

### 4.2 apply
```powershell
cd E:\hermes-hybrid
$date   = Get-Date -Format 'yyyy-MM-dd'
$yaml   = "memory\candidates_from_self_review_$date.yaml"
$source = "self_review_$date"
python scripts\ingest_memory_candidates.py --apply --yaml $yaml --user-id 100816750945255424 --source $source
```

### 4.3 검증
```powershell
python -c "import sqlite3; con=sqlite3.connect(r'data\state.db'); print(con.execute('SELECT source, COUNT(*) FROM memos GROUP BY source ORDER BY source').fetchall())"
```

기대: `generated_candidates`, `user_feedback_style`, `self_review_<date>` 분포 확인.

### 4.4 봇 재시작 불필요
`SqliteMemory.search()`는 매 호출 시 `aiosqlite.connect(db_path)` 새 connection 사용 (`src/memory/sqlite.py:53/74/96/141`) → 다음 master.start부터 즉시 새 rows recall.

---

## 4b. v4.2 compile_split_memory 운영 절차

W7 SelfReview와 무관한 **별도 import + compile 흐름**. v4.2가 도입한 사용자 자동 메모리 (Claude / ChatGPT / Discord) → `data/processed_memory/` → `data/memory/{USER,MEMORY}.md`.

### 4b.1 Claude Code 자동 메모리 import (dry-run)

```powershell
cd E:\hermes-hybrid
python scripts\import_claude_memory.py --dry-run
```

기대 출력:
- Source files read (e.g. 5 = MEMORY.md index + 4 detail files)
- Candidates extracted (frontmatter fallback 포함)
- PII / security / conflicts / needs_review 카운트 (자동 sanitize 결과)
- Snapshot would be copied to `data/external_memory/snapshots/<ts>/` (gitignored)

### 4b.2 apply

```powershell
python scripts\import_claude_memory.py --apply
```

결과:
- `data/processed_memory/*.md` 8개 sanitized derivative 생성/갱신
- `data/external_memory/snapshots/<ts>/` 원본 복사 (track되지 않음)
- `data/source_manifests/claude.jsonl`에 manifest record append (record_type=manifest, retention=manifest_only, no raw payloads)

### 4b.3 compile (USER.md + MEMORY.md)

```powershell
python -c "from src.core.memory_curator import MemoryCurator; from pathlib import Path; MemoryCurator(adapter=None, memory_root=Path('data/memory'), experience_log_root=Path('logs/experience'), enabled=True).compile_split_memory(processed_memory_root=Path('data/processed_memory'))"
```

결과:
- `data/memory/USER.md` (`user_profile.md` + `response_style.md` 머지, token budget 2000)
- `data/memory/MEMORY.md` (나머지 5종 머지)
- `data/memory/{USER,MEMORY}.manifest.json` (sidecar, source_hashes 포함)
- `_compose_prompt.read_prompt_prepend()`가 다음 master.start부터 자동 prepend

### 4b.4 ChatGPT / Discord import (선택)

```powershell
python scripts\ingest_conversations.py --source chatgpt --apply
python scripts\ingest_conversations.py --source discord --apply
```

각 source별로 별도 sanitizer + manifest 흐름 실행. `data/external_memory/<source>/<file>`로 raw 복사 (gitignored).

### 4b.5 검증

```powershell
# processed_memory 갱신 확인
Get-ChildItem E:\hermes-hybrid\data\processed_memory -Filter *.md | Select-Object Name, LastWriteTime, Length

# USER.md / MEMORY.md token budget + source_hashes
Get-Content E:\hermes-hybrid\data\memory\USER.manifest.json | ConvertFrom-Json | Select-Object profile, token_budget, source_hashes
```

### 4b.6 봇 재시작 불필요

`MemoryCurator.read_prompt_prepend()`는 매 `_compose_prompt()` 호출 시 파일 mtime 기반 lazy load → 다음 master.start부터 새 USER.md/MEMORY.md 즉시 반영.

### 4b.7 Layer 2 retrieval supplement 활성화 (선택)

기본 OFF. 활성화 시:
```powershell
[Environment]::SetEnvironmentVariable('HERMES_MEMORY_RETRIEVAL_ENABLED','true','User')
# 봇 재시작 필요 (settings는 startup 시 load)
```

활성 후 `master.retrieval_injected` 로그 발생 (Layer 1 `master.memory_injected`와 독립). 통계 모니터링은 §7 참조.

---

## 5. D+7 / D+14 / D+30 검증 기준

기준일: P0 main merge 시점 = 2026-05-08. 따라서 **D+7 = 2026-05-15, D+14 = 2026-05-22, D+30 = 2026-06-07**.

### Day 7 — 첫 일요일/월요일 cron 사이클 완료 후

- **J1 Loop 1 — Memory grew from real gaps**
  ```powershell
  python -c "import sqlite3; con=sqlite3.connect(r'data\state.db'); print(con.execute(\"SELECT source, COUNT(*) FROM memos WHERE source LIKE 'self_review_%' GROUP BY source\").fetchall())"
  ```
  기대: `self_review_2026-05-10` 등 1건 이상.

- **J2 Loop 3 — Dialectic emitted drift yaml**
  ```powershell
  Get-ChildItem E:\hermes-hybrid\data\user_profile_drift_*.yaml | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  ```
  기대: 가장 최근 월요일 06:00 yaml 존재 + `confirmed_claims` ≥1 + `new_claims` ≥1.

- **J3 Loop 5 — DelegationPattern emitted**
  ```powershell
  Get-ChildItem E:\hermes-hybrid\data\delegation_patterns_*.yaml | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  ```
  기대: 가장 최근 월요일 12:00 yaml 존재 + cluster ≥1 (또는 graceful "insufficient data" 명시).

### Day 14 — 두 번째 사이클

- **J4 Loop 2 — Skill self-modified**
  ```powershell
  Select-String -Path 'E:\hermes-hybrid\agents\**\SKILL.md' -Pattern 'Auto-modified note' |
    Select-Object -ExpandProperty Filename -Unique
  ```
  기대: 1개 이상 SKILL.md에 W9 자동 보강 audit 라인.

- **J5 Loop 1+4 — should_store flip 발생**
  W7가 두 번 실행되면 negative-feedback memo의 `should_store: false` 제안이 새 candidates yaml에 반영. 사용자 review 후 ingest 시 그 entry 제거.

- **J6 Loop 4 — gap count decreasing**
  ```powershell
  Get-ChildItem E:\hermes-hybrid\data\self_review_*.md | Sort-Object LastWriteTime |
    ForEach-Object { Write-Output $_.Name; Select-String -Path $_.FullName -Pattern 'Recurring patterns' -Context 0,2 }
  ```
  기대: 2번째 review의 recurring patterns count가 1번째보다 감소.

### Day 30 — 전체 baseline 비교

- **J7 Growth metrics delta**
  현재는 `scripts/compare_growth_metrics.py`가 별도 작성 안 됨 — 수동 비교:
  ```powershell
  python scripts\capture_growth_metrics.py --output data\growth_metrics_d30.yaml
  python -c "import yaml; b=yaml.safe_load(open(r'data\growth_metrics.generated.yaml')); c=yaml.safe_load(open(r'data\growth_metrics_d30.yaml')); print('Δ skill:', c['skill_count']-b['skill_count']); print('Δ memos:', c['memory_count']['memos_db_rows']-b['memory_count']['memos_db_rows']); print('Δ records:', c['record_count']-b['record_count'])"
  ```
  기대: 6 차원(skill/memos/jobs/prompt_pattern/ab_stats/experience) 중 ≥3에서 양의 증가.

- **J8 MCP usage**: `logs/experience/*.jsonl` 중 `handled_by='mcp_external'` rows 카운트. 0이면 외부 MCP 클라이언트 통합이 다음 작업.

- **J9 Cross-loop integration**: 단일 ExperienceRecord row에서 `memory_inject_count > 0` AND `agent_handles` non-empty AND `skill_ids` non-empty + 사용자 reaction이 positive 인 케이스 1건 이상.

### J-checkpoint 실패 시 대응 (수정 안 함, 원인만 보고)

- **D+7 J1-J3 fail**: cron 실제 firing 점검 — `schtasks /Query /TN <name>` 의 `Last Run Time` 확인
- **D+14 J4-J6 fail**: 사용 패턴 자체가 trigger 안 됐을 가능성 — wiring 결함이 아니라 "growth pressure absent"
- **D+30 J7-J9 fail**: threshold 튜닝 또는 multi-agent dispatch 사용량 부족 — 코드 변경 필요한 결정점

---

## 6. Emergency Disable

### 즉시 회피 (모든 W marker block short-circuit)
```powershell
# User-level env 설정 (시스템 재시작 후에도 유지)
setx HERMES_DISABLE_GROWTH_BLOCKS true

# 봇 재시작 (PID는 매 부팅마다 다름 — Get-Process로 찾아서 stop)
Get-Process -Name python | ForEach-Object {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)"
  if ($p.CommandLine -like '*run_bot.py*') { Stop-Process -Id $_.Id -Force }
}
Start-Process powershell -ArgumentList '-NoProfile','-File','E:\hermes-hybrid\start.ps1' `
  -WorkingDirectory 'E:\hermes-hybrid' -WindowStyle Hidden
```

이후 봇 동작:
- W4 SOUL injection — skip
- W6a/W6b MCP extensions — skip (1-tool fallback)
- W10 recurring detector — skip
- W11 curator/promoter scan — skip
- W12 delegation suggestion — skip

### 영구 해제 (env 제거)
```powershell
[Environment]::SetEnvironmentVariable('HERMES_DISABLE_GROWTH_BLOCKS', $null, 'User')
# 또는
Remove-Item Env:\HERMES_DISABLE_GROWTH_BLOCKS
# 봇 재시작 필요
```

### Layer 2 P2 retrieval supplement만 즉시 OFF (W marker block과 독립)
```powershell
# default가 false라 평소엔 미설정 상태로 충분.
# 사용자가 명시적으로 활성화한 후 빠르게 OFF하려면:
[Environment]::SetEnvironmentVariable('HERMES_MEMORY_RETRIEVAL_ENABLED','false','User')
# 봇 재시작 후 effective. Layer 1 (Phase 21 legacy)은 영향 없음.
```

### 부분 rollback (특정 marker만 제거)
```bash
bash scripts/rollback_marked_blocks.sh --names W4,W10,W12
# 또는 단일
bash scripts/rollback_marked_blocks.sh --names W12
```

### 데이터 rollback
```powershell
# generated_candidates 28 rows 제거
python -c "import sqlite3; con=sqlite3.connect(r'data\state.db'); con.execute(\"DELETE FROM memos WHERE source='generated_candidates'\"); con.commit(); print('deleted')"

# self_review_* rows 제거 (날짜 패턴)
python -c "import sqlite3; con=sqlite3.connect(r'data\state.db'); con.execute(\"DELETE FROM memos WHERE source LIKE 'self_review_%'\"); con.commit(); print('deleted')"
```

### Windows timer 9개 모두 제거
```powershell
$names = @('HermesSelfReview','HermesDialectic','HermesSkillSelfModify',
           'HermesDelegationPattern','HermesSkillDraftQueueDrainer',
           'HermesReflection','HermesABReport','HermesCurator','HermesPromoter')
foreach ($n in $names) { schtasks /Delete /TN $n /F }
```

---

## 7. P1 toggle 검토 시점 (선반영 금지)

각 toggle을 켜기 전에 확인할 사실:

### `skill_promoter_auto_install=true` — D+7~D+14 검토
- `logs/curator/auto_skills/` 누적 draft 수 + 평균 score
- `data/skill_promotion.log` 의 score 분포 (≥0.85 / 0.5-0.85 / <0.5 비율)
- W9/W10/W11 출처별 draft 품질 차이
- **flip 조건**: 평균 score ≥0.85 + score <0.5 비율 ≤30% + W9 revert 건수 ≤W9 install 건수
- **flip 위치**: `src/jobs/skill_promoter.py:86` constructor default 또는 호출부 명시

### `skill_hot_reload_enabled=true` — 봇 재시작 빈도 부담될 때
- 효과: AgentRegistry가 30s polling으로 SKILL.md 자동 reload
- 폴링 overhead: 27 SKILL.md mtime check 30초마다 — 무시 가능
- **flip 위치**: `.env` 또는 `src/config.py` 기본값

### `feedback_keyword_match_enabled=true` — 30일 모니터링 후
- 효과: Phase 20 reaction-based feedback에 keyword matching FP 추가
- FP rate가 검증되지 않음
- 30일 reaction 수가 50건 이상 누적된 후 검토

### `memory_retrieval_enabled=true` — Layer 2 P2 retrieval supplement
- 효과: `_maybe_inject_memory` Layer 2가 활성화. `MemoryInjectionService`가 `data/processed_memory/` + `data/memory/{USER,MEMORY}.md`에서 query 매칭 → history_window prepend
- ab key: `memory_retrieval_v1` (Phase 21 `memory_inject` A/B와 **독립**)
- **선결조건**:
  - `data/processed_memory/`에 1개 이상 entry (`scripts/import_claude_memory.py --apply` 1회 이상 실행)
  - `compile_split_memory` 실행으로 `data/memory/USER.md` + `MEMORY.md` 생성
- **flip 기준**:
  - 1주일 동안 `master.retrieval_failed` 로그 0건
  - Layer 2 활성 시 `master.retrieval_injected` 로그 통계: hits > 0 비율 ≥30% (낮으면 retriever_k / token_budget 튜닝 검토)
  - Phase 21 Layer 1 `master.memory_injected` 통계가 baseline 대비 변화 없음 (independence 확인)
- **flip 위치**:
  - 즉시 활성: `[Environment]::SetEnvironmentVariable('HERMES_MEMORY_RETRIEVAL_ENABLED','true','User')` + 봇 재시작
  - 영구 default 변경: `src/config.py:225` `memory_retrieval_enabled: bool = True` (commit 필요)
- **관찰 명령**:
  ```powershell
  Select-String -Path E:\hermes-hybrid\logs\bot*.log -Pattern 'master\.retrieval_injected|master\.retrieval_failed' | Select-Object -Last 30
  ```

### W12 active dispatch biasing — P2 (코드 변경 필요)
- 1단계: `src/state/task_state.py`에 `task.suggested_handles: list[str] = Field(default_factory=list)` 추가 (1줄)
- 2단계: `_dispatch_master()`에서 `task.agent_handles` 비어있을 때 `task.suggested_handles` 채택
- 3단계: W12 marker block을 log-only → mutating으로 전환
- **선결조건**: dispatch chain audit (`agent_handles` always-from-mention 가정 검증)

---

## 8. Plan / docs 참조 위치

- 전체 plan: `C:\Users\kang9\.claude\plans\refactored-inventing-pony.md` (973 lines, P0 모든 단계 + J 검증)
- gap analysis: `docs/hermes_agent_gap_analysis.generated.md`
- apply plan: `docs/apply_plan.generated.md` (P1/P2 결정 추적)
- mcp capabilities: `docs/mcp_capabilities.generated.md` (17 growth-action endpoints)
- 본 문서: `docs/growth_agent_ops.md` (운영 체크리스트)

문제 발견 시 절차:
1. 본 문서 §3 명령으로 사실 확인 (read-only)
2. §6 emergency disable 또는 §6 부분 rollback 검토
3. 코드/설정 수정은 사용자 명시 결정 후
