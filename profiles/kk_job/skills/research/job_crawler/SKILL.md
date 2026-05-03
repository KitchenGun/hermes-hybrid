---
name: job_crawler
description: Crawl game programmer postings from gamejob/jobkorea/Nexon/NC/Netmarble and emit normalized JSON.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, crawling, job, game]
    category: research
    requires_toolsets: [terminal]
    config:
      - key: per_source_limit
        default: 30
        prompt: "소스별 최대 수집 공고 수"
      - key: timeout_sec
        default: 12
        prompt: "소스별 HTTP 타임아웃 (초)"
    required_environment_variables: []
---

# When to Use
- 매일 07:40 KST `morning_game_jobs` cron의 1단계 (수집)
- on_demand 게임 프로그래머 검색 보조

## 사용하지 말아야 할 때
- 일반 직무 검색 (web_search 스킬을 쓸 것 — 이건 게임사 5곳 특화)
- 로그인이 필요한 마이페이지/지원함 페이지 접근

# Procedure

## Quick recipe

```bash
python3 ~/.hermes/profiles/kk_job/skills/research/job_crawler/scripts/crawl_game_jobs.py \
  --keywords "게임 프로그래머,클라이언트 프로그래머,Unreal,언리얼,UE5" \
  --output /tmp/kk_job_raw.json
```

성공 시 `/tmp/kk_job_raw.json` 에 다음 구조로 저장:
```json
[
  {
    "crawled_at": "2026-05-03T07:40:12+09:00",
    "source": "gamejob",
    "company": "...",
    "title": "...",
    "seniority": "신입|경력|...",
    "employment_type": "정규직|...",
    "location": "...",
    "requirements": "...",
    "preferred": "...",
    "tech_stack": "Unreal, C++, ...",
    "url": "https://...",
    "deadline": "YYYY-MM-DD|상시|None",
    "raw_text": "...",
    "applied": false,
    "expired": false
  }
]
```

## 1. 입력
- `--keywords`: 콤마 구분 키워드 (기본: "게임 프로그래머,클라이언트 프로그래머,Unreal,언리얼,UE5,Game Programmer")
- `--output`: 결과 JSON 파일 경로 (필수)
- `--per-source-limit`: 소스당 최대 수집 수 (기본 30)
- `--timeout`: HTTP 타임아웃 초 (기본 12)

## 2. 소스
순서대로 수집. 한 소스가 실패해도 나머지는 진행.
- gamejob — `https://www.gamejob.co.kr/` 검색 페이지
- jobkorea — `https://www.jobkorea.co.kr/` 검색 페이지
- nexon — `https://career.nexon.com/`
- ncsoft — `https://careers.ncsoft.com/`
- netmarble — `https://recruit.netmarble.com/`

## 3. 정규화 / dedup
- URL 기준 dedupe (querystring 정규화: utm_*, fbclid 등 제거)
- 마감 표기 파싱: "YYYY-MM-DD" / "상시채용" / "오늘 마감" 등
- 마감일 < 현재면 `expired=true` (raw에는 남기되 매칭 단계에서 제외)

## 4. 실패 격리
- 소스별 try/except. 실패하면 stderr에 `[crawler] <source> failed: <reason>` 한 줄 + 결과에서 해당 소스만 빠짐
- 전체 0건이어도 exit 0 (호출 측이 raw 파일 비어있는 것을 보고 다음 단계 결정)

# Pitfalls
- **JS 렌더링**: 일부 사이트(NC, 넷마블)는 SPA. 1차 구현은 정적 HTML/RSS만 시도. 0건이면 stderr 경고만 남기고 다음 소스로.
- **rate limit**: 소스 간 0.5초 sleep. 한 소스 내부에서도 페이지당 0.3초.
- **인코딩**: 게임잡은 EUC-KR 가능성 — `requests`가 자동 디코드 실패 시 `chardet`로 폴백.
- **robots.txt**: 공개 검색 페이지만 접근. 회원가입 게이트 페이지는 시도조차 X.
- **개인정보**: 공고 본문에 채용 담당자 이메일 있어도 raw에 그대로 저장 (공개 정보). Discord/시트 출력 시에는 마스킹 X (사용자가 응대용으로 필요).

# Verification
1. `--output` 파일 존재 + 유효 JSON 배열
2. 5개 소스 모두 시도된 흔적이 stderr 로그에 (성공·실패 무관)
3. URL dedup 동작: 동일 URL 중복 없음
4. `expired=true` 항목이 결과에 포함됨 (필터는 호출 측 책임)

# References
- requests + BeautifulSoup4 표준 패턴
- 게임잡 검색 URL: `https://www.gamejob.co.kr/Recruit/joblist?menucode=duty&duty=1&...`
