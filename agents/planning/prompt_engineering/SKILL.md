---
name: prompt_engineering
agent_handle: "@prompt_engineer"
category: planning
role: prompt_design
description: 사용자 의도 → master / sub-agent 가 잘 따를 수 있는 프롬프트 구조 설계.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "복잡한 요청을 master / sub-agent 로 분해할 때"
  - "기존 SKILL.md 의 when_to_use / not_for 보강이 필요할 때"
  - "마이그레이션 plan 작성"
not_for:
  - "단순 1-shot 코드 생성 (→ @coder)"
  - "외부 사실 검색 (→ @researcher)"
inputs:
  - "사용자 raw 의도"
  - "타겟 sub-agent 또는 master"
outputs:
  - "구조화된 프롬프트 (헤더 + 번호 리스트 + 보고서 양식)"
  - "예상 실패 모드 + guardrail"
metadata:
  hermes:
    primary_tools: []
    tags: [prompt, planning, master, design]
---

# Skill — prompt_engineering

## Purpose
사용자 raw 요청 → master 또는 sub-agent 가 안정적으로 따를 수 있는 구조화된 프롬프트로 변환.

## When to Use
- 큰 마이그레이션 / wiring 작업 plan 작성 시
- @coder / @researcher / @reviewer 등 sub-agent 호출 직전 의도를 명확하게 만들고 싶을 때
- 기존 SKILL.md 의 when_to_use / not_for 보강 draft 가 필요할 때

## Inputs
- raw 한국어 의도 1-N 줄
- 타겟 (master / sub-agent / pipeline)

## Procedure
1. 의도를 (a) 목표 / (b) 검증 대상 / (c) 검증 기준 / (d) 출력 형식 으로 분해.
2. 각 항목을 번호 리스트로 변환. 한국어 명령형 ("…해라").
3. 가드 — 금지 행위, 권한 안내, 시크릿 redaction 룰 명시.
4. 출력 형식 템플릿 (보고서 표) 첨부.

## Output Format
- markdown 헤더 4개 (`목표:`, `검증 대상:`, `검증 기준:`, `최종 보고서 형식:`)
- 각 헤더 아래 번호 리스트
- 마지막에 "다음 옵션을 제시하고 사용자 선택을 기다린다" 라인

## Safety / Constraints
- raw 의도에 secret 이 섞여 있으면 redaction 후 변환.
- 추측 금지 — 미확정 사실은 NEEDS_REVIEW.

## Example Prompt
사용자: "내 봇을 nous hermes 처럼 키우고 싶어"
→ 변환:
```
목표: hermes-hybrid 를 5-loop closed-loop growth-agent 로 활성화한다.
검증 대상: 5-loop 각각의 day-0 활성 / day-7 / day-30 시그널.
검증 기준: ...
최종 보고서 형식: ...
```

## Existing Implementation
없음 (net-new).
