"""오늘 가장 볼 만한 종목 하나를 고른다.

두 단계로 나눈다.
  1) 데이터로 후보 좁히기 — 거래량 배수, 등락률, 뉴스 화제성, 52주 위치, RSI, 볼린저.
     여기까지는 계산만 하므로 토큰이 들지 않는다.
  2) 후보 5개만 모델에 보내 하나를 고르게 한다. 이미 만들어 둔 뉴스 요약을 함께 보내며
     기사 본문은 보내지 않는다. 그래서 요청 한 번, 2천 토큰 안쪽으로 끝난다.

투자 자문이 아니다. "오늘 이 종목을 왜 들여다볼 만한가"를 정리하는 용도다.
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from summarize import (SCHEMA_ONLY_MARKER, ask, load_env, normalize, providers)

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
SHORTLIST = 5

INSTRUCTIONS = """너는 개인 투자자에게 오늘 들여다볼 만한 종목 하나를 골라 준다.
후보마다 오늘 지표와 (있으면) 오늘 뉴스 요약이 함께 주어진다.

- pick: 고른 종목의 ticker. 반드시 후보 안에서 고른다.
- headline: 왜 오늘 이 종목인지 한 문장. 20자 내외로 짧게.
- reason: 2~3문장. 주어진 지표와 뉴스에 실제로 나온 숫자를 인용해 근거를 댄다.
- risk: 1~2문장. 이 판단이 틀릴 수 있는 지점이나 조심할 부분.
- others: 고르지 않은 후보들에 대한 한 줄 코멘트 배열. 각 항목은 ticker와 note.

규칙:
- 주어진 데이터에 없는 사실이나 수치를 지어내지 않는다.
- 매수·매도를 권하지 않는다. "오늘 살펴볼 이유"를 설명하는 것이 목적이다.
- 지표가 좋아 보여도 근거가 약하면 그 점을 reason이나 risk에 솔직히 적는다.
- 모두 한국어로 쓴다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "pick": {"type": "string"},
        "headline": {"type": "string"},
        "reason": {"type": "string"},
        "risk": {"type": "string"},
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
    "required": ["pick", "headline", "reason", "risk", "others"],
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

    picked = shortlist(prices, news)
    if not picked:
        sys.exit("후보를 만들 수 없습니다.")
    print("후보:")
    for item in picked:
        print(f"  {item['points']:>5}  {item['row']['name']} ({item['row']['ticker']}) "
              f"· {', '.join(item['detail'])}")

    payload = "\n\n".join(describe(item, news, judged) for item in picked)
    result, used_in, used_out, _, provider = ask(
        chain, 0, payload, instructions=INSTRUCTIONS, schema=SCHEMA,
        marker=SCHEMA_ONLY_MARKER)

    tickers = {item["row"]["ticker"]: item for item in picked}
    if result.get("pick") not in tickers:
        sys.exit(f"후보에 없는 종목을 골랐습니다: {result.get('pick')}")
    chosen = tickers[result["pick"]]["row"]

    (HERE / "recommendation.json").write_text(json.dumps(dict(
        generated_at=datetime.now(KST).isoformat(timespec="seconds"),
        model=provider, disclaimer="투자 자문이 아니며, 오늘 살펴볼 이유를 정리한 것입니다.",
        usage=dict(input_tokens=used_in, output_tokens=used_out),
        candidates=[dict(ticker=item["row"]["ticker"], name=item["row"]["name"],
                         points=item["points"], detail=item["detail"]) for item in picked],
        pick=dict(ticker=chosen["ticker"], name=chosen["name"],
                  group=chosen.get("group", "holding"),
                  headline=result["headline"], reason=result["reason"],
                  risk=result["risk"]),
        others=result["others"]), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n추천: {chosen['name']} ({chosen['ticker']}) — {result['headline']}")
    print(f"{provider} · 입력 {used_in:,} · 출력 {used_out:,} 토큰")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
