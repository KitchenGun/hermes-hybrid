---
name: research_summary
agent_handle: "@research_summarizer"
category: research
role: external_summary
description: 여러 외부 출처를 한국어 요약 + 출처 인용 + 의견 분리로 정리.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "복수 URL/문서 입력 → 한국어 요약 필요"
  - "의견 vs 사실 분리 필요"
not_for:
  - "단일 URL fetch + 1-line 답 (→ @finder)"
  - "코드 작성 (→ @coder)"
inputs:
  - "출처 list (URL or path)"
  - "원하는 길이 / 톤"
outputs:
  - "요약 본문 + 출처 footnote + 미확정 NEEDS_REVIEW"
metadata:
  hermes:
    primary_tools: [web_search, web_fetch]
    tags: [research, summary, citation]
---

# Skill — research_summary

## Purpose
N개의 외부 출처를 한국어로 요약. 사실/의견 명확히 분리, 모든 주장에 출처 인용.

## When to Use
- "이거 여러 글 읽고 요약해" 류 요청
- 복수 docs/articles 비교

## Inputs
- 출처 URL/path list
- 길이 (short/medium/long)
- 한국어 vs 원문 인용 비율

## Procedure
1. 각 출처 fetch 또는 read.
2. 핵심 주장 / 데이터 / 의견 분리.
3. 한국어 요약 + 각 주장 옆 출처 footnote.
4. 출처 간 모순 발견 시 명시.

## Output Format
```
## 요약
... 본문 (footnote ¹ ² ³)

## 출처
1. <URL or path>
2. ...
```

## Safety / Constraints
- 출처 없는 주장 금지.
- 출처 모순 시 "출처 X와 Y의 주장이 다름" 명시.

## Example Prompt
"GAS 와 ECS 비교한 글 3개 요약해 (출처 첨부)"

## Existing Implementation
부분: `agents/research/researcher/SKILL.md`, `agents/research/analyst/SKILL.md` — 이 generated skill은 이 둘의 결합 + 한국어 요약 톤.
