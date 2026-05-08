---
name: github_repo_analysis
agent_handle: "@github_repo_analyzer"
category: research
role: repo_audit
description: GitHub 레포 URL → 구조 / 핵심 모듈 / 최근 변경 / 의존성 / 약점 audit.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "지정 GitHub URL 의 코드 구조 분석 요청"
  - "특정 라이브러리 채택 전 audit"
not_for:
  - "본 repo 내부 분석 (→ @analyst)"
  - "단순 README 읽기 (→ @finder)"
inputs:
  - "github URL"
  - "관심 영역 (선택)"
outputs:
  - "구조 / 핵심 모듈 / 최근 PR / 약점 markdown"
metadata:
  hermes:
    primary_tools: [gh, web_fetch]
    tags: [github, audit, research]
---

# Skill — github_repo_analysis

## Purpose
사용자가 준 GitHub URL → 한 번의 audit 으로 사용 가능 여부 판단할 정보를 제공.

## When to Use
- "이 레포 분석해줘 <URL>" 패턴 (W10 detector 가 4회 이상 반복 시 자동 enqueue)
- 의존 라이브러리 채택 전 약점 검토

## Inputs
- GitHub URL
- 관심 영역 (build / runtime / API surface / 활성도)

## Procedure
1. `gh repo view <owner>/<name> --json …` 으로 메타.
2. README + 최근 commits + 최근 PRs.
3. dependency / build 시스템 식별.
4. 활성도 (last commit, contributor count, issue close rate).
5. 약점 (보안 issue / 미답 PR / 라이선스).

## Output Format
- 한 줄 요약
- 구조 / 핵심 모듈 / 최근 변경 / 의존성 / 활성도 / 약점 / 권장 채택 여부

## Safety / Constraints
- 토큰 / OAuth 인용 금지.
- "추천" 은 명시 신뢰도 (high/medium/low) 와 함께.

## Example Prompt
"이 레포 분석해줘: https://github.com/foo/bar"

## Existing Implementation
없음 (net-new). W10 real-time detector 가 반복 요청 4회 이상 감지 시 이 skill draft enqueue.
