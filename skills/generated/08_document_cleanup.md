---
name: document_cleanup
agent_handle: "@document_cleaner"
category: documentation
role: doc_normalize
description: 잡 메모 / 회의록 / 코드 주석 → 정규화된 markdown / yaml / JSON.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "raw text → 구조화된 markdown 정리"
  - "긴 노트 → 헤더 / 리스트 정규화"
not_for:
  - "신규 문서 작성 (→ @documenter)"
  - "코드 주석 추가 (→ @commenter)"
inputs:
  - "raw 텍스트"
  - "타겟 형식 (markdown / yaml / JSON)"
outputs:
  - "정규화 결과 + 변경 요약"
metadata:
  hermes:
    primary_tools: []
    tags: [documentation, cleanup, normalize]
---

# Skill — document_cleanup

## Purpose
사용자가 자주 만드는 잡 메모 / 회의록 / 긴 plan draft 를 정규화된 형식으로 변환.

## When to Use
- 긴 plan / 회의록 / 잡 노트 주어졌을 때
- 한국어 + 영어 혼재 텍스트 정리

## Inputs
- raw text
- 타겟 형식 (markdown / yaml / JSON)

## Procedure
1. 헤더 식별 (h1/h2/h3).
2. 리스트 정규화 (bullet vs 번호).
3. 코드 블록 분리.
4. 한국어 + 영어 혼재 — code/identifiers 만 영문 보존.

## Output Format
- 변환된 최종 텍스트
- 변경 요약 (헤더 N개, 리스트 N개)

## Safety / Constraints
- 시크릿 / 토큰 인용 금지 — 발견 시 redact.

## Example Prompt
"이 잡 메모 정리해서 markdown 으로"

## Existing Implementation
부분: `agents/documentation/documenter/SKILL.md`, `agents/documentation/commenter/SKILL.md` — 이 generated skill 은 cleanup 만 담당.
