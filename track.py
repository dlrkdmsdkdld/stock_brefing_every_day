"""지난 추천이 그 뒤로 어떻게 됐는지 따라간다.

recommend.py가 고른 종목을 history.json에 쌓고, 여기서 현재가를 받아 수익률을 갱신한다.
추천이 실제로 쓸모 있었는지 판단할 근거를 남기는 것이 목적이며, 모델을 쓰지 않아 토큰이 들지 않는다.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
HISTORY = HERE / "history.json"
TRACK_DAYS = 30      # 이 기간 안의 추천만 따라간다
SHOW = 7             # 브리핑에 보여줄 최근 건수


def load():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return {"picks": []}


def save(data):
    HISTORY.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record(recommendation):
    """오늘 추천을 기록한다. 같은 날 같은 종목은 덮어쓴다."""
    data = load()
    day = recommendation["generated_at"][:10]
    kept = [row for row in data["picks"] if row["date"] != day]
    for kind in ("mine", "new"):
        pick = recommendation.get(kind)
        if not pick or pick.get("price") is None:
            continue
        kept.append(dict(date=day, kind=kind, ticker=pick["ticker"], name=pick["name"],
                         headline=pick["headline"], price_at_pick=pick["price"],
                         currency="KRW" if str(pick["ticker"]).isdigit() else "USD"))
    kept.sort(key=lambda row: (row["date"], row["kind"]))
    data["picks"] = kept
    save(data)
    return data


def latest_close(ticker, currency):
    """추천 이후 현재까지의 종가. 국내 종목은 .KS를 붙인다."""
    symbol = f"{ticker}.KS" if currency == "KRW" else ticker
    frame = yf.Ticker(symbol).history(period="10d", auto_adjust=False, timeout=20)
    frame = frame[frame["Close"] > 0].sort_index()
    if frame.empty:
        raise ValueError("시세 없음")
    return round(float(frame["Close"].iloc[-1]), 4), frame.index[-1].date().isoformat()


def update():
    """추천 기록에 현재가와 수익률을 채운다."""
    data = load()
    today = datetime.now(KST).date()
    cutoff = today - timedelta(days=TRACK_DAYS)
    fresh = [row for row in data["picks"]
             if datetime.fromisoformat(row["date"]).date() >= cutoff]
    for row in fresh:
        try:
            price, date = latest_close(row["ticker"], row["currency"])
            row["price_now"] = price
            row["price_date"] = date
            row["return_pct"] = round((price / row["price_at_pick"] - 1) * 100, 2)
            row.pop("error", None)
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
    data["picks"] = fresh
    data["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    save(data)
    return data


def recent(limit=SHOW):
    """브리핑에 보여줄 최근 추천 성적. 오늘 추천은 아직 결과가 없으므로 제외한다."""
    data = load()
    today = datetime.now(KST).date().isoformat()
    rows = [row for row in data.get("picks", [])
            if row["date"] < today and row.get("return_pct") is not None]
    rows.sort(key=lambda row: row["date"], reverse=True)
    return rows[:limit]


def scoreboard(rows):
    """맞춘 비율과 평균 수익률. 표본이 적을 때는 그 사실을 함께 봐야 한다."""
    if not rows:
        return None
    wins = sum(1 for row in rows if row["return_pct"] > 0)
    average = sum(row["return_pct"] for row in rows) / len(rows)
    return dict(count=len(rows), wins=wins, hit_rate=round(wins / len(rows) * 100),
                average=round(average, 2),
                best=max(rows, key=lambda row: row["return_pct"]),
                worst=min(rows, key=lambda row: row["return_pct"]))


def main():
    yf.set_tz_cache_location(str(HERE / ".cache" / "yfinance"))
    path = HERE / "recommendation.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("generated_at", "")[:10] == datetime.now(KST).date().isoformat():
            record(payload)
    data = update()
    rows = recent()
    board = scoreboard(rows)
    print(f"추천 기록 {len(data['picks'])}건 (최근 {TRACK_DAYS}일)")
    if board:
        print(f"최근 {board['count']}건 · 상승 {board['wins']}건 ({board['hit_rate']}%) · "
              f"평균 {board['average']:+.2f}%")
        for row in rows:
            print(f"  {row['date']} [{row['kind']}] {row['name']} ({row['ticker']}) "
                  f"{row['return_pct']:+.2f}%")
    else:
        print("아직 성적을 볼 만한 지난 추천이 없습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
