"""완성된 브리핑을 텔레그램으로 보낸다.

환경변수
  TELEGRAM_BOT_TOKEN  BotFather에서 받은 봇 토큰 (필수). .env 파일에 적어둬도 된다.
  TELEGRAM_CHAT_ID    받을 사람의 chat id (필수). --whoami로 확인할 수 있다.
  SEND_HTML           "0"이면 briefing.html 첨부를 건너뛴다.
  BRIEF_URL           브리핑 웹페이지 주소. 첫 메시지 맨 위에 링크로 붙는다.
                      비워 두면 GitHub Actions의 저장소 정보로 Pages 주소를 유추한다.

텔레그램 메시지는 4096자 제한이라 줄 단위로 잘라 여러 번 보내고,
전문을 한 번에 보려면 briefing.html을 파일로 함께 보낸다.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
CHUNK = 3500
API = "https://api.telegram.org/bot{token}/{method}"


def brief_url():
    """브리핑 웹페이지 주소. 직접 지정한 값이 우선."""
    explicit = os.getenv("BRIEF_URL", "").strip()
    if explicit:
        return explicit
    # GitHub Actions에서는 저장소 이름으로 Pages 주소를 만들 수 있다.
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if "/" in repository:
        owner, name = repository.split("/", 1)
        return f"https://{owner}.github.io/{name}/"
    return ""


def load_env():
    """로컬 실행 편의를 위해 .env가 있으면 읽는다. 이미 설정된 환경변수가 우선."""
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def call(method, payload, token):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(API.format(token=token, method=method), data=data,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as body:
        return json.load(body)


def send_document(path, caption, token, chat_id):
    """multipart/form-data를 손으로 만들어 파일을 올린다(외부 의존성 없이)."""
    boundary = uuid.uuid4().hex
    parts = []
    for key, value in (("chat_id", chat_id), ("caption", caption)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                     f"{value}\r\n".encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="document"; '
                 f'filename="{path.name}"\r\nContent-Type: text/html\r\n\r\n'.encode())
    parts.append(path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        API.format(token=token, method="sendDocument"), data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=60) as body:
        return json.load(body)


def tech(row):
    """RSI와 볼린저밴드 위치를 한 줄에 짧게."""
    if "rsi" not in row:
        return ""
    parts = [f"RSI {row['rsi']:.0f}"]
    if row.get("rsi_zone") != "중립":
        parts[0] += f"({row['rsi_zone']})"
    band = row.get("bb_position")
    if band and band != "밴드 내":
        parts.append(f"BB {band}")
    return "  " + " · ".join(parts)


def cap_key(row):
    return row.get("market_cap") or -1


def cap_text(row):
    value = row.get("market_cap")
    if value is None:
        return ""
    if row["currency"] == "KRW":
        return f"{value / 1e12:,.1f}조" if value >= 1e12 else f"{value / 1e8:,.0f}억"
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if value >= size:
            return f"${value / size:,.2f}{unit}"
    return f"${value:,.0f}"


def delta(row):
    value = row.get("change")
    if value is None:
        return ""
    sign = "+" if value > 0 else "-" if value < 0 else ""
    size = abs(value)
    return f"{sign}₩{size:,.0f}" if row["currency"] == "KRW" else f"{sign}${size:,.2f}"


def money(row):
    price = row.get("price")
    if price is None:
        return "조회 실패"
    return f"₩{price:,.0f}" if row["currency"] == "KRW" else f"${price:,.2f}"


def move(row):
    percent = row.get("change_pct")
    if percent is None:
        return ""
    return f"{'▲' if percent > 0 else '▼' if percent < 0 else '-'}{percent:+.2f}%"


def build(prices, news, judged):
    """텔레그램에 보낼 본문. 가격 표와 종목별 뉴스 요약을 모두 담는다."""
    every = prices["holdings"]
    holdings = [row for row in every if row.get("group", "holding") == "holding"]
    watch = [row for row in every if row.get("group") == "watch"]
    by_name = {row["name"]: row for row in holdings}
    scored = [row["change_pct"] for row in holdings if row.get("change_pct") is not None]
    up = sum(1 for value in scored if value > 0)
    down = sum(1 for value in scored if value < 0)
    average = sum(scored) / len(scored) if scored else 0
    trade_dates = sorted({row["date"] for row in holdings if "date" in row})
    best = max(holdings, key=lambda r: r.get("change_pct") or -999)
    worst = min(holdings, key=lambda r: r.get("change_pct") if r.get("change_pct") is not None else 999)

    link = brief_url()
    lines = [f"<b>📊 포트폴리오 브리핑 {datetime.now(KST):%Y-%m-%d (%a)}</b>"]
    if link:
        # 표·차트까지 편하게 보려면 웹페이지가 낫다. 맨 위에 둔다.
        lines.append(f'🔗 <a href="{esc(link)}">브리핑 전문 웹페이지 열기</a>')
    lines += [
             f"종가 기준일 {' / '.join(trade_dates) or '없음'} · 오늘 뉴스 {news['counts']['기사']}건", "",
             f"상승 {up} · 하락 {down} · 평균 {average:+.2f}%",
             f"최고 {esc(best['name'])} {best.get('change_pct', 0):+.2f}% / "
             f"최저 {esc(worst['name'])} {worst.get('change_pct', 0):+.2f}%", ""]

    for market, currency in (("국내", "KRW"), ("해외", "USD")):
        rows = [row for row in holdings if row["currency"] == currency]
        if not rows:
            continue
        lines.append(f"<b>[{market} · 시총순]</b>")
        for row in sorted(rows, key=cap_key, reverse=True):
            extra = (f"  (NXT ₩{row['nxt_price']:,.0f} {row['nxt_change_pct']:+.2f}%)"
                     if "nxt_price" in row else "")
            lines.append(f"{esc(row['name'])} <code>{cap_text(row)}</code>  {money(row)}  "
                         f"{move(row)}{extra}{tech(row)}")
        lines.append("")

    path = HERE / "recommendation.json"
    if path.exists():
        pick = json.loads(path.read_text(encoding="utf-8"))
        if pick.get("generated_at", "")[:10] == datetime.now(KST).date().isoformat():
            choice = pick["pick"]
            group = "보유" if choice.get("group", "holding") == "holding" else "관심"
            lines += [f"<b>⭐ 오늘의 추천 · {esc(choice['name'])} ({esc(choice['ticker'])}, {group})</b>",
                      esc(choice["headline"]), esc(choice["reason"]),
                      f"유의: {esc(choice['risk'])}", ""]

    spots = [
        ("상승", sorted([r for r in every if (r.get("change_pct") or 0) > 0],
                        key=lambda r: -r["change_pct"])[:3], lambda r: f"{r['change_pct']:+.2f}%"),
        ("하락", sorted([r for r in every if (r.get("change_pct") or 0) < 0],
                        key=lambda r: r["change_pct"])[:3], lambda r: f"{r['change_pct']:+.2f}%"),
        ("거래량 급증", sorted([r for r in every if r.get("volume_ratio", 0) >= 1.5],
                           key=lambda r: -r["volume_ratio"])[:3],
         lambda r: f"{r['volume_ratio']:.1f}배"),
        ("52주 신고가 근접", sorted([r for r in every if (r.get("from_year_high") or -99) >= -3],
                              key=lambda r: -r["from_year_high"])[:3],
         lambda r: f"{r['from_year_high']:+.1f}%"),
        ("52주 신저가 근접", sorted([r for r in every if (r.get("from_year_low") or 99) <= 5],
                              key=lambda r: r["from_year_low"])[:3],
         lambda r: f"{r['from_year_low']:+.1f}%"),
    ]
    shown = [(title, rows, render) for title, rows, render in spots if rows]
    if shown:
        lines.append("<b>🔥 오늘의 주목</b>")
        for title, rows, render in shown:
            body = ", ".join(f"{esc(r['name'])} {render(r)}" for r in rows)
            lines.append(f"{title}: {body}")
        lines.append("")

    if watch:
        lines.append(f"<b>[관심 종목 {len(watch)} · 시총순]</b>")
        for row in sorted(watch, key=cap_key, reverse=True):
            lines.append(f"{esc(row['name'])} <code>{cap_text(row)}</code>  {money(row)}  "
                         f"{delta(row)}  {move(row)}{tech(row)}")
        lines.append("")

    lines.append("<b>📰 종목별 오늘의 뉴스</b>")
    for name, row in news["news"].items():
        price = by_name.get(name, {})
        lines += ["", f"<b>▪ {esc(name)} {move(price)}</b>"]
        if not row["stories"]:
            lines.append("  오늘자 기사 없음")
            continue
        for story in row["stories"]:
            verdict = judged.get(normalize(story["title"]))
            stance = f"[{verdict['stance']}] " if verdict else "[판단보류] "
            lines.append(f"· {stance}{esc(story['title'])}")
            if verdict:
                lines.append(f"  {esc(verdict.get('summary', ''))}")
                lines.append(f"  → {esc(verdict.get('impact', ''))}")
    lines += ["", "<i>RSI(14) 와일더 방식 · 볼린저밴드 20일·2σ. "
              "실시간 가격이 아니며 투자 자문이 아닙니다.</i>"]
    if link:
        # 메시지가 여러 건으로 쪼개지므로 맨 아래에도 링크를 둔다.
        lines += ["", f'🔗 <a href="{esc(link)}">브리핑 전문 웹페이지 열기</a>']
    return lines


def normalize(title):
    import re
    import html as html_module
    return re.sub(r"[^0-9a-z가-힣]", "", html_module.unescape(title or "").lower())


def chunks(lines):
    """4096자 제한에 맞춰 줄 단위로 나눈다. 한 줄이 너무 길면 그 줄만 잘라낸다."""
    block, size = [], 0
    for line in lines:
        line = line if len(line) <= CHUNK else line[:CHUNK - 3] + "..."
        if size + len(line) + 1 > CHUNK and block:
            yield "\n".join(block)
            block, size = [], 0
        block.append(line)
        size += len(line) + 1
    if block:
        yield "\n".join(block)


def main():
    load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN 없음")

    if "--whoami" in sys.argv:
        # 봇에게 아무 메시지나 한 번 보낸 뒤 실행하면 chat id를 알려준다.
        updates = call("getUpdates", {}, token).get("result", [])
        if not updates:
            sys.exit("받은 메시지가 없습니다. 텔레그램에서 봇에게 아무 말이나 먼저 보내세요.")
        for update in updates:
            chat = (update.get("message") or update.get("channel_post") or {}).get("chat", {})
            if chat:
                print(f"chat_id={chat['id']}  ({chat.get('title') or chat.get('username') or chat.get('first_name')})")
        return 0

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        sys.exit("TELEGRAM_CHAT_ID 없음 (python notify.py --whoami 로 확인)")

    prices = json.loads((HERE / "prices.json").read_text(encoding="utf-8"))
    news = json.loads((HERE / "news.json").read_text(encoding="utf-8"))
    verdicts_path = HERE / "verdicts.json"
    judged = (json.loads(verdicts_path.read_text(encoding="utf-8")).get("verdicts", {})
              if verdicts_path.exists() else {})

    sent = 0
    for part in chunks(build(prices, news, judged)):
        call("sendMessage", dict(chat_id=chat_id, text=part, parse_mode="HTML",
                                 disable_web_page_preview=True), token)
        sent += 1

    page = HERE / "briefing.html"
    if page.exists() and os.getenv("SEND_HTML", "1") != "0":
        try:
            send_document(page, "브리핑 전문 (열어서 보세요)", token, chat_id)
        except Exception as exc:
            print(f"[경고] briefing.html 첨부 실패: {type(exc).__name__}: {exc}", file=sys.stderr)

    print(f"텔레그램 전송 완료: 메시지 {sent}건 + 브리핑 파일")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as error:
        sys.exit(f"텔레그램 API 오류 {error.code}: {error.read().decode('utf-8', 'replace')[:300]}")
