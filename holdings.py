"""보유 종목 목록을 한 곳에서 읽는다. 종목 추가·삭제는 holdings.json만 고치면 된다."""
import json
from pathlib import Path

HERE = Path(__file__).parent


def load():
    payload = json.loads((HERE / "holdings.json").read_text(encoding="utf-8"))
    holdings = payload["holdings"]
    for item in holdings:
        missing = {"name", "ticker", "currency"} - set(item)
        if missing:
            raise ValueError(f"holdings.json 항목에 {missing} 없음: {item}")
        item.setdefault("keywords", [item["name"]])
        item.setdefault("query", item["name"])
        item["market"] = "국내" if item["currency"] == "KRW" else "해외"
    return holdings


HOLDINGS = load()
KOREA = [item for item in HOLDINGS if item["market"] == "국내"]
OVERSEAS = [item for item in HOLDINGS if item["market"] == "해외"]
