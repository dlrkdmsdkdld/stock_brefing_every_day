"""종목 목록을 한 곳에서 읽는다. 추가·삭제는 holdings.json만 고치면 된다.

holdings  = 보유 종목. 가격·지표에 더해 뉴스까지 수집한다.
watchlist = 관심 종목. 가격·지표만 보고 뉴스는 수집하지 않는다.

보유 종목에 "news": false를 주면 그 종목만 뉴스를 건너뛴다. 가격·지표·시총은 그대로 나온다.
요약에 드는 토큰을 줄이면서도 종목을 목록에서 빼지 않으려고 둔 스위치다. true로 되돌리면 끝.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent


def _normalize(item, group):
    missing = {"name", "ticker", "currency"} - set(item)
    if missing:
        raise ValueError(f"holdings.json 항목에 {missing} 없음: {item}")
    item.setdefault("keywords", [item["name"]])
    item.setdefault("query", item["name"])
    item["market"] = "국내" if item["currency"] == "KRW" else "해외"
    item["group"] = group
    # 관심 종목은 평소 뉴스를 안 보고, 보유 종목은 news:false일 때만 건너뛴다.
    item["news"] = bool(item.get("news", group == "holding"))
    return item


def load():
    payload = json.loads((HERE / "holdings.json").read_text(encoding="utf-8"))
    held = [_normalize(item, "holding") for item in payload.get("holdings", [])]
    watched = [_normalize(item, "watch") for item in payload.get("watchlist", [])]
    seen = {item["ticker"] for item in held}
    # 보유와 관심에 같은 종목이 있으면 보유 쪽만 남긴다.
    watched = [item for item in watched if item["ticker"] not in seen]
    return held, watched


HOLDINGS, WATCHLIST = load()
ALL = HOLDINGS + WATCHLIST
NEWS_HOLDINGS = [item for item in HOLDINGS if item["news"]]
KOREA = [item for item in HOLDINGS if item["market"] == "국내"]
OVERSEAS = [item for item in HOLDINGS if item["market"] == "해외"]
