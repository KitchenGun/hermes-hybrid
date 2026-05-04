---
name: job_inventory
description: Scan all hermes-hybrid profiles, parse cron/on_demand/watcher yaml files and skills tree, emit JSON inventory for advisor analysis.
version: 1.0.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [analysis, inventory, advisor]
    category: analysis
    config:
      - key: default_profiles_root
        description: "기본 profiles 디렉토리 (WSL 경로)"
        default: "/home/kang/.hermes/profiles"
    required_environment_variables: []
---

# When to Use

- advisor_ops 잡(cron/on_demand) 시작 시 첫 단계로 호출
- 모든 프로파일의 잡 yaml + skills 트리를 단일 JSON 인벤토리로 평탄화
- 후속 LLM 추천 단계의 입력 데이터로 사용

## 사용하지 말아야 할 때

- 단일 프로파일만 보면 충분할 때 (대신 `--profile <id>` 인자 사용)
- 실시간 hot-reload (디스크 풀스캔이라 비용 ~수백 ms)
- 다른 프로파일에서 호출 (advisor_ops 내부 분석 전용)

# Procedure

## 1. 입력 검증

```
optional:
  --profiles-root <path>   기본: /home/kang/.hermes/profiles
  --profile <id>           특정 프로파일만 스캔 (생략 시 전체)
  --output <path>          기본: stdout (JSON)
```

## 2. 스캔 절차

각 프로파일에 대해:
1. `config.yaml` 읽기 → model/tier_policy/mcp_servers/web/skills.auto_load 추출
2. `cron/*.yaml`, `on_demand/*.yaml`, `watchers/*.yaml` 읽기 → 잡 메타 추출
3. `skills/<category>/<name>/` 디렉토리 트리 인벤토리
4. 잡 prompt 텍스트에서 단순 키워드 매칭으로 외부 도구 언급 검출 (TODO/FIXME, "MCP", "plugin" 등)

## 3. JSON 출력 스키마

```json
{
  "scanned_at": "ISO8601 KST",
  "profiles": [
    {
      "id": "kk_job",
      "config": { "model": {...}, "tier_policy": {...}, "web_backend": "brave",
                  "mcp_servers": {}, "skills_auto_load": [...] },
      "skills": [ {"category": "research", "name": "web_search", "has_scripts": true} ],
      "jobs": [ {"name": "morning_game_jobs", "trigger_type": "cron", ...} ],
      "hints": [ {"job": "...", "kind": "TODO", "snippet": "..."} ]
    }
  ],
  "summary": { "profile_count": N, "job_count": M, "skill_count": K, ... }
}
```

# Pitfalls

- **WSL 경로 가정**: 기본값은 `/home/kang/.hermes/profiles`. Windows native에서
  실행할 경우 `--profiles-root` 명시 필수.
- **yaml 파싱 실패**: 단일 yaml이 깨져도 다른 잡은 계속 처리 (graceful degrade).
  실패 잡은 `parse_errors`에 기록하고 진행.
- **대용량 prompt**: 잡 prompt 본문은 인벤토리에 포함 안 함 (해시 + 길이만).
  필요하면 LLM이 별도 read_file로 가져간다.
- **민감 정보**: env 키 이름은 추출하되 값은 절대 읽지 않음.

# Verification

1. exit 0 + JSON stdout 또는 --output 파일 생성
2. `summary.profile_count`가 실제 디렉토리 수와 일치
3. parse_errors 0 (또는 명시적 보고)

# References

- Hermes 공식 yaml 스키마: kk_job/cron/morning_game_jobs.yaml 참조 구현
