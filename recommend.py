"""오늘 볼 만한 종목을 두 갈래로 고른다.

  내 목록  — 보유·관심 종목 중에서 오늘 가장 눈에 띄는 것
  새 종목  — 내 목록에 없는 것 중에서 편입을 검토해 볼 만한 것

두 단계로 나눈다.
  1) 데이터로 후보 좁히기 — 계산만 하므로 토큰이 들지 않는다.
     내 목록: 거래량 배수, 등락률, 뉴스 화제성, 52주 위치, RSI, 볼린저.
     새 종목: Yahoo 스크리너(급등·급락·거래활발·저평가성장)에서 받아 내 목록을 빼고,
             남은 종목의 시세를 받아 RSI·볼린저·52주 위치를 계산한다.
  2) 두 후보군을 한 번의 요청으로 모델에 보낸다. 기사 본문은 보내지 않고 요약만 쓴다.

투자 자문이 아니다. "오늘 이 종목을 왜 들여다볼 만한가"를 정리하는 용도다.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

import indicators
from holdings import ALL
from summarize import (SCHEMA_ONLY_MARKER, ask, load_env, normalize, providers)

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
SHORTLIST = 5
OUTSIDE_SHORTLIST = 5
# 여러 화면을 함께 봐서 급등주만 올라오는 편향을 줄인다.
SCREENS = ("day_gainers", "day_losers", "most_actives",
           "undervalued_growth_stocks", "growth_technology_stocks")
SCREEN_COUNT = 12
MIN_CAP = 2e9          # 초소형주는 변동이 커 후보에서 제외
MIN_PRICE = 5.0

INSTRUCTIONS = """너는 개인 투자자에게 오늘 들여다볼 만한 종목을 골라 준다.
후보가 두 묶음으로 주어진다.

  [내 목록] 이미 보유하거나 관심 목록에 넣어 둔 종목
  [새 종목] 목록에 없는 종목. 편입을 검토해 볼 만한지 보는 대상

각 묶음에서 하나씩 골라 inside와 outside에 담는다. 형식은 둘 다 같다.

- ticker: 고른 종목. 반드시 해당 묶음의 후보 안에서 고른다.
- headline: 왜 오늘 이 종목인지 한 문장. 20자 내외로 짧게.
- reason: 2~3문장. 주어진 지표와 뉴스에 실제로 나온 숫자를 인용해 근거를 댄다.
- risk: 1~2문장. 이 판단이 틀릴 수 있는 지점이나 조심할 부분.

그리고 others에 고르지 않은 후보들의 한 줄 코멘트를 담는다(ticker, note).

규칙:
- 주어진 데이터에 없는 사실이나 수치를 지어내지 않는다.
- 매수·매도를 권하지 않는다. "오늘 살펴볼 이유"를 설명하는 것이 목적이다.
- [새 종목]은 뉴스 요약이 없다. 지표만으로 판단해야 하므로 근거가 얕다는 점을 risk에 적는다.
- 지표가 좋아 보여도 근거가 약하면 그 점을 reason이나 risk에 솔직히 적는다.
- 모두 한국어로 쓴다."""

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": "string"},
        "headline": {"type": "string"},
        "reason": {"type": "string"},
        "risk": {"type": "string"},
    },
    "required": ["ticker", "headline", "reason", "risk"],
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "inside": PICK_SCHEMA,
        "outside": PICK_SCHEMA,
        "others": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "note": {"type": "string"}},
                "required": ["ticker", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["inside", "outside", "others"],
    "additionalProperties": False,
}


def buzz_rank(news):
    """오늘자 기사 수를 시장별 순위로 바꾼다.

    국내는 네이버·구글을 함께 훑어 기사 수가 원래 많다. 절대 건수로 비교하면
    국내 종목이 늘 이기므로 같은 시장 안에서의 상대 순위로 환산한다.
    """
    rows = {**news.get("news", {}), **news.get("alerts", {})}
    by_market = {}
    for name, row in rows.items():
        by_market.setdefault(row["market"], []).append((row.get("today_count", 0), name))
    rank = {}
    for market, entries in by_market.items():
        entries.sort(reverse=True)
        for order, (count, name) in enumerate(entries):
            rank[name] = dict(count=count,
                              score=1 - order / max(len(entries) - 1, 1))
    return rank


def score(row, buzz):
    """오늘 얼마나 눈여겨볼 만한가. 방향이 아니라 '주목도'를 본다."""
    points = 0.0
    detail = []
    ratio = row.get("volume_ratio")
    if ratio and ratio > 1:
        points += min((ratio - 1) * 4, 8)
        detail.append(f"거래량 {ratio:.1f}배")
    change = abs(row.get("change_pct") or 0)
    points += min(change * 0.7, 7)
    if change >= 3:
        detail.append(f"등락 {row['change_pct']:+.2f}%")
    seen = buzz.get(row["name"])
    if seen:
        points += seen["score"] * 5
        if seen["count"]:
            detail.append(f"오늘 기사 {seen['count']}건")
    band = row.get("bb_position")
    if band and band != "밴드 내":
        points += 4
        detail.append(f"볼린저 {band}")
    rsi = row.get("rsi")
    if rsi is not None and (rsi >= 70 or rsi <= 30):
        points += 3
        detail.append(f"RSI {rsi:.0f}")
    near = row.get("from_year_low")
    if near is not None and near <= 5:
        points += 2
        detail.append(f"52주 저점 대비 {near:+.1f}%")
    high = row.get("from_year_high")
    if high is not None and high >= -3:
        points += 2
        detail.append(f"52주 고점 대비 {high:+.1f}%")
    return round(points, 1), detail


def shortlist(prices, news):
    buzz = buzz_rank(news)
    rows = [row for row in prices["holdings"] if row.get("change_pct") is not None]
    ranked = []
    for row in rows:
        points, detail = score(row, buzz)
        ranked.append(dict(row=row, points=points, detail=detail))
    ranked.sort(key=lambda item: -item["points"])
    return ranked[:SHORTLIST]


def screen_universe():
    """Yahoo 스크리너에서 시장 후보를 모은다. 내 목록에 이미 있는 종목은 뺀다."""
    owned = {item["ticker"] for item in ALL}
    found, errors = {}, []
    for name in SCREENS:
        try:
            result = yf.screen(name, count=SCREEN_COUNT)
            quotes = result.get("quotes", []) if isinstance(result, dict) else (result or [])
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        for quote in quotes:
            symbol = quote.get("symbol")
            cap = quote.get("marketCap") or 0
            price = quote.get("regularMarketPrice") or 0
            if not symbol or symbol in owned or symbol in found:
                continue
            if cap < MIN_CAP or price < MIN_PRICE:
                continue
            found[symbol] = dict(ticker=symbol, market_cap=cap,
                                 name=quote.get("shortName") or symbol,
                                 change_pct=round(quote.get("regularMarketChangePercent") or 0, 2),
                                 screens=[name])
        for symbol in found:
            if symbol in {q.get("symbol") for q in quotes} and name not in found[symbol]["screens"]:
                found[symbol]["screens"].append(name)
    return list(found.values()), errors


def enrich(candidate, cutoff, start):
    """후보 종목의 시세를 받아 RSI·볼린저·52주 위치·거래량 배수를 계산한다."""
    security = yf.Ticker(candidate["ticker"])
    frame = security.history(start=start.isoformat(), end=cutoff.isoformat(),
                             auto_adjust=False, actions=False, timeout=20)
    frame = frame.loc[[index.date() < cutoff for index in frame.index]].sort_index()
    closes = [float(value) for value in frame["Close"]]
    if len(closes) < 25:
        raise ValueError("시세 부족")
    candidate.update(indicators.compute(closes), price=round(closes[-1], 4),
                     date=frame.index[-1].date().isoformat())
    info = security.fast_info
    high, low = info["yearHigh"], info["yearLow"]
    if high and low and high > low:
        candidate["year_position"] = round((closes[-1] - low) / (high - low), 3)
        candidate["from_year_high"] = round((closes[-1] / high - 1) * 100, 2)
        candidate["from_year_low"] = round((closes[-1] / low - 1) * 100, 2)
    volume, average = info["lastVolume"], info["tenDayAverageVolume"]
    if volume and average:
        candidate["volume_ratio"] = round(volume / average, 2)
    return candidate


def outside_shortlist(prices):
    """시장 후보 중 지표가 눈에 띄는 것부터 고른다."""
    from datetime import timedelta
    from prices import cutoff_date, HISTORY_DAYS
    cutoff = cutoff_date("America/New_York")
    start = cutoff - timedelta(days=HISTORY_DAYS)
    universe, errors = screen_universe()
    ready = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for candidate in pool.map(
                lambda row: _safe_enrich(row, cutoff, start), universe):
            if candidate:
                ready.append(candidate)
    scored = []
    for candidate in ready:
        points, detail = score(candidate, {})
        scored.append(dict(row=candidate, points=points, detail=detail))
    scored.sort(key=lambda item: -item["points"])
    return scored[:OUTSIDE_SHORTLIST], errors


def _safe_enrich(candidate, cutoff, start):
    try:
        return enrich(candidate, cutoff, start)
    except Exception:
        return None


def describe_outside(item):
    row = item["row"]
    cap = row.get("market_cap") or 0
    return "\n".join([
        f"### {row['ticker']} · {row['name']} (새 종목)",
        f"지표: {' / '.join(item['detail']) or '특이사항 없음'}",
        f"종가 {row['price']:,} USD ({row.get('change_pct', 0):+.2f}%) · "
        f"시총 ${cap / 1e9:,.1f}B · RSI {row.get('rsi', '?')} · "
        f"볼린저 {row.get('bb_position', '?')} · 52주 위치 {row.get('year_position', 0):.0%}",
        f"선정 화면: {', '.join(row['screens'])}",
        "오늘 뉴스: 수집하지 않음(목록 밖 종목)"])


def describe(item, news, judged):
    row = item["row"]
    group = "보유" if row.get("group", "holding") == "holding" else "관심"
    lines = [f"### {row['ticker']} · {row['name']} ({group})",
             f"지표: {' / '.join(item['detail']) or '특이사항 없음'}",
             f"종가 {row['price']:,} {row['currency']} ({row.get('change_pct', 0):+.2f}%) · "
             f"RSI {row.get('rsi', '?')} · 볼린저 {row.get('bb_position', '?')} · "
             f"52주 위치 {row.get('year_position', 0):.0%}"]
    stories = ((news.get("news", {}).get(row["name"])
                or news.get("alerts", {}).get(row["name"])) or {}).get("stories") or []
    for story in stories:
        verdict = judged.get(normalize(story["title"]))
        if verdict:
            lines.append(f"오늘 뉴스[{verdict['stance']}]: {verdict['summary']}")
        else:
            lines.append(f"오늘 뉴스(요약 없음): {story['title']}")
    if not stories:
        lines.append("오늘 뉴스: 없음")
    return "\n".join(lines)


def main():
    load_env()
    chain = providers()
    if not chain:
        sys.exit("쓸 수 있는 제공처가 없습니다.")

    prices = json.loads((HERE / "prices.json").read_text(encoding="utf-8"))
    news = json.loads((HERE / "news.json").read_text(encoding="utf-8"))
    path = HERE / "verdicts.json"
    judged = (json.loads(path.read_text(encoding="utf-8")).get("verdicts", {})
              if path.exists() else {})

    inside = shortlist(prices, news)
    if not inside:
        sys.exit("내 목록에서 후보를 만들 수 없습니다.")
    outside, screen_errors = outside_shortlist(prices)

    print("[내 목록] 후보")
    for item in inside:
        print(f"  {item['points']:>5}  {item['row']['name']} ({item['row']['ticker']}) "
              f"· {', '.join(item['detail'])}")
    print("[새 종목] 후보")
    for item in outside:
        print(f"  {item['points']:>5}  {item['row']['name']} ({item['row']['ticker']}) "
              f"· {', '.join(item['detail'])}")
    if screen_errors:
        print(f"  스크리너 오류: {screen_errors}", file=sys.stderr)

    blocks = ["[내 목록] 아래에서 inside를 고른다."]
    blocks += [describe(item, news, judged) for item in inside]
    if outside:
        blocks.append("[새 종목] 아래에서 outside를 고른다.")
        blocks += [describe_outside(item) for item in outside]
    result, used_in, used_out, _, provider = ask(
        chain, 0, "\n\n".join(blocks), instructions=INSTRUCTIONS, schema=SCHEMA,
        marker=SCHEMA_ONLY_MARKER)

    def resolve(key, pool, group):
        choice = result.get(key) or {}
        rows = {item["row"]["ticker"]: item["row"] for item in pool}
        row = rows.get(choice.get("ticker"))
        if not row:
            print(f"[경고] {key}: 후보에 없는 종목 {choice.get('ticker')} - 제외",
                  file=sys.stderr)
            return None
        return dict(ticker=row["ticker"], name=row["name"], group=group,
                    price=row.get("price"), change_pct=row.get("change_pct"),
                    market_cap=row.get("market_cap"),
                    headline=choice["headline"], reason=choice["reason"], risk=choice["risk"])

    picked_inside = resolve("inside", inside, "mine")
    picked_outside = resolve("outside", outside, "new") if outside else None
    if not picked_inside and not picked_outside:
        sys.exit("모델이 후보 밖 종목만 골랐습니다.")

    (HERE / "recommendation.json").write_text(json.dumps(dict(
        generated_at=datetime.now(KST).isoformat(timespec="seconds"),
        model=provider, disclaimer="투자 자문이 아니며, 오늘 살펴볼 이유를 정리한 것입니다.",
        usage=dict(input_tokens=used_in, output_tokens=used_out),
        candidates=dict(
            mine=[dict(ticker=i["row"]["ticker"], name=i["row"]["name"],
                       points=i["points"], detail=i["detail"]) for i in inside],
            new=[dict(ticker=i["row"]["ticker"], name=i["row"]["name"],
                      points=i["points"], detail=i["detail"]) for i in outside]),
        screen_errors=screen_errors,
        mine=picked_inside, new=picked_outside,
        others=result.get("others", [])), ensure_ascii=False, indent=2), encoding="utf-8")

    for label, pick in (("내 목록", picked_inside), ("새 종목", picked_outside)):
        if pick:
            print(f"\n{label}: {pick['name']} ({pick['ticker']}) — {pick['headline']}")
    print(f"{provider} · 입력 {used_in:,} · 출력 {used_out:,} 토큰")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
