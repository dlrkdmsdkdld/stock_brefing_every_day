"""보유·관심 종목의 최근 거래일 종가와 기술적 지표 조회.

국내: pykrx(KRX)를 1차 출처로 쓰고 Yahoo·네이버로 교차 검증한다.
해외: yfinance(Yahoo) 단일 출처.
"""
import json
import math
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from pykrx import stock

import indicators
from holdings import ALL

AGENT = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
# 정규장 마감 + 종가 확정 여유. 이 시각을 넘겼으면 그날 일봉을 확정된 종가로 쓴다.
SESSION_CLOSE = {"Asia/Seoul": time(15, 40), "America/New_York": time(16, 15)}
# RSI(14)와 볼린저밴드(20)를 안정적으로 계산하려면 거래일이 넉넉해야 한다.
HISTORY_DAYS = 150


def cutoff_date(zone):
    """이 날짜부터는 일봉을 쓰지 않는다.

    장중 값을 종가로 오인하지 않는 것이 목적이지, 마감된 종가를 버리는 것이 아니다.
    그래서 시장이 이미 닫혔으면 그날 일봉까지 포함한다. 예를 들어 06:30 KST 실행은
    뉴욕 기준 전날 17:30이라, 16:00에 끝난 그날 종가가 이미 확정돼 있다.
    """
    now = datetime.now(ZoneInfo(zone))
    closed = now.time() >= SESSION_CLOSE[zone]
    return now.date() + timedelta(days=1) if closed else now.date()


def closed_frame(frame, cutoff):
    """아직 안 끝난 세션의 일봉을 잘라내고 날짜순으로 정렬한다."""
    frame = frame.loc[[index.date() < cutoff for index in frame.index]]
    if frame.empty:
        raise ValueError(f"최근 {HISTORY_DAYS}일 내 완료 거래일 데이터 없음")
    return frame.sort_index()


def latest(frame, column, cutoff):
    frame = closed_frame(frame, cutoff)
    value = float(frame.iloc[-1][column])
    if not math.isfinite(value) or value <= 0:
        raise ValueError("최신 가격이 비정상: 이전 가격으로 대체하지 않음")
    previous = float(frame.iloc[-2][column]) if len(frame) > 1 else float("nan")
    if not math.isfinite(previous) or previous <= 0:
        previous = None
    return frame.index[-1].date(), value, previous


def yahoo_close(symbol, start, cutoff):
    security = yf.Ticker(symbol)
    frame = security.history(start=start.isoformat(), end=cutoff.isoformat(),
                             auto_adjust=False, actions=False, timeout=20)
    return security.get_history_metadata(), latest(frame, "Close", cutoff), frame


def series(frame, column, cutoff):
    """지표 계산용 종가 리스트. 종가와 같은 데이터에서 뽑아 기준을 맞춘다."""
    return [float(value) for value in closed_frame(frame, cutoff)[column]]


def korean_market_cap(ticker):
    """국내 시가총액. 네이버가 표시하는 값(보통주 기준)을 쓴다.

    pykrx의 시총 조회는 KRX 로그인이 필요하고, Yahoo의 상장주식수는 우선주까지 포함해
    네이버 표시값보다 10% 남짓 크게 나온다. 사용자가 보는 숫자와 맞추는 쪽을 택했다.
    """
    url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=20) as body:
        payload = json.load(body)
    text = next((row["value"] for row in payload.get("totalInfos", [])
                 if row.get("code") == "marketValue"), None)
    if not text:
        raise ValueError("네이버 시가총액 없음")
    total, cleaned = 0, text.replace(",", "").replace(" ", "")
    for amount, unit in re.findall(r"(\d+)(조|억|만)?", cleaned):
        total += int(amount) * {"조": 10**12, "억": 10**8, "만": 10**4, "": 1}[unit]
    if total <= 0:
        raise ValueError(f"시가총액 파싱 실패: {text}")
    return total, f"네이버 표시값({text})"


def market_cap(symbol, price):
    """시가총액. 상장주식수 x 우리가 쓰는 종가로 계산해 표시 가격과 기준을 맞춘다.

    Yahoo가 주는 marketCap은 최신 체결가 기준이라 종가와 어긋날 수 있어 2순위로 둔다.
    """
    info = yf.Ticker(symbol).fast_info
    try:
        shares = info["shares"]
        if shares:
            return round(float(shares) * price), "상장주식수 x 종가"
    except (KeyError, TypeError):
        pass
    try:
        value = info["marketCap"]
        if value:
            return round(float(value)), "Yahoo marketCap(최신가 기준)"
    except (KeyError, TypeError):
        pass
    raise ValueError("시가총액 정보 없음")


def naver_close(ticker):
    """네이버 금융 일별 시세의 마지막 거래일 종가. 3번째 독립 출처."""
    url = f"https://m.stock.naver.com/api/stock/{ticker}/integration"
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=20) as body:
        payload = json.load(body)
    trend = payload.get("dealTrendInfos") or []
    if not trend:
        raise ValueError("네이버 일별 시세 없음")
    row = trend[0]
    date = datetime.strptime(row["bizdate"], "%Y%m%d").date()
    return payload.get("stockName"), date, float(str(row["closePrice"]).replace(",", ""))


def nxt_close(ticker, previous_close):
    """넥스트레이드(NXT) 최종 체결가. 네이버 종목 페이지의 NXT 탭을 읽는다.

    NXT는 애프터마켓이 20:00까지라 15:30에 끝나는 KRX 종가와 다를 수 있다.
    페이지에 거래일이 없으므로 등락률로 역산한 전일 종가가 KRX 전일 종가와 맞는지 확인해
    같은 거래일 시세임을 검증한다.
    """
    url = f"https://finance.naver.com/item/main.naver?code={ticker}"
    page = urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": AGENT["User-Agent"]}),
        timeout=20).read().decode("utf-8", "replace")
    start = page.find('id="rate_info_nxt"')
    if start < 0:
        raise ValueError("NXT 시세 영역 없음(비대상 종목)")
    block = page[start:start + 2500]
    price = re.search(r'<span class="blind">([\d,]+)</span>', block)
    ratio = re.search(r"([\d.]+)%", re.sub(r"<[^>]+>", " ", block))
    direction = re.search(r'<em class="no_(\w+?)"', block)
    if not (price and ratio and direction):
        raise ValueError("NXT 시세 파싱 실패")
    sign = -1 if direction.group(1).startswith("down") else (0 if direction.group(1).startswith("pre") else 1)
    value = float(price.group(1).replace(",", ""))
    percent = sign * float(ratio.group(1))
    implied = value / (1 + percent / 100) if percent else value
    if not previous_close or abs(implied - previous_close) / previous_close > 0.002:
        raise ValueError(f"거래일 검증 실패: 역산 전일 종가 {implied:,.0f} != KRX {previous_close:,.0f}")
    return round(value, 4), round(percent, 2)


def check(result, key, date, price, other):
    """교차 출처 결과를 기록한다. 실패나 불일치를 숨기지 않는다."""
    try:
        other_name, other_date, other_price = other()
        result[f"{key}_price"] = other_price
        result[f"{key}_name"] = other_name
        result[key] = "match" if other_date == date and abs(price - other_price) < 0.5 else "mismatch"
    except Exception as exc:
        result[key] = "unavailable"
        result[f"{key}_error"] = f"{type(exc).__name__}: {exc}"


def fetch(item):
    name, ticker, currency = item["name"], item["ticker"], item["currency"]
    korea = currency == "KRW"
    zone = "Asia/Seoul" if korea else "America/New_York"
    today = datetime.now(ZoneInfo(zone)).date()
    cutoff = cutoff_date(zone)
    start = today - timedelta(days=HISTORY_DAYS)
    result = dict(name=name, ticker=ticker, currency=currency, group=item["group"],
                  source="pykrx/KRX" if korea else "yfinance/Yahoo Finance",
                  price_type="마감된 최근 거래일 종가(국내는 수정주가 기준)",
                  market_timezone=zone, status="error")
    try:
        if korea:
            # pykrx 1.2.8의 adjusted=False 경로는 KRX 로그인이 필요해 기본(수정주가)을 쓴다.
            frame = stock.get_market_ohlcv_by_date(
                start.strftime("%Y%m%d"), (cutoff - timedelta(days=1)).strftime("%Y%m%d"), ticker)
            date, price, previous = latest(frame, "종가", cutoff)
            result.update(indicators.compute(series(frame, "종가", cutoff)))
            result["provider_name"] = stock.get_market_ticker_name(ticker)
            try:
                result["market_cap"], result["market_cap_source"] = korean_market_cap(ticker)
            except Exception as exc:
                result["market_cap_error"] = f"{type(exc).__name__}: {exc}"
            check(result, "yahoo_check", date, price,
                  lambda: (None,) + yahoo_close(ticker + ".KS", start, cutoff)[1][:2])
            check(result, "naver_check", date, price, lambda: naver_close(ticker))
            # NXT 종가는 참고용으로 덧붙인다. 실패해도 KRX 종가 조회는 그대로 성공 처리한다.
            try:
                result["nxt_price"], result["nxt_change_pct"] = nxt_close(ticker, previous)
                result["nxt_source"] = "네이버 금융 NXT 탭(넥스트레이드)"
            except Exception as exc:
                result["nxt_error"] = f"{type(exc).__name__}: {exc}"
        else:
            metadata, (date, price, previous), frame = yahoo_close(ticker, start, cutoff)
            result.update(indicators.compute(series(frame, "Close", cutoff)))
            if metadata.get("currency") != currency:
                raise ValueError(f"통화 확인 실패: {metadata.get('currency')}")
            result["provider_name"] = metadata.get("longName") or metadata.get("shortName")
            # 표를 시총순으로 정렬하므로 보유·관심 모두 시가총액을 받는다.
            try:
                result["market_cap"], result["market_cap_source"] = market_cap(ticker, price)
            except Exception as exc:
                result["market_cap_error"] = f"{type(exc).__name__}: {exc}"
        result.update(price=round(price, 4), date=date.isoformat(), status="ok")
        # 직전 거래일 대비 등락률. 이전 값이 없으면 채우지 않고 비워 둔다.
        if previous:
            result["previous_close"] = round(previous, 4)
            result["change"] = round(price - previous, 4)
            result["change_pct"] = round((price - previous) / previous * 100, 2)
        if (today - date).days > 7:
            result["status"] = "stale"
        if "mismatch" in (result.get("yahoo_check"), result.get("naver_check")):
            result["status"] = "mismatch"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main():
    yf.set_tz_cache_location(str(Path(__file__).parent / ".cache" / "yfinance"))
    # 국내 요청은 순차 실행하여 과도한 호출을 피한다.
    results = [fetch(item) for item in ALL if item["currency"] == "KRW"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results.extend(pool.map(fetch, [item for item in ALL if item["currency"] != "KRW"]))
    output = Path(__file__).parent / "prices.json"
    output.write_text(json.dumps(dict(
        fetched_at=datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        note="당일 일봉 제외. 실시간/시간외 가격 아님. 7일 초과 데이터는 stale 표시.",
        holdings=results), ensure_ascii=False, indent=2), encoding="utf-8")
    for row in results:
        price = f"{row['price']:,.2f}" if "price" in row else "조회 실패"
        checks = "/".join(row[key] for key in ("yahoo_check", "naver_check") if key in row) or "-"
        print(f"{row['name']} ({row['ticker']}) | {price} {row['currency']} | "
              f"{row.get('date', '-')} | {row['status']} | {checks}"
              + (f" | {row['error']}" if "error" in row else ""))
    print(f"저장: {output}")
    return 1 if any(row["status"] != "ok" for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
