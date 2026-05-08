---
name: unity_game_analysis
agent_handle: "@unity_game_analyst"
category: research
role: unity_audit
description: Unity 프로젝트 (디스크 디렉토리 또는 GitHub) 의 컴포넌트/시스템 audit.
auto_generated:
  date: 2026-05-08
  source: hermes_growth_migration_p0a
  status: candidate
when_to_use:
  - "Unity 프로젝트 구조 / addressables / SO 의존성 분석"
  - "ScriptableObject / Prefab 변경 영향 평가"
not_for:
  - "Unreal 프로젝트 (→ @unreal_game_dev — UE5 skill)"
  - "코드 직접 수정 (→ @coder)"
inputs:
  - "Unity 프로젝트 path 또는 GitHub URL"
  - "관심 시스템 (URP/HDRP, Addressables, Netcode 등)"
outputs:
  - "씬/프리팹/SO 구조 markdown + 권장 개선"
metadata:
  hermes:
    primary_tools: [filesystem, web_fetch]
    tags: [unity, game, analysis]
---

# Skill — unity_game_analysis

## Purpose
사용자 본업이 Unity 게임 개발 → 보조 audit. 봇이 Unity 코드를 직접 작성하진 않지만, 구조/의존/충돌을 빠르게 진단.

## When to Use
- "이 Unity 프로젝트 audit 해줘"
- prefab/SO 변경 전 영향 평가

## Inputs
- 프로젝트 경로 or GitHub URL
- 관심 시스템

## Procedure
1. ProjectSettings, Packages/manifest.json 읽기 (URP/HDRP/version).
2. Assets/ tree → top-level 폴더 분류 (Scenes / Prefabs / Scripts / SO / Addressables).
3. Addressables groups (있을 경우) catalog.
4. SO 의존 그래프 (asset GUID 기반).
5. 약점 — 미사용 asset / 순환 의존 / 거대 prefab.

## Output Format
- 한 줄 요약
- 구조 / 의존 / 약점 / 권장
- "GPU 휴리스틱 사용 금지" — 사용자가 GPU 를 에디터·빌드·플레이테스트에 활용 중 (memory `user_role.md`).

## Safety / Constraints
- 사용자의 본업 코드 직접 수정 금지.
- license / 자산 ownership 추측 금지.

## Example Prompt
"이 Unity 프로젝트 폴더 한번 봐줘: D:/myproject"

## Existing Implementation
없음 (net-new).
