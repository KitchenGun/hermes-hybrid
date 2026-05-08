---
name: local_model_benchmark
agent_handle: "@local_model_bencher"
category: infrastructure
role: model_bench
description: 로컬 Ollama 모델 latency / quality / throughput 벤치 + 결과 yaml.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "신규 Ollama 모델 도입 전 벤치"
  - "월 1회 정기 성능 비교"
not_for:
  - "Claude / OpenAI 벤치 (→ @researcher)"
  - "production lane 변경 — 그건 사용자 결정"
inputs:
  - "모델 list"
  - "프롬프트 set (선택, default sample)"
outputs:
  - "latency / quality / throughput yaml + summary markdown"
metadata:
  hermes:
    primary_tools: [ollama]
    tags: [bench, ollama, local]
---

# Skill — local_model_benchmark

## Purpose
로컬 ollama 모델군에 대한 latency / quality / throughput 비교.

## When to Use
- 신규 모델 다운로드 후 벤치
- 월 1회 정기 재벤치 (job_local_model_rebench cron 후보)

## Inputs
- 모델 list (e.g. ["qwen3:4b", "llama3.2:3b"])
- 프롬프트 set (선택)

## Procedure
1. 각 모델 warmup (1 prompt).
2. 5 prompt × N runs → latency p50/p95.
3. quality — Critic.evaluate self_score.
4. throughput — tokens/sec.
5. data/bench/<date>.yaml 적재.

## Output Format
- yaml: 모델별 latency / quality / tokens_per_sec / sample_size
- markdown summary (top-3 ranked)

## Safety / Constraints
- production master lane 자동 변경 금지 — 사용자 결정.
- Ollama 미실행 시 "ollama not running" 명시.

## Example Prompt
"로컬 모델 벤치 한번 돌려"

## Existing Implementation
- `scripts/bench_local_models.py`, `scripts/list_ollama_models.py` — 이 skill 은 chat-time wrapper.
- `src/llm/adapters/ollama.py` — Ollama 어댑터 (있을 경우, NEEDS_REVIEW path 확인).
