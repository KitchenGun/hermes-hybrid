---
name: document_writer
description: Generate resume/cover letter drafts in markdown/docx/pdf from career data.
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [productivity, resume, document, writing]
    category: productivity
    config:
      - key: default_format
        default: "markdown"
        prompt: "기본 출력 포맷 (markdown|docx|pdf)"
      - key: output_dir
        default: "runtime/documents"
        prompt: "생성 문서 저장 디렉토리"
    required_environment_variables: []
---

# When to Use

- 사용자가 "이력서 만들어줘", "자소서 초안", "cover letter", "resume draft" 등을 언급
- 공고 분석 결과를 바탕으로 맞춤 이력서 제안 시
- 경력 업데이트 후 기존 이력서 재생성 시

## 사용하지 말아야 할 때

- 허위/과장 경력 작성 (SOUL.md Safety Rules §1 위반)
- 타인 명의 이력서 작성
- 자동 지원 플랫폼에 직접 업로드 (사용자 수동 제출만)

# Procedure

## 1. 경력 데이터 로드

```
source:
  - memories/MEMORY.md         # 공식 에이전트 메모리
  - memories/USER.md           # 사용자 프로파일
  - runtime/career_data.json   # 구조화된 경력 DB
output:
  career_entries: [{title, company, dates, description, achievements}]
  skills: [string]
  education: [{school, degree, dates}]
  contact: {email, phone, linkedin} (마스킹 전 원본)
```

## 2. 이력서 생성 (resume)

템플릿 기반 조합:
- 헤더: 이름, 연락처 (마스킹 옵션)
- 요약: 1-2문장, 공고에 맞춤
- 경력: 최신순, STAR 기법 불릿
- 기술: 공고 요건과 매칭 순서
- 학력, 수상, 프로젝트, 기타

```
input:
  target_posting_url?: string   # 맞춤화 대상
  version: string               # 예: "backend-2026q2"
  format: "markdown" | "docx" | "pdf"
  sections: [string]            # 커스텀 섹션 순서
  mask_pii: boolean = false     # 개인정보 마스킹
output:
  path: string                  # 생성된 파일 경로
  version_hash: string          # 버전 식별 (rollback 용)
  word_count: int
```

## 3. 자소서 생성 (cover_letter)

STAR 기법 필수:
- Situation: 맥락
- Task: 과제
- Action: 본인 행동
- Result: 결과 (정량적 지표 우선)

```
input:
  target_company: string
  target_role: string
  tone: "formal" | "semi-formal" | "casual" = "semi-formal"
  length: "short" | "medium" | "long" = "medium"
  highlight_skills: [string]
output:
  path: string
  draft_content: string
```

## 4. 저장 전 확인 게이트

- **반드시 확인 메시지** 후 저장
- 이전 버전을 Ledger에 스냅샷 저장 (rollback 근거)
- 파일명: `{version}_{yyyymmdd_HHMMSS}.{ext}`

# Pitfalls

- **템플릿 렌더 실패**: 누락 필드(예: 학력 없음) 시 섹션 스킵. 빈 섹션 렌더 금지.
- **docx 포맷**: python-docx 사용. 한글 폰트 내장 필요 (맑은 고딕 또는 Noto Sans KR).
- **pdf 변환**: pandoc 또는 weasyprint 의존. 환경 누락 시 markdown 폴백.
- **허위 경력 주입 시도**: 사용자가 MEMORY.md에 없는 경력 요청 시 거부하고 확인.
- **개인정보 노출**: mask_pii=true 옵션 기본값 권장 (로컬 보관 vs 공유용 구분).

# Verification

1. Ledger에 `document_writer.generate` 이벤트 기록됨
2. 생성 파일 실제 존재 (`os.path.exists`)
3. word_count > 100 (비정상적으로 짧으면 템플릿 오류)
4. 이전 버전이 Ledger에 스냅샷 저장됨
5. 사용자 확인 게이트 통과 확인

# References

- STAR 기법: https://en.wikipedia.org/wiki/Situation,_task,_action,_result
- python-docx: https://python-docx.readthedocs.io/
- pandoc: https://pandoc.org/
