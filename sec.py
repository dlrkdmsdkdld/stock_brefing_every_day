"""SEC EDGAR 공시를 감시해 새로 올라온 것만 텔레그램으로 보낸다.

  Form 4   내부자 거래. 거래 후 2영업일 내 제출이라 가장 시의성이 높다.
  8-K      실적·인수·경영진 교체·소송 등 중요 사건. 4영업일 내.
  13F-HR   기관 분기 보유 내역. 분기 종료 후 45일 이내라 이미 지난 정보지만,
           발표 당일 시장이 반응하므로 따라간다. 전분기 대비 신규매수·청산을 함께 보여준다.

모델을 쓰지 않으므로 토큰이 들지 않는다. 이미 보낸 공시는 seen.json에 남겨 두 번 보내지 않는다.

환경변수
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   없으면 화면에만 출력한다.
  SEC_USER_AGENT   SEC는 연락처가 든 User-Agent를 요구한다.
"""
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from holdings import ALL

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
SEEN = HERE / "seen.json"
CONFIG = HERE / "filings.json"
AGENT = {"User-Agent": os.getenv("SEC_USER_AGENT", "stock-brief silvia@brainupapp.com")}
ATOM = {"a": "http://www.w3.org/2005/Atom"}
# SEC는 초당 10회로 제한한다. 넉넉히 벌려 둔다.
PAUSE = 0.15
# 13F는 분기마다 나오므로 접수일 기준으로 정리하면 지난 분기 건이 바로 지워져
# 매번 다시 알림이 나간다. 그래서 '언제 확인했는지'를 기준으로 오래 보관한다.
KEEP_DAYS = 400
KEEP_MAX = 5000
MAX_NOTIFY = 12


def get(url, retries=3):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=AGENT), timeout=25) as body:
                return body.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def load_seen():
    if SEEN.exists():
        return json.loads(SEEN.read_text(encoding="utf-8"))
    return {"ids": {}}


def save_seen(data):
    """확인 시각 기준으로 오래된 것만 버린다. 파일이 무한정 커지지 않게 개수도 제한한다."""
    cutoff = (datetime.now(KST) - timedelta(days=KEEP_DAYS)).date().isoformat()
    kept = {key: day for key, day in data["ids"].items() if day >= cutoff}
    if len(kept) > KEEP_MAX:
        newest = sorted(kept.items(), key=lambda pair: pair[1], reverse=True)[:KEEP_MAX]
        kept = dict(newest)
    data["ids"] = kept
    data["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    SEEN.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ticker_map():
    """내 종목만 CIK로 바꾼다. 국내 종목은 SEC 대상이 아니다."""
    data = json.loads(get("https://www.sec.gov/files/company_tickers.json"))
    lookup = {row["ticker"]: int(row["cik_str"]) for row in data.values()}
    mine = {}
    for item in ALL:
        if item["currency"] == "KRW":
            continue
        cik = lookup.get(item["ticker"])
        if cik:
            mine[cik] = item
    return mine


def company_filings(cik, forms):
    """한 회사의 최근 제출물. data.sec.gov는 접수 순으로 돌려준다."""
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    payload = json.loads(get(url))
    recent = payload["filings"]["recent"]
    rows = []
    for index, form in enumerate(recent["form"]):
        if form not in forms:
            continue
        rows.append(dict(form=form, date=recent["filingDate"][index],
                         accession=recent["accessionNumber"][index],
                         doc=recent["primaryDocument"][index],
                         desc=(recent.get("primaryDocDescription") or [""] * len(recent["form"]))[index],
                         company=payload["name"], cik=cik))
    return rows


def filing_url(row):
    plain = row["accession"].replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{plain}/"
            f"{row['doc'] or row['accession'] + '-index.htm'}")


def insider_detail(row):
    """Form 4 본문에서 누가 얼마나 사고팔았는지 뽑는다."""
    plain = row["accession"].replace("-", "")
    url = (f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{plain}/"
           f"{row['accession']}.txt")
    try:
        text = get(url).decode("utf-8", "replace")
    except Exception:
        return {}
    who = re.search(r"<rptOwnerName>([^<]+)</rptOwnerName>", text)
    title = re.search(r"<officerTitle>([^<]+)</officerTitle>", text)
    detail = {"who": (who.group(1).strip() if who else None),
              "title": (title.group(1).strip() if title else None)}
    buys = sells = 0.0
    for block in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
                            text, re.S):
        code = re.search(r"<transactionAcquiredDisposedCode>.*?<value>([AD])</value>",
                         block, re.S)
        shares = re.search(r"<transactionShares>.*?<value>([\d.]+)</value>", block, re.S)
        price = re.search(r"<transactionPricePerShare>.*?<value>([\d.]+)</value>", block, re.S)
        if not (code and shares):
            continue
        amount = float(shares.group(1)) * (float(price.group(1)) if price else 0)
        if code.group(1) == "A":
            buys += amount
        else:
            sells += amount
    detail["buy_value"] = round(buys)
    detail["sell_value"] = round(sells)
    return detail


def holdings_from_13f(row):
    """13F 정보표(XML)에서 종목별 평가액을 뽑는다."""
    plain = row["accession"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{plain}"
    index = json.loads(get(f"{base}/index.json"))
    target = None
    for item in index["directory"]["item"]:
        name = item["name"].lower()
        if name.endswith(".xml") and "primary_doc" not in name:
            target = item["name"]
            break
    if not target:
        return {}
    text = get(f"{base}/{target}").decode("utf-8", "replace")
    positions = {}
    for block in re.findall(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", text, re.S):
        name = re.search(r"<(?:\w+:)?nameOfIssuer>([^<]+)<", block)
        value = re.search(r"<(?:\w+:)?value>([\d.]+)<", block)
        if name and value:
            key = name.group(1).strip()
            positions[key] = positions.get(key, 0) + float(value.group(1))
    return positions


def compare_13f(row, previous):
    """전분기 대비 신규 매수와 청산을 추린다."""
    now = holdings_from_13f(row)
    if not now or not previous:
        return None
    before = holdings_from_13f(previous)
    if not before:
        return None
    added = sorted(((name, size) for name, size in now.items() if name not in before),
                   key=lambda pair: -pair[1])[:5]
    dropped = sorted(((name, size) for name, size in before.items() if name not in now),
                     key=lambda pair: -pair[1])[:5]
    return dict(total=round(sum(now.values())), count=len(now),
                added=added, dropped=dropped)


def money(value):
    if value >= 1e9:
        return f"${value / 1e9:,.2f}B"
    if value >= 1e6:
        return f"${value / 1e6:,.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:,.0f}K"
    return f"${value:,.0f}"


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def scan_companies(seen, config):
    """내 보유·관심 종목의 Form 4 / 8-K 중 처음 보는 것만 고른다."""
    forms = set(config.get("watch_forms", []))
    if not forms:
        return []
    mine = ticker_map()
    today = datetime.now(KST).date()
    window = (today - timedelta(days=3)).isoformat()
    fresh = []
    for cik, item in mine.items():
        time.sleep(PAUSE)
        try:
            rows = company_filings(cik, forms)
        except Exception as exc:
            print(f"[경고] {item['ticker']} 조회 실패: {type(exc).__name__}", file=sys.stderr)
            continue
        for row in rows:
            # 며칠 지난 공시는 처음 켰을 때 쏟아지지 않도록 잘라 낸다.
            if row["date"] < window or row["accession"] in seen["ids"]:
                continue
            row["item"] = item
            fresh.append(row)
    fresh.sort(key=lambda row: row["date"], reverse=True)
    return fresh


def scan_investors(seen, config):
    """지정한 기관의 새 13F를 고르고 전분기와 비교한다."""
    fresh = []
    for investor in config.get("investors", []):
        cik = int(investor["cik"])
        time.sleep(PAUSE)
        try:
            rows = company_filings(cik, {"13F-HR", "13F-HR/A"})
        except Exception as exc:
            print(f"[경고] {investor['name']} 조회 실패: {type(exc).__name__}", file=sys.stderr)
            continue
        if not rows or rows[0]["accession"] in seen["ids"]:
            continue
        row = rows[0]
        row["investor"] = investor["name"]
        try:
            row["diff"] = compare_13f(row, rows[1] if len(rows) > 1 else None)
        except Exception:
            row["diff"] = None
        fresh.append(row)
    return fresh


def company_message(row):
    item = row["item"]
    label = {"4": "내부자 거래", "8-K": "중요 사건"}.get(row["form"], row["form"])
    lines = [f"<b>📄 {esc(item['name'])} ({esc(item['ticker'])}) · {label}</b>",
             f"{row['form']} · 접수 {row['date']}"]
    if row["form"] == "4":
        detail = insider_detail(row)
        who = detail.get("who")
        title = detail.get("title")
        if who:
            lines.append(f"{esc(who)}{f' · {esc(title)}' if title else ''}")
        buy, sell = detail.get("buy_value", 0), detail.get("sell_value", 0)
        if buy or sell:
            parts = []
            if buy:
                parts.append(f"취득 {money(buy)}")
            if sell:
                parts.append(f"처분 {money(sell)}")
            lines.append(" · ".join(parts))
    elif row.get("desc"):
        lines.append(esc(row["desc"])[:120])
    lines.append(f'<a href="{filing_url(row)}">공시 원문</a>')
    return "\n".join(lines)


def investor_message(row):
    lines = [f"<b>🏛 {esc(row['investor'])} · 13F 제출</b>",
             f"{row['form']} · 접수 {row['date']}"]
    diff = row.get("diff")
    if diff:
        lines.append(f"보유 {diff['count']}종목 · 평가액 {money(diff['total'] * 1000)}")
        if diff["added"]:
            lines.append("신규: " + ", ".join(
                f"{esc(name)} {money(size * 1000)}" for name, size in diff["added"][:4]))
        if diff["dropped"]:
            lines.append("청산: " + ", ".join(
                f"{esc(name)}" for name, _ in diff["dropped"][:4]))
    else:
        lines.append("전분기 비교 불가 (정보표를 읽지 못함)")
    lines.append("<i>13F는 분기말 기준이라 최대 45일 지난 내역입니다.</i>")
    lines.append(f'<a href="{filing_url(row)}">공시 원문</a>')
    return "\n".join(lines)


def send(messages):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        print("[알림] 텔레그램 설정이 없어 화면에만 출력합니다.")
        for text in messages:
            print("---\n" + re.sub(r"<[^>]+>", "", text))
        return
    for text in messages:
        payload = json.dumps(dict(chat_id=chat, text=text, parse_mode="HTML",
                                  disable_web_page_preview=True)).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(request, timeout=30)
        time.sleep(0.4)


def main():
    # notify.py와 같은 .env를 쓴다.
    try:
        from notify import load_env
        load_env()
    except Exception:
        pass

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    seen = load_seen()
    dry = "--dry-run" in sys.argv

    companies = scan_companies(seen, config)
    investors = scan_investors(seen, config)
    print(f"새 공시 · 종목 {len(companies)}건 · 13F {len(investors)}건")

    messages, marked = [], []
    for row in investors:                      # 13F를 먼저 보낸다. 건수가 적고 중요도가 높다.
        messages.append(investor_message(row))
        marked.append(row)
    for row in companies[:MAX_NOTIFY]:
        messages.append(company_message(row))
        marked.append(row)
    if len(companies) > MAX_NOTIFY:
        messages.append(f"<i>이 밖에 {len(companies) - MAX_NOTIFY}건이 더 있습니다. "
                        f"너무 많아 생략했습니다.</i>")
        marked.extend(companies[MAX_NOTIFY:])

    if not messages:
        print("보낼 새 공시가 없습니다.")
        return 0
    if dry:
        for text in messages:
            print("---\n" + re.sub(r"<[^>]+>", "", text))
        print(f"\n[모의 실행] {len(messages)}건 · seen.json을 갱신하지 않았습니다.")
        return 0

    send(messages)
    today = datetime.now(KST).date().isoformat()
    for row in marked:
        seen["ids"][row["accession"]] = today
    save_seen(seen)
    print(f"전송 완료 {len(messages)}건 · 기록 {len(seen['ids'])}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
