#!/usr/bin/env bash
# bootstrap_profile.sh — Hermes 프로파일을 WSL에 연결
#
# 사용법 (WSL 내부에서):
#   bash /mnt/e/hermes-hybrid/scripts/bootstrap_profile.sh calendar_ops
#
# 수행 작업:
#   1) 프로파일 디렉토리 존재 확인
#   2) ~/.hermes-<name>  →  /mnt/e/hermes-hybrid/profiles/<name>  symlink 생성
#   3) 런타임 디렉토리(~/.hermes-<name>-runtime/{memories,sessions,logs}) 생성
#   4) .env 존재 확인, 없으면 .env.example 복사
#   5) auth.json 존재 확인 (없으면 재인증 안내만)
#
# 주의:
#   - /mnt/e 는 WSL에서 성능 느림. 정적 파일(config/skill/cron)만 여기 둠.
#   - 런타임(memories/sessions/logs)은 WSL 로컬 디렉토리 사용.

set -euo pipefail

PROFILE_NAME="${1:-}"
if [ -z "$PROFILE_NAME" ]; then
    echo "usage: $0 <profile_name>" >&2
    echo "예: $0 calendar_ops" >&2
    exit 1
fi

REPO_ROOT="${HERMES_HYBRID_REPO:-/mnt/e/hermes-hybrid}"
SRC="$REPO_ROOT/profiles/$PROFILE_NAME"
LINK="$HOME/.hermes-$PROFILE_NAME"
RUNTIME="$HOME/.hermes-$PROFILE_NAME-runtime"

# ── 1. 프로파일 존재 확인 ────────────────────────────────────────
if [ ! -d "$SRC" ]; then
    echo "[bootstrap] ERROR: 프로파일 디렉토리 없음: $SRC" >&2
    exit 1
fi
if [ ! -f "$SRC/config.yaml" ]; then
    echo "[bootstrap] ERROR: config.yaml 없음: $SRC/config.yaml" >&2
    exit 1
fi

echo "[bootstrap] 프로파일 소스: $SRC"

# ── 2. Symlink 생성 ──────────────────────────────────────────────
if [ -L "$LINK" ]; then
    CURRENT_TARGET=$(readlink "$LINK")
    if [ "$CURRENT_TARGET" = "$SRC" ]; then
        echo "[bootstrap] symlink 이미 올바름: $LINK"
    else
        echo "[bootstrap] WARNING: symlink가 다른 곳을 가리킴"
        echo "           current: $CURRENT_TARGET"
        echo "           expected: $SRC"
        read -p "           재링크할까요? [y/N] " -r
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm "$LINK"
            ln -s "$SRC" "$LINK"
            echo "[bootstrap] 재링크 완료: $LINK -> $SRC"
        else
            echo "[bootstrap] 취소됨"
            exit 1
        fi
    fi
elif [ -e "$LINK" ]; then
    echo "[bootstrap] ERROR: $LINK 이 디렉토리/파일로 존재 (symlink 아님)" >&2
    echo "           수동으로 백업 후 제거하세요: mv $LINK ${LINK}.bak" >&2
    exit 1
else
    ln -s "$SRC" "$LINK"
    echo "[bootstrap] symlink 생성: $LINK -> $SRC"
fi

# ── 3. 런타임 디렉토리 (WSL 로컬) ─────────────────────────────────
mkdir -p "$RUNTIME"/{memories,sessions,logs,.cache}
echo "[bootstrap] 런타임 디렉토리: $RUNTIME"

# ── 4. .env 체크 ─────────────────────────────────────────────────
if [ ! -f "$SRC/.env" ]; then
    if [ -f "$SRC/.env.example" ]; then
        cp "$SRC/.env.example" "$SRC/.env"
        echo ""
        echo "[bootstrap] ⚠️  .env 파일을 생성했습니다: $SRC/.env"
        echo "             다음 값을 채워주세요:"
        echo "               - DISCORD_BRIEFING_WEBHOOK_URL"
        echo "               - GOOGLE_CALENDAR_ID"
        echo "               - TIMEZONE"
        echo ""
    else
        echo "[bootstrap] WARNING: .env와 .env.example 둘 다 없음" >&2
    fi
else
    echo "[bootstrap] .env 존재 OK"
fi

# ── 5. OAuth 자격증명 체크 ───────────────────────────────────────
if [ ! -f "$SRC/auth.json" ]; then
    echo ""
    echo "[bootstrap] ⚠️  auth.json 없음 — Google OAuth 재인증 필요"
    echo "             다음 명령으로 인증 흐름 실행:"
    echo "               hermes -p $PROFILE_NAME auth google-calendar"
    echo "             또는 ~/.hermes-$PROFILE_NAME/auth.json 에 직접 배치"
    echo ""
fi

# ── 6. 환경변수 export 안내 ───────────────────────────────────────
cat <<EOF

[bootstrap] 완료. 다음을 쉘 설정(~/.bashrc 또는 ~/.zshrc)에 추가하세요:

  export HERMES_PROFILE_HOME="$SRC"
  export HERMES_RUNTIME_HOME="$RUNTIME"

확인 명령:
  hermes -p $PROFILE_NAME chat -q "오늘 일정 알려줘"

크론 등록 (예시):
  hermes -p $PROFILE_NAME cron create \\
    --name morning_briefing \\
    --schedule "0 8 * * *" \\
    --skill google_calendar \\
    --skill discord_notify \\
    --prompt "\$(cat $SRC/cron/read/morning_briefing.yaml)"

EOF
