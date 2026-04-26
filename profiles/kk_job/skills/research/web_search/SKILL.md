---
name: web_search
description: Job posting search via Brave Search / Tavily / Exa with content extraction.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, search, job, web]
    category: research
    config:
      - key: default_backend
        default: "brave"
        prompt: "검색 백엔드 (brave|tavily|exa)"
      - key: max_results
        default: 10
        prompt: "한 번에 가져올 최대 결과 수"
    required_environment_variables:
      - name: BRAVE_SEARCH_API_KEY
        prompt: "Brave Search API 키"
---

# When to Use

- 사용자가 "구인 공고", "채용", "job posting", "OO회사 채용" 등을 언급
- 이력서 맞춤화를 위해 특정 공고 상세 분석이 필요할 때
- 주간 잡 다이제스트 cron 실행 시

## 사용하지 말아야 할 때

- 회사 내부 정보 조회 (권한 미보유)
- LinkedIn 같이 로그인 필요한 플랫폼 (공식 API 별도)
- 위조 가능성 있는 개인 블로그/카페 공고

# Procedure

## 1. 검색 (search)

backend에 따라 API 호출:
- Brave Search: `GET https://api.search.brave.com/res/v1/web/search`
- Tavily: `POST https://api.tavily.com/search`
- Exa: `POST https://api.exa.ai/search`

```
input:
  query: string          # "백엔드 엔지니어 Python 서울"
  backend: "brave" | "tavily" | "exa"
  max_results: int = 10
  site_filter: [string]? # ["wanted.co.kr", "programmers.co.kr"]
output:
  results: [{url, title, snippet, published_date}]
```

## 2. 콘텐츠 추출 (extract)

공고 페이지 본문 추출:
- 공식 `web_extract` 보조 모델 사용
- 주요 섹션 추출: 직무명, 회사, 요건, 우대사항, 복지, 마감일

```
input:
  url: string
output:
  title: string
  company: string
  description: string
  requirements: [string]
  preferred: [string]
  deadline: ISO8601?
  salary_range?: {min, max}
  location: string
  raw_html_hash: string  # 캐시용
```

## 3. 매칭 점수 계산 (score)

사용자 경력(MEMORY.md)과 공고 요건 비교:
- 요건 충족률 × 0.6 + 우대사항 충족률 × 0.4
- 0~100 점수 반환
- 부족한 스킬 목록 포함

# Pitfalls

- **Rate Limit**: Brave 무료 플랜 월 2000 쿼리. 고빈도 잡은 캐시 필수.
- **공고 만료**: 404/301 수신 시 "만료됨" 표시, 북마크에서 자동 플래그.
- **동적 페이지**: JavaScript 렌더링 필요한 사이트(wanted.co.kr 등)는 `browser` toolset 사용.
- **중복 공고**: 같은 공고가 여러 플랫폼에 게시됨. URL 정규화로 dedup.
- **번역 품질**: 영문 공고 자동 번역은 오해 위험. 원문 + 번역 병기.

# Verification

1. Ledger에 `web_search.query` 이벤트 기록됨
2. 응답 `results` 배열 길이 > 0 (0건이면 "결과 없음" 명시)
3. 각 결과의 URL HTTP 200 확인 (스팟체크)
4. 추출된 `requirements`와 `preferred` 배열 각각 1개 이상

# References

- Brave Search API: https://brave.com/search/api/
- Tavily API: https://docs.tavily.com/
- Exa API: https://docs.exa.ai/
