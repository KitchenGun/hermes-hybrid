---
name: job_factory_improvement
agent_handle: "@job_factory_tuner"
category: infrastructure
role: job_tuning
description: config/job_factory.yaml 정책 (threshold / cloud_allowed / claude_allowed) 데이터 기반 tuning.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "ExperienceLog 통계로 quality_threshold 조정"
  - "특정 job_type cloud_allowed 토글"
not_for:
  - "score_matrix 직접 편집 (bench harness 사용)"
  - "신규 job_type 추가 (→ @architect + 사용자 결정)"
inputs:
  - "최근 N일 ExperienceLog stats"
  - "tuning 대상 job_type"
outputs:
  - "config/job_factory.yaml diff 제안 + 근거 + risk + rollback"
metadata:
  hermes:
    primary_tools: [yaml]
    tags: [job_factory, tuning, infrastructure]
---

# Skill — job_factory_improvement

## Purpose
ExperienceLog 통계 기반 `config/job_factory.yaml` 의 boolean policy / threshold tuning.

## When to Use
- 특정 job_type 의 quality_threshold 가 너무 높음/낮음 시그널
- cloud_allowed 토글 후보 발생

## Inputs
- 최근 N일 ExperienceLog (CuratorJob `handled_by_stats.json`)
- tuning 대상 job_type 이름

## Procedure
1. handled_by_stats.json + by_tool fail rate 읽기.
2. 대상 job_type 의 success / failure / latency 분포 분석.
3. threshold 변경 권장값 제안 + 변경 risk 계산.
4. yaml diff 형태로 출력.

## Output Format
```yaml
# Before
- name: code_review
  quality_threshold: 70
# After
- name: code_review
  quality_threshold: 75   # +5 (failure_rate 12% → 8% expected)
```

## Safety / Constraints
- 변경 전 후 expected delta + rollback procedure 명시.
- score_matrix.json 은 직접 수정 금지 (bench harness 사용).

## Example Prompt
"code_review job_type 최근 한달 통계 기반으로 threshold 조정 제안"

## Existing Implementation
부분:
- `src/jobs/` (CuratorJob), `config/job_factory.yaml`, `src/jobs/skill_promoter.py` (auto-skill draft)
- 새 layer: 정책 yaml 자체 tuning 제안.
