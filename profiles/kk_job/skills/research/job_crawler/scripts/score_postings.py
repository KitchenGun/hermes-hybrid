#!/usr/bin/env python3
"""score_postings.py — STRICT Top-N matcher for kk_job morning_game_jobs.

Reads enriched raw postings (seniority/deadline filled by enrich_postings.py),
applies USER.md weights with hard filters, writes curated JSON for the
curated sheet tab + Discord briefing. Replaces the LLM matching step in
cron yaml — Python rules are deterministic and stricter about seniority
and location mismatches than free-form LLM judgment.

Hard filters (excluded entirely):
  - applied=true, expired=true
  - seniority "경력 N년 이상" where N > USER_YEARS+1 (default user=3y, cap=4)
  - title contains a non-metro location bracket (e.g. [대구], [부산])
  - title matches a NEGATIVE keyword (마케팅, 기획, 디자이너 등)

Usage:
    python3 score_postings.py \\
        --input  /tmp/kk_job_raw_enriched.json \\
        --output /tmp/kk_job_curated.json \\
        [--top-n 5] [--user-years 3] [--per-company-cap 2]
"""
from __future__ import annotations
import argparse, json, re, sys
from datetime import datetime, timezone, timedelta

sys.stdout.reconfigure(encoding='utf-8')
KST = timezone(timedelta(hours=9))


def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--user-years", type=int, default=3)
    p.add_argument("--per-company-cap", type=int, default=2)
    return p.parse_args()


_args = _parse_args()
today = datetime.now(KST).strftime("%Y-%m-%d")
data = json.load(open(_args.input, "r", encoding="utf-8"))

USER_YEARS = _args.user_years
LOC_NEGATIVE = ['[대구','[부산','[대전','[광주','[제주','[울산','[창원','[포항','[강원','[전주','[청주']

EXP_MIN_RE = re.compile(r"경력\s*(\d+)\s*년\s*이상")
EXP_RANGE_RE = re.compile(r"경력\s*(\d+)\s*[~∼\-]\s*(\d+)\s*년")
EXP_OPEN_TOKENS = {"신입","경력무관","경력 무관","무관","경력없음"}

def parse_seniority(s):
    s = (s or "").strip()
    if not s: return ('unknown', None, None)
    if any(t in s for t in EXP_OPEN_TOKENS): return ('open', None, None)
    m = EXP_MIN_RE.search(s)
    if m: return ('min', int(m.group(1)), None)
    m = EXP_RANGE_RE.search(s)
    if m: return ('range', int(m.group(1)), int(m.group(2)))
    return ('unknown', None, None)

def fit_score(kind, low, high):
    if kind == 'open': return (20, False, '경력무관/신입 OK', '')
    if kind == 'min':
        if low <= USER_YEARS:     return (20, False, f'경력 {low}년 이상 — 사용자 3년차 충족', '')
        if low == USER_YEARS + 1: return (10, False, f'경력 {low}년 이상 — 사용자 -1년', f'⚠️ 경력 {low}년 — 사용자 -1년 부족')
        return (0, True, f'경력 {low}년 이상 미스매치', '')
    if kind == 'range':
        if low <= USER_YEARS + 1 and high >= USER_YEARS - 1:
            return (20, False, f'경력 {low}~{high}년 — 핏', '')
        return (0, True, f'경력 {low}~{high}년 — 범위 밖', '')
    return (8, False, '연차 정보 미공개', '⚠️ 연차 요건 미공개')

UNREAL_KW = ['unreal','언리얼','ue4','ue5']
GAMEPLAY_KW = ['게임플레이','클라이언트','gameplay','client','콘텐츠']
TECH_KW = ['테크니컬 아티스트','TA']
ENGINE_KW = ['엔진 프로그래머','engine programmer']
SERVER_KW = ['서버','server']
NEGATIVE_KW = ['웹 개발','회로','LP시스템','강사','회계','PR','마케팅','시뮬레이션','콘텐츠사업','TI','디지털트윈','매니저','기획','디자이너','애니메이터','사운드','3D 모델러']
NON_GAME = ['디자인캠프이엠','지앤아이소프트','와이엠엑스','SBS아카데미','이스트게임즈']

def is_likely_unity(title, company):
    t = title.lower(); c = (company or '').lower()
    if 'unity' in t or '유니티' in t: return True
    if '모바일' in title and any(k in c for k in ['데브캣','넷마블','컴투스','민트로켓']):
        return True
    return False

def role_score(title, raw_text):
    t = (title or '').lower() + ' ' + (raw_text or '').lower()
    if any(k.lower() in t for k in GAMEPLAY_KW): return 25, '클라/게임플레이 직무'
    if any(k.lower() in t for k in ENGINE_KW):   return 18, '엔진 프로그래머 (인접)'
    if any(k.lower() in t for k in TECH_KW):     return 15, '테크니컬 아티스트 (인접)'
    if any(k.lower() in t for k in SERVER_KW):   return 5,  '서버 위주'
    return 8, '직무 일반'

def unreal_score(title, raw_text, company):
    t = (title or '') + ' ' + (raw_text or '') + ' ' + (company or '')
    tl = t.lower()
    if any(k in tl for k in UNREAL_KW): return 40, 'UE 명시'
    if company and any(k in company for k in ['펄어비스','크래프톤','시프트업']): return 35, 'UE 사용 회사 (추정)'
    if 'NC' in (company or '') or 'AION2' in t or 'Horizon Steel' in t: return 38, 'NC 신작 UE5 추정'
    if is_likely_unity(title, company): return 5, 'Unity 추정 (모바일/명시)'
    return 12, '엔진 미공개'

def loc_score(title, raw_text):
    t = (title or '') + ' ' + (raw_text or '')
    if any(neg in t for neg in LOC_NEGATIVE):
        return (0, '비수도권', '⚠️ 비수도권 — 사용자 서울/재택 선호')
    return (5, '서울/판교 추정', '')

def out_score(co):
    if co and any(k in co for k in ['펄어비스','크래프톤','시프트업','NC','웹젠','데브캣','컴투스']):
        return 10, '대형/출시 IP 회사'
    return 5, '출시작 미공개'


def freshness_score(posted_at, source):
    """Boost recent listings, hard-exclude stale ones.

    Returns (score, note, hard_exclude, mismatch_warn).
    User intent: very old postings are likely already filled → exclude past
    60 days. Recent postings (≤ 7 days) get a meaningful bonus so newly
    opened roles surface to the top even if other dimensions are equal.

    shiftup is a single perpetually-updated page with no per-posting date,
    so we treat it as neutral (assume actively maintained).
    """
    if source == 'shiftup':
        return (5, '최신 (회사 직접 페이지)', False, '')
    if not posted_at:
        return (0, '등록일 미상', False, '')
    try:
        d = datetime.strptime(posted_at[:10], "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        return (0, f'등록일 파싱 실패: {posted_at}', False, '')
    age = (datetime.now(KST) - d).days
    if age <= 7:   return (15, f'D+{age} 신규',         False, '')
    if age <= 14:  return (10, f'D+{age} 최근',         False, '')
    if age <= 30:  return (5,  f'D+{age} 한 달 이내',    False, '')
    if age <= 60:  return (-5, f'D+{age} 1~2개월 경과', False, f'⚠️ D+{age} 등록일 한 달 초과')
    return (0, f'D+{age} 60일 초과', True, '')

def normalize_company(c):
    return (c or '').strip().lstrip('㈜').replace('주식회사','').replace('(주)','').strip()

candidates = []
for r in data:
    if r.get('applied'): continue
    if r.get('expired'): continue
    title = r.get('title',''); company = r.get('company',''); raw_text = r.get('raw_text','')
    if any(n in (title+company) for n in NEGATIVE_KW): continue
    if any(n in (title+company) for n in NON_GAME): continue
    sen = r.get('seniority','')
    kind, lo, hi = parse_seniority(sen)
    fit, exclude, fit_note, fit_warn = fit_score(kind, lo, hi)
    if exclude: continue
    rs, rs_note = role_score(title, raw_text)
    if rs < 8: continue
    ls, ls_note, loc_warn = loc_score(title, raw_text)
    if ls == 0: continue
    fs, fs_note, fs_exclude, fs_warn = freshness_score(r.get('posted_at',''), r.get('source',''))
    if fs_exclude: continue   # HARD EXCLUDE: 60+ days stale
    us, us_note = unreal_score(title, raw_text, company)
    os_, os_note = out_score(company)
    total = us + rs + fit + os_ + ls + fs
    warns = [w for w in [fit_warn, loc_warn, fs_warn] if w]
    if is_likely_unity(title, company):
        warns.append('⚠️ Unity 추정 — 사용자 메인 스택(UE) 차이')
    candidates.append({
        'company': company or '(미공개)',
        'co_norm': normalize_company(company),
        'title': title.strip(), 'url': r.get('url',''), 'seniority': sen,
        'source': r.get('source'),
        'match_score': min(100, total),
        'reason_parts': [us_note, rs_note, fit_note, fs_note, ls_note, os_note],
        'warns': warns,
    })

SRC_PRIORITY = {'jobkorea':0, 'shiftup':1, 'gamejob':2}
candidates.sort(key=lambda x: (-x['match_score'], SRC_PRIORITY.get(x['source'], 9)))
seen = {}
for c in candidates:
    # Dedupe by title prefix — gamejob lists postings under the platform's
    # own "Sword 채용관" company while jobkorea uses the real company name,
    # so co_norm doesn't help when the same posting appears on both sites.
    # Use 18-char prefix: longer prefixes get fooled by trailing-char
    # truncation (e.g. "...프로그래머 모집" vs "...프로그래머 ..").
    key = c['title'][:18]
    if key in seen: continue
    seen[key] = c
candidates = list(seen.values())
candidates.sort(key=lambda x: -x['match_score'])

per_co = {}; final = []
for c in candidates:
    n = per_co.get(c['co_norm'], 0)
    if n >= _args.per_company_cap: continue
    final.append(c); per_co[c['co_norm']] = n + 1

print(f'[score] {len(candidates)} candidates, per-co cap={_args.per_company_cap} -> {len(final)} final',
      file=sys.stderr)

curated = []
for c in final[:_args.top_n]:
    reason = '✅ ' + ', '.join(p for p in c['reason_parts'] if p)
    mismatch = ' / '.join(c['warns'])
    curated.append({
        'date': today,
        'company': c['company'],
        'title': c['title'],
        'match_score': c['match_score'],
        'match_reason': reason,
        'mismatch': mismatch,
        'url': c['url'],
    })
with open(_args.output, 'w', encoding='utf-8') as f:
    json.dump(curated, f, ensure_ascii=False, indent=2)

for i, c in enumerate(curated, 1):
    print(f"[score] #{i} [{c['match_score']:>3}] {c['company']} | {c['title'][:50]}",
          file=sys.stderr)
