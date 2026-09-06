"""종목 목록을 한 곳에서 읽는다. 추가·삭제는 holdings.json만 고치면 된다.

holdings  = 보유 종목. 가격·지표에 더해 뉴스까지 수집한다.
watchlist = 관심 종목. 가격·지표만 보고 뉴스는 수집하지 않는다.
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
KOREA = [item for item in HOLDINGS if item["market"] == "국내"]
OVERSEAS = [item for item in HOLDINGS if item["market"] == "해외"]
