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
import html
import json
import os
import re
import sys
import time
import urllib.error
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
# 한꺼번에 많이 보낼 때 텔레그램이 429로 막는다. 간격을 두고, 막히면 알려 준 만큼 기다린다.
SEND_GAP = 1.2
SEND_RETRY = 4


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
                         items=(recent.get("items") or [""] * len(recent["form"]))[index],
                         desc=(recent.get("primaryDocDescription") or [""] * len(recent["form"]))[index],
                         company=payload["name"], cik=cik))
    return rows


def filing_url(row):
    plain = row["accession"].replace("-", "")
    return (f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{plain}/"
            f"{row['doc'] or row['accession'] + '-index.htm'}")


# Form 4 거래 코드. 같은 '취득'이라도 성격이 전혀 다르다.
# P/S만 본인 판단으로 시장에서 사고판 것이고, A는 회사가 준 것, F는 세금 원천징수라
# 자발적 매매가 아니다. 이걸 뭉뚱그리면 "CEO가 샀다"는 잘못된 신호가 된다.
TRADE_CODE = {
    "P": ("매수", "장내 매수"),
    "S": ("매도", "장내 매도"),
    "A": ("보상 수령", "주식 보상 수령"),
    "F": ("세금 납부", "세금 원천징수용 주식 인도"),
    "M": ("옵션 행사", "옵션·RSU 행사로 취득"),
    "G": ("증여", "증여"),
    "C": ("전환", "전환"),
    "X": ("옵션 행사", "옵션 행사"),
    "D": ("회사 반환", "회사에 반환"),
}
SIGNAL_CODES = ("P", "S")     # 이 둘만 매매 신호로 본다

# 8-K 항목 코드. EDGAR가 공시마다 알려주므로 이것만으로도 무슨 일인지 대충 잡힌다.
ITEM_LABEL = {
    "1.01": "중요 계약 체결", "1.02": "중요 계약 종료", "1.03": "파산·법정관리",
    "1.05": "사이버 보안 사고",
    "2.01": "자산 인수·매각 완료", "2.02": "실적 발표", "2.03": "채무 발생",
    "2.04": "채무 조기 상환 의무", "2.05": "구조조정 비용", "2.06": "자산 손상차손",
    "3.01": "상장 규정 위반·상장폐지", "3.02": "미등록 주식 발행", "3.03": "주주 권리 변경",
    "4.01": "회계법인 교체", "4.02": "과거 재무제표 신뢰 불가",
    "5.01": "경영권 변동", "5.02": "임원·이사 변동", "5.03": "정관 변경",
    "5.07": "주주총회 결과", "5.08": "주주 제안",
    "7.01": "정보 공개(Reg FD)", "8.01": "기타 중요 사항", "9.01": "재무제표·첨부자료",
}
# 주가에 영향이 큰 항목. 이게 있으면 본문 요약까지 받는다.
ITEM_KEY = {"1.01", "1.02", "1.03", "1.05", "2.01", "2.02", "2.05", "2.06",
            "3.01", "4.01", "4.02", "5.01", "5.02"}
BODY_CHARS = 3500


def item_labels(raw):
    """8-K 항목 코드를 한국어 이름으로. 모르는 코드는 번호 그대로 둔다."""
    codes = [code.strip() for code in (raw or "").split(",") if code.strip()]
    return codes, [f"{code} {ITEM_LABEL[code]}" if code in ITEM_LABEL else code
                   for code in codes]


def document_text(row):
    """공시 본문을 텍스트로. 표지·법적 문구가 많아 뒤쪽 실제 내용까지 넉넉히 가져온다."""
    plain = row["accession"].replace("-", "")
    url = (f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{plain}/"
           f"{row['doc']}")
    page = get(url).decode("utf-8", "replace")
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", page, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text)).strip()
    # 표지 상단(회사 주소·CIK 등)은 잘라 내고 실제 항목부터 본다.
    start = re.search(r"Item\s+\d+\.\d+", text)
    if start:
        text = text[start.start():]
    return text[:BODY_CHARS]


def explain(row, labels):
    """8-K 본문을 한국어로 풀어 준다. 법률 영어라 그대로 읽기 어렵다."""
    try:
        from summarize import SCHEMA_ONLY_MARKER, ask, load_env, providers
        load_env()          # sec.py를 단독 실행할 때도 .env의 키를 읽게 한다
    except Exception as exc:
        print(f"[경고] 요약 모듈 로드 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    chain = providers()
    if not chain:
        print("[알림] 제공처가 없어 8-K 요약을 건너뜁니다.", file=sys.stderr)
        return None
    try:
        body = document_text(row)
    except Exception as exc:
        print(f"[경고] 본문 조회 실패 {row['item']['ticker']}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None
    if len(body) < 200:
        print(f"[알림] {row['item']['ticker']} 본문이 {len(body)}자뿐이라 요약을 건너뜁니다.",
              file=sys.stderr)
        return None

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            "impact": {"type": "string"},
        },
        "required": ["summary", "impact"],
        "additionalProperties": False,
    }
    instructions = (
        "너는 미국 기업 공시(8-K)를 한국 개인 투자자에게 풀어 준다.\n"
        "- summary: 무슨 일이 있었는지 2~3문장. 본문에 나온 이름·날짜·금액을 그대로 인용한다.\n"
        "- impact: 이 종목 주가에 어떤 의미인지 1~2문장. 단정하지 말고 근거를 들어 말한다.\n"
        "규칙: 본문에 없는 내용을 지어내지 않는다. 표지나 법적 상용구뿐이고 실질 내용이 없으면 "
        "summary에 그렇게 적는다. 매수·매도를 권하지 않는다. 한국어로 쓴다.")
    payload = (f"회사: {row['item']['name']} ({row['item']['ticker']})\n"
               f"항목: {', '.join(labels) or '미표기'}\n\n{body}")
    try:
        result, used_in, used_out, _, provider = ask(
            chain, 0, payload, instructions=instructions, schema=schema,
            marker=SCHEMA_ONLY_MARKER)
    except Exception as exc:
        print(f"[경고] 8-K 요약 실패 {row['item']['ticker']}: "
              f"{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return None
    result["usage"] = (used_in, used_out)
    result["provider"] = provider
    return result


def insider_detail(row):
    """Form 4 본문에서 누가 어떤 성격으로 얼마나 거래했는지 뽑는다."""
    plain = row["accession"].replace("-", "")
    url = (f"https://www.sec.gov/Archives/edgar/data/{row['cik']}/{plain}/"
           f"{row['accession']}.txt")
    try:
        text = get(url).decode("utf-8", "replace")
    except Exception:
        return {}
    who = re.search(r"<rptOwnerName>([^<]+)</rptOwnerName>", text)
    title = re.search(r"<officerTitle>([^<]+)</officerTitle>", text)
    director = re.search(r"<isDirector>\s*(?:1|true)\s*</isDirector>", text, re.I)
    tenpct = re.search(r"<isTenPercentOwner>\s*(?:1|true)\s*</isTenPercentOwner>", text, re.I)
    detail = {"who": (who.group(1).strip() if who else None),
              "title": (title.group(1).strip() if title else None),
              "is_director": bool(director), "is_ten_percent": bool(tenpct)}

    # 거래 코드별로 주식 수와 금액을 모은다. 가격이 없는 건(보상 수령 등)은 금액이 0이 된다.
    groups = {}
    prices = []
    for block in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
                            text, re.S):
        code = re.search(r"<transactionCode>([A-Z])</transactionCode>", block)
        side = re.search(r"<transactionAcquiredDisposedCode>.*?<value>([AD])</value>",
                         block, re.S)
        shares = re.search(r"<transactionShares>.*?<value>([\d.]+)</value>", block, re.S)
        price = re.search(r"<transactionPricePerShare>.*?<value>([\d.]*)</value>", block, re.S)
        if not (code and shares and side):
            continue
        count = float(shares.group(1))
        unit = float(price.group(1)) if price and price.group(1) else 0.0
        key = code.group(1)
        bucket = groups.setdefault(key, dict(shares=0.0, value=0.0, side=side.group(1)))
        bucket["shares"] += count
        bucket["value"] += count * unit
        if unit:
            prices.append(unit)

    detail["trades"] = [
        dict(code=key, label=TRADE_CODE.get(key, ("기타", key))[0],
             note=TRADE_CODE.get(key, ("기타", f"코드 {key}"))[1],
             side=bucket["side"], shares=round(bucket["shares"]),
             value=round(bucket["value"]))
        for key, bucket in sorted(groups.items(), key=lambda pair: -pair[1]["value"])]
    if prices:
        detail["price_avg"] = round(sum(prices) / len(prices), 2)

    held = re.findall(r"<sharesOwnedFollowingTransaction>.*?<value>([\d.]+)</value>", text, re.S)
    if held:
        detail["shares_after"] = round(float(held[-1]))

    # 장내 매수·매도만 신호로 본다. 보상 수령이나 세금 납부는 본인 판단이 아니다.
    detail["signal"] = {
        key: dict(shares=round(bucket["shares"]), value=round(bucket["value"]))
        for key, bucket in groups.items() if key in SIGNAL_CODES}
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


def who_line(detail):
    """보고자와 직위. 임원인지 이사인지 대주주인지에 따라 무게가 다르다."""
    parts = [esc(detail["who"])] if detail.get("who") else []
    role = []
    if detail.get("title"):
        role.append(esc(detail["title"]))
    if detail.get("is_director"):
        role.append("이사")
    if detail.get("is_ten_percent"):
        role.append("10% 이상 주주")
    if role:
        parts.append(" · ".join(role))
    return " · ".join(parts)


def company_message(row):
    item = row["item"]
    label = {"4": "내부자 거래", "8-K": "중요 사건"}.get(row["form"], row["form"])
    head = f"<b>📄 {esc(item['name'])} ({esc(item['ticker'])}) · {label}</b>"
    lines = []

    if row["form"] == "4":
        detail = insider_detail(row)
        signal = detail.get("signal") or {}
        # 장내 매수/매도가 있으면 그것만으로 제목을 붙인다. 이게 실제 신호다.
        if "P" in signal and "S" in signal:
            head = f"<b>🔵🔴 {esc(item['name'])} ({esc(item['ticker'])}) · 내부자 매수+매도</b>"
        elif "P" in signal:
            head = f"<b>🔵 {esc(item['name'])} ({esc(item['ticker'])}) · 내부자 매수</b>"
        elif "S" in signal:
            head = f"<b>🔴 {esc(item['name'])} ({esc(item['ticker'])}) · 내부자 매도</b>"
        lines.append(f"{row['form']} · 접수 {row['date']}")
        person = who_line(detail)
        if person:
            lines.append(person)

        for trade in detail.get("trades", []):
            mark = {"P": "🔵", "S": "🔴"}.get(trade["code"], "▪")
            price = f" @ ${detail['price_avg']:,.2f}" if (
                trade["code"] in SIGNAL_CODES and detail.get("price_avg")) else ""
            amount = f" · {money(trade['value'])}" if trade["value"] else ""
            lines.append(f"{mark} {trade['label']} {trade['shares']:,}주{price}{amount}")
        if not detail.get("trades"):
            lines.append("거래 내역을 읽지 못했습니다. 원문을 확인하세요.")
        if detail.get("shares_after") is not None:
            lines.append(f"거래 후 보유 {detail['shares_after']:,}주")
        if not signal and detail.get("trades"):
            lines.append("<i>보상 수령·세금 납부 등으로, 본인 판단의 매매가 아닙니다.</i>")
    else:
        codes, labels = item_labels(row.get("items"))
        lines.append(f"{row['form']} · 접수 {row['date']}")
        if labels:
            lines.append("· " + " / ".join(esc(text) for text in labels))
        # 8-K는 항목을 가리지 않고 모두 한국어로 풀어 준다.
        if row["form"] == "8-K":
            told = explain(row, labels)
            if told:
                lines.append(f"\n{esc(told['summary'])}")
                lines.append(f"→ {esc(told['impact'])}")
                row["usage"] = told.get("usage")
        elif row.get("desc"):
            lines.append(esc(row["desc"])[:120])

    lines.append(f'<a href="{filing_url(row)}">공시 원문</a>')
    return head + "\n" + "\n".join(lines)


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
    for order, text in enumerate(messages, 1):
        payload = json.dumps(dict(chat_id=chat, text=text, parse_mode="HTML",
                                  disable_web_page_preview=True)).encode()
        for attempt in range(1, SEND_RETRY + 1):
            try:
                request = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage", data=payload,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(request, timeout=30)
                break
            except urllib.error.HTTPError as error:
                if error.code != 429 or attempt == SEND_RETRY:
                    print(f"[경고] {order}번째 메시지 전송 실패: HTTP {error.code}",
                          file=sys.stderr)
                    break
                # 텔레그램이 몇 초 기다리라고 알려 준다. 그만큼 쉬었다 다시 보낸다.
                try:
                    wait = json.loads(error.read()).get("parameters", {}).get("retry_after", 5)
                except Exception:
                    wait = 5
                print(f"    전송 제한, {wait}초 대기 ({order}/{len(messages)})", file=sys.stderr)
                time.sleep(wait + 1)
            except Exception as exc:
                print(f"[경고] {order}번째 메시지 전송 실패: {type(exc).__name__}",
                      file=sys.stderr)
                break
        time.sleep(SEND_GAP)


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
    for row in companies:
        messages.append(company_message(row))
        marked.append(row)

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
