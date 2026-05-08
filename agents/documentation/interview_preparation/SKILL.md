---
name: interview_preparation
agent_handle: "@interview_prep"
category: documentation
role: career_prep
description: 게임 프로그래머 면접 대비 — 자주 나오는 질문 / 코딩 / 시스템 설계 / 포트폴리오 정리 보조.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "면접 예상 질문 리스트 / 모의 답변 정리"
  - "포트폴리오 케이스 스터디 작성 보조"
not_for:
  - "기술 검색 (→ @researcher)"
  - "코드 리뷰 (→ @reviewer)"
inputs:
  - "타겟 회사 / 직무"
  - "이력서 또는 포트폴리오 path"
outputs:
  - "예상 질문 + 모범 답변 + 약점 보완"
metadata:
  hermes:
    primary_tools: [filesystem, web_fetch]
    tags: [career, interview, game]
---

# Skill — interview_preparation

## Purpose
사용자가 게임 프로그래머 직군이라는 컨텍스트 (memory `user_role.md`) 기반, 면접 대비 보조.

## When to Use
- 특정 회사 채용 공고 + 이력서 → 예상 질문 도출
- 모의 답변 작성

## Inputs
- 회사 / 직무
- 이력서 / 포트폴리오 path
- 약점 자기 평가 (선택)

## Procedure
1. 타겟 직무 분석 (engine / gameplay / graphics / tools / online …).
2. 자주 나오는 질문 카테고리 (자료구조 / OS / 그래픽 / 엔진 / 시스템 설계).
3. 사용자 이력서 매칭 후 강점/약점 매핑.
4. 모범 답변 + 약점 보완 책 / 자료 추천.

## Output Format
- markdown — 카테고리별 Q&A + 약점 보완 plan

## Safety / Constraints
- 회사 내부 정보 추측 금지.
- 이력서에 secret 있을 시 redact.

## Example Prompt
"넥슨 엔진 프로그래머 면접 예상 질문 정리"

## NEEDS_REVIEW
사용자가 실제로 면접 단계인지 transcript 명시 부족 — Loop 3 dialectic 가 confirm/retire 결정.

## Existing Implementation
없음 (net-new).
