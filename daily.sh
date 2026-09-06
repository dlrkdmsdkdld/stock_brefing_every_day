#!/bin/zsh
# 매일 자동 실행: 가격 -> 뉴스 -> Claude 요약/판단 -> 브리핑.
# 요약은 OpenAI API(OPENAI_API_KEY)를 쓴다. .env에 넣어두면 자동으로 읽는다.
set -u
cd "$(dirname "$0")"
LOG="logs/$(date +%Y-%m-%d).log"
mkdir -p logs
exec >>"$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') 시작 ====="

PY=".venv/bin/python"

"$PY" prices.py || echo "[경고] prices.py 실패 또는 검증 불일치"
"$PY" news.py   || echo "[경고] news.py 실패 또는 오늘자 기사 없는 종목 있음"

if [ "${SKIP_SUMMARY:-0}" = "1" ]; then
  echo "SKIP_SUMMARY=1 - 요약 건너뜀 (기존 verdicts.json 유지)"
else
  "$PY" summarize.py || echo "[경고] 요약 일부 실패 - 가능한 만큼만 반영해 브리핑 생성"
  "$PY" recommend.py || echo "[경고] 추천 종목 생성 실패 - 해당 섹션 없이 브리핑 생성"
fi

"$PY" brief.py >/dev/null || echo "[경고] brief.py가 확인 필요 항목을 보고함"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
  "$PY" notify.py || echo "[경고] 텔레그램 전송 실패"
else
  echo "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 없음 - 전송 건너뜀"
fi
echo "===== $(date '+%H:%M:%S') 종료 ====="
