#!/bin/zsh
# 매일 자동 실행: 가격 -> 뉴스 -> Claude 요약/판단 -> 브리핑.
# claude CLI의 기존 로그인을 쓰므로 API 키가 따로 필요 없다.
set -u
cd "$(dirname "$0")"
LOG="logs/$(date +%Y-%m-%d).log"
mkdir -p logs
exec >>"$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') 시작 ====="

PY=".venv/bin/python"
CLAUDE="${CLAUDE_BIN:-$HOME/.local/bin/claude}"

"$PY" prices.py || echo "[경고] prices.py 실패 또는 검증 불일치"
"$PY" news.py   || echo "[경고] news.py 실패 또는 오늘자 기사 없는 종목 있음"

if [ -x "$CLAUDE" ]; then
  "$CLAUDE" -p "$(cat prompts/summarize.md)" \
      --permission-mode acceptEdits --allowed-tools Read Write Edit Bash \
    || echo "[경고] Claude 요약 실패 - 이전 verdicts.json으로 브리핑 생성"
else
  echo "[경고] claude CLI 없음($CLAUDE) - 요약 건너뜀"
fi

"$PY" brief.py >/dev/null || echo "[경고] brief.py가 확인 필요 항목을 보고함"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  "$PY" notify.py || echo "[경고] 텔레그램 전송 실패"
else
  echo "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 없음 - 전송 건너뜀"
fi
echo "===== $(date '+%H:%M:%S') 종료 ====="
