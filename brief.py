"""prices.json + news.json + verdicts.json을 하나의 포트폴리오 브리핑으로 합친다.

briefing.md(문서)와 briefing.html(웹 페이지)을 같이 만든다.
가격·뉴스 데이터가 없거나 오늘 것이 아니면 각 수집 스크립트를 먼저 실행한다.
"""
import html as html_module
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
CHECK_LABEL = {"match": "일치", "mismatch": "불일치", "unavailable": "확인불가"}
STANCE_TONE = {"호재": "good", "약한 호재": "good", "중립": "neutral",
               "약한 악재": "bad", "악재": "bad"}
# 밴드 상단 이탈은 과열(빨강), 하단 이탈은 과매도(파랑) 쪽으로 읽는다.
BAND_TONE = {"상단 이탈": "up", "하단 이탈": "down", "밴드 내": "flat"}
RSI_TONE = {"과매수": "up", "과매도": "down", "중립": "flat"}


def esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def normalize(title):
    return re.sub(r"[^0-9a-z가-힣]", "", html_module.unescape(title or "").lower())


def load(name, script, today):
    """오늘 만들어진 데이터가 없으면 수집 스크립트를 실행한 뒤 읽는다."""
    path = HERE / name
    fresh = False
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        fresh = payload.get("fetched_at", "")[:10] == today.isoformat()
    if not fresh:
        print(f"[{name} 갱신] {script} 실행 중...", file=sys.stderr)
        subprocess.run([sys.executable, str(HERE / script)], cwd=HERE, check=False)
        payload = json.loads(path.read_text(encoding="utf-8"))
    return payload


def recommendation():
    """오늘의 추천 종목. recommend.py가 만든 결과를 읽는다."""
    path = HERE / "recommendation.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    # 어제 결과가 남아 있으면 오늘 브리핑에 섞이지 않도록 걸러 낸다.
    today = datetime.now(KST).date().isoformat()
    return payload if payload.get("generated_at", "")[:10] == today else None


def verdicts():
    """기사별 요약·호재/악재 판단. Claude가 본문을 읽고 verdicts.json에 남긴 내용."""
    path = HERE / "verdicts.json"
    if not path.exists():
        return {}, None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("verdicts", {}), payload.get("analyst")


def primary_link(story):
    """구글 RSS 링크는 리다이렉트 경유라 원문 매체 링크를 우선한다."""
    for source in ("네이버 금융", "Yahoo Finance", "구글 뉴스 RSS"):
        info = story["sources"].get(source)
        if info and info.get("url"):
            return info["url"]
    return ""


def outlets(story):
    return ", ".join(dict.fromkeys(
        info["publisher"] or source for source, info in story["sources"].items()))


def move(row):
    percent = row.get("change_pct")
    if percent is None:
        return "-", "flat"
    arrow = "▲" if percent > 0 else ("▼" if percent < 0 else "-")
    return f"{arrow} {percent:+.2f}%", ("up" if percent > 0 else "down" if percent < 0 else "flat")


def amount(row):
    price = row.get("price")
    if price is None:
        return "조회 실패"
    return f"₩{price:,.0f}" if row["currency"] == "KRW" else f"${price:,.2f}"


def nxt(row):
    if "nxt_price" in row:
        return f"₩{row['nxt_price']:,.0f} ({row['nxt_change_pct']:+.2f}%)"
    return row.get("nxt_error", "-").split(":")[0] if "nxt_error" in row else "-"


def delta(row):
    """전일 대비 변동액. 통화에 맞춰 표기한다."""
    value = row.get("change")
    if value is None:
        return "-"
    sign = "+" if value > 0 else "-" if value < 0 else ""
    size = abs(value)
    return f"{sign}₩{size:,.0f}" if row["currency"] == "KRW" else f"{sign}${size:,.2f}"


def cap_key(row):
    """시총 내림차순 정렬용. 시총을 못 구한 종목은 맨 뒤로 보낸다."""
    return row.get("market_cap") or -1


def cap_text(row):
    """시가총액 표기. 국내는 조, 해외는 T/B/M 단위로 줄인다."""
    value = row.get("market_cap")
    if value is None:
        return "-"
    if row["currency"] == "KRW":
        return f"{value / 1e12:,.1f}조" if value >= 1e12 else f"{value / 1e8:,.0f}억"
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if value >= size:
            return f"${value / size:,.2f}{unit}"
    return f"${value:,.0f}"


def rsi_text(row):
    if "rsi" not in row:
        return "-"
    zone = "" if row["rsi_zone"] == "중립" else f" {row['rsi_zone']}"
    return f"{row['rsi']:.1f}{zone}"


def band_text(row):
    if "bb_position" not in row:
        return "-"
    return row["bb_position"]


def verification(row):
    checks = [CHECK_LABEL.get(row[key], row[key])
              for key in ("yahoo_check", "naver_check") if key in row]
    return f"KRX 기준 · Yahoo/네이버 {'/'.join(checks)}" if checks else "Yahoo 단일 출처"


# ---------------------------------------------------------------- 마크다운


def table(rows, with_nxt=False):
    head = ["종목 (시가총액)", "KRX 종가" if with_nxt else "종가", "전일 대비"] + (
        ["NXT 종가"] if with_nxt else []) + ["RSI(14)", "볼린저(20,2σ)", "검증"]
    lines = ["| " + " | ".join(head) + " |",
             "| --- | ---: | ---: |" + (" ---: |" if with_nxt else "") + " ---: | --- | --- |"]
    for row in sorted(rows, key=cap_key, reverse=True):
        cells = [f"{row['name']} ({row['ticker']}, {cap_text(row)})", amount(row), move(row)[0]]
        if with_nxt:
            cells.append(nxt(row))
        cells += [rsi_text(row), band_text(row), verification(row)]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def watch_table(rows):
    lines = ["| 종목 | 시가총액 | 종가 | 변동 | 변동률 | RSI(14) | 볼린저(20,2σ) |",
             "| --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for row in sorted(rows, key=cap_key, reverse=True):
        lines.append("| " + " | ".join([
            f"{row['name']} ({row['ticker']})", cap_text(row), amount(row), delta(row),
            move(row)[0], rsi_text(row), band_text(row)]) + " |")
    return lines


def summarize(rows):
    scored = [row for row in rows if row.get("change_pct") is not None]
    if not scored:
        return "등락률을 계산할 수 있는 종목이 없습니다."
    up = [row for row in scored if row["change_pct"] > 0]
    down = [row for row in scored if row["change_pct"] < 0]
    best, worst = max(scored, key=lambda r: r["change_pct"]), min(scored, key=lambda r: r["change_pct"])
    average = sum(row["change_pct"] for row in scored) / len(scored)
    return (f"{len(scored)}종목 중 상승 {len(up)} · 하락 {len(down)} · 보합 "
            f"{len(scored) - len(up) - len(down)}, 단순 평균 {average:+.2f}%. "
            f"최고 {best['name']} {best['change_pct']:+.2f}%, "
            f"최저 {worst['name']} {worst['change_pct']:+.2f}%.")


def story_lines(story, judged):
    mark = "교차확인" if story["source_count"] > 1 else "단일출처"
    verdict = judged.get(normalize(story["title"]))
    head = f"**{verdict['stance']}** · " if verdict else ""
    lines = [f"- {head}[{story['title']}]({primary_link(story)})",
             f"  - {outlets(story)} · {story['published_kst'][11:16]} KST · "
             f"{mark}({'+'.join(story['sources'])})"]
    if verdict:
        lines.append(f"  - 요약: {verdict.get('summary', '-')}")
        lines.append(f"  - 판단: {verdict.get('impact', '-')}")
        if verdict.get("caution"):
            lines.append(f"  - 유의: {verdict['caution']}")
    else:
        lines.append(f"  - 제목만 확보({story.get('body_error', '본문 없음')}) · 요약·판단 보류")
    return lines


def stance_summary(stories, judged):
    """접힌 줄에서도 무슨 뉴스인지 보이도록 호재/악재 건수를 센다."""
    counts, pending = {}, 0
    for story in stories:
        verdict = judged.get(normalize(story["title"]))
        if verdict:
            counts[verdict["stance"]] = counts.get(verdict["stance"], 0) + 1
        else:
            pending += 1
    parts = [f"{stance} {count}" for stance, count in sorted(counts.items(), key=lambda p: -p[1])]
    if pending:
        parts.append(f"판단보류 {pending}")
    return " · ".join(parts)


def pick_lines(pick):
    if not pick:
        return []
    choice = pick["pick"]
    group = "보유" if choice.get("group", "holding") == "holding" else "관심"
    lines = ["## 오늘의 추천 종목", "",
             f"### [{group}] {choice['name']} ({choice['ticker']}) — {choice['headline']}", "",
             f"- **왜**: {choice['reason']}",
             f"- **유의**: {choice['risk']}"]
    if pick.get("others"):
        lines += ["", "함께 검토한 후보:"]
        lines += [f"- {row['ticker']}: {row['note']}" for row in pick["others"]]
    lines += ["", f"_{pick['disclaimer']} 후보는 거래량·등락·뉴스 화제성·52주 위치·RSI·볼린저로 "
              f"추린 상위 {len(pick['candidates'])}개입니다._", ""]
    return lines


def spotlight(prices):
    """오늘 눈에 띄는 종목을 데이터만으로 뽑는다. 모델을 쓰지 않으므로 토큰이 들지 않는다."""
    rows = [row for row in prices["holdings"] if row.get("change_pct") is not None]
    tag = lambda row: "보유" if row.get("group", "holding") == "holding" else "관심"
    moved = sorted(rows, key=lambda r: r["change_pct"], reverse=True)
    volume = [r for r in rows if r.get("volume_ratio", 0) >= 1.5]
    near_high = [r for r in rows if r.get("from_year_high") is not None
                 and r["from_year_high"] >= -3]
    near_low = [r for r in rows if r.get("from_year_low") is not None
                and r["from_year_low"] <= 5]
    hot = [r for r in rows if r.get("rsi") is not None and r["rsi"] >= 70]
    cold = [r for r in rows if r.get("rsi") is not None and r["rsi"] <= 30]
    return dict(
        tag=tag,
        up=[r for r in moved if r["change_pct"] > 0][:5],
        down=[r for r in moved if r["change_pct"] < 0][-5:][::-1],
        volume=sorted(volume, key=lambda r: -r["volume_ratio"])[:5],
        near_high=sorted(near_high, key=lambda r: -r["from_year_high"])[:5],
        near_low=sorted(near_low, key=lambda r: r["from_year_low"])[:5],
        hot=sorted(hot, key=lambda r: -r["rsi"])[:5],
        cold=sorted(cold, key=lambda r: r["rsi"])[:5])


def spotlight_lines(prices):
    view = spotlight(prices)
    tag = view["tag"]
    lines = ["## 오늘의 주목", "",
             "가격 데이터만으로 뽑았습니다. 모델을 쓰지 않으므로 추가 비용이 없습니다.", ""]
    blocks = [
        ("상승 상위", view["up"], lambda r: f"{r['change_pct']:+.2f}%"),
        ("하락 상위", view["down"], lambda r: f"{r['change_pct']:+.2f}%"),
        ("거래량 급증 (10일 평균 대비)", view["volume"],
         lambda r: f"{r['volume_ratio']:.1f}배 · {r['change_pct']:+.2f}%"),
        ("52주 신고가 근접 (-3% 이내)", view["near_high"],
         lambda r: f"고점 대비 {r['from_year_high']:+.1f}%"),
        ("52주 신저가 근접 (+5% 이내)", view["near_low"],
         lambda r: f"저점 대비 {r['from_year_low']:+.1f}%"),
        ("RSI 과매수 (70 이상)", view["hot"], lambda r: f"RSI {r['rsi']:.1f}"),
        ("RSI 과매도 (30 이하)", view["cold"], lambda r: f"RSI {r['rsi']:.1f}"),
    ]
    for title, rows, render in blocks:
        if not rows:
            continue
        body = ", ".join(f"{r['name']}({tag(r)} {cap_text(r)}, {render(r)})" for r in rows)
        lines.append(f"- **{title}** — {body}")
    lines.append("")
    return lines


def alert_section(prices, news, judged):
    """볼린저 하단을 이탈한 종목과 그 뉴스. 관심 종목도 포함한다."""
    by_ticker = {row["ticker"]: row for row in prices["holdings"]}
    alerts = news.get("alerts", {})
    lower = [row for row in prices["holdings"] if row.get("bb_position") == "하단 이탈"]
    upper = [row for row in prices["holdings"] if row.get("bb_position") == "상단 이탈"]
    lines = ["## 오늘의 특이점", ""]
    if not lower and not upper:
        return lines + ["- 볼린저밴드(20, 2σ)를 벗어난 종목이 없습니다.", ""]
    if upper:
        names = ", ".join(f"{row['name']}({row['ticker']}, %B {row['bb_percent_b']:.2f})"
                          for row in sorted(upper, key=lambda r: -r["bb_percent_b"]))
        lines += [f"**상단 이탈 {len(upper)}종목** — {names}", ""]
    if not lower:
        return lines + ["**하단 이탈 없음** — 하단을 벗어난 종목이 있으면 관련 뉴스를 함께 찾습니다.", ""]
    lines += [f"**하단 이탈 {len(lower)}종목** — 관련 뉴스를 찾아 함께 싣습니다.", ""]
    for row in sorted(lower, key=lambda r: r["bb_percent_b"]):
        group = "보유" if row.get("group", "holding") == "holding" else "관심"
        lines.append(f"### [{group}] {row['name']} ({row['ticker']}) "
                     f"{amount(row)} {move(row)[0]}")
        lines.append("")
        lines.append(f"- %B {row['bb_percent_b']:.3f} · RSI {row['rsi']:.1f} "
                     f"({row['rsi_zone']}) · 하단 밴드 {row['bb_lower']:,.2f}")
        holding_news = alerts.get(row["name"]) or news["news"].get(row["name"])
        stories = (holding_news or {}).get("stories") or []
        if not stories:
            lines.append("- 오늘자 관련 기사 없음")
        for story in stories:
            lines += story_lines(story, judged)
        lines.append("")
    return lines


def news_off(prices_by_name, news):
    """가격은 보지만 뉴스는 끈 보유 종목. 브리핑에서 빠진 이유를 밝혀 둔다."""
    covered = set(news["news"]) | set(news.get("alerts", {}))
    return [row for name, row in prices_by_name.items()
            if row.get("group", "holding") == "holding" and name not in covered]


def news_section(prices_by_name, news, judged):
    """종목별로 접었다 펼 수 있게 <details>로 감싼다. GitHub 마크다운에서도 동작한다."""
    lines = []
    for name, row in news["news"].items():
        price = prices_by_name.get(name, {})
        label = f"{move(price)[0]} {amount(price)}" if price.get("price") else ""
        tally = stance_summary(row["stories"], judged)
        head = f"<b>{name}</b> <code>{row['ticker']}</code> {label}"
        lines += ["<details>", f"<summary>{head}{' — ' + tally if tally else ''}</summary>", ""]
        if not row["stories"]:
            lines.append(f"- 오늘자 기사 없음 (후보 {row['candidates']}건, 과거 기사로 대체하지 않음)")
        for story in row["stories"]:
            lines += story_lines(story, judged)
        lines += ["", "</details>", ""]
    return lines


# ---------------------------------------------------------------- HTML

TEMPLATE_HEAD = """<title>보유종목 데일리 브리핑</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Gowun+Batang:wght@400;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@300;400;500;600&display=swap">
<style>
:root{
  --ground:#F1F3F5; --surface:#FFFFFF; --ink:#15181E; --muted:#5B616D;
  --line:#DCE0E6; --rule:#0E5449; --chip:#E9ECF1; --chipink:#3D4250;
  --up:#C4322A; --down:#1A56C0; --flat:#767C88; --shadow:0 1px 2px rgba(20,26,38,.06);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#0D1015; --surface:#151A21; --ink:#E7EAEF; --muted:#98A0AE;
    --line:#242B35; --rule:#43AF95; --chip:#1C232C; --chipink:#AAB3C0;
    --up:#EF6A5F; --down:#6FA2F6; --flat:#8B93A0; --shadow:none;
  }
}
:root[data-theme="dark"]{
  --ground:#0D1015; --surface:#151A21; --ink:#E7EAEF; --muted:#98A0AE;
  --line:#242B35; --rule:#43AF95; --chip:#1C232C; --chipink:#AAB3C0;
  --up:#EF6A5F; --down:#6FA2F6; --flat:#8B93A0; --shadow:none;
}
*{box-sizing:border-box}
body{margin:0; background:var(--ground); color:var(--ink);
  font-family:"IBM Plex Sans KR","Apple SD Gothic Neo","Malgun Gothic",system-ui,sans-serif;
  line-height:1.65; -webkit-font-smoothing:antialiased}
.page{max-width:1080px; margin:0 auto; padding:40px 24px 72px; display:flex; flex-direction:column; gap:40px}
.masthead{display:flex; flex-direction:column; gap:10px; border-bottom:2px solid var(--rule); padding-bottom:20px}
.eyebrow{font-size:11px; letter-spacing:.16em; text-transform:uppercase; color:var(--rule); font-weight:600}
h1{font-family:"Gowun Batang",serif; font-weight:700; font-size:clamp(28px,4.4vw,42px);
   margin:0; letter-spacing:-.01em; text-wrap:balance}
.dateline{display:flex; flex-wrap:wrap; gap:8px 20px; color:var(--muted); font-size:13px}
.dateline b{color:var(--ink); font-weight:500}
h2{font-family:"Gowun Batang",serif; font-size:20px; font-weight:700; margin:0 0 14px;
   padding-bottom:8px; border-bottom:1px solid var(--line)}
section{display:flex; flex-direction:column}
.lede{font-size:15px; margin:0; max-width:62ch}
.byline{font-size:12px; color:var(--muted); margin:0 0 16px; max-width:70ch}
.tally{display:flex; flex-wrap:wrap; gap:26px; margin-top:16px; align-items:baseline}
.tally div{display:flex; align-items:baseline; gap:8px}
.tally span{font-size:12px; color:var(--muted); letter-spacing:.04em}
.tally b{font-family:"IBM Plex Mono",monospace; font-size:26px; font-weight:500; font-variant-numeric:tabular-nums}
.rows{display:flex; flex-direction:column; border-top:1px solid var(--line)}
.row{display:grid; grid-template-columns:minmax(130px,1.4fr) 116px 84px minmax(90px,1fr) 78px 84px minmax(96px,auto);
     gap:12px; align-items:center; padding:11px 4px; border-bottom:1px solid var(--line)}
.row.head{padding-bottom:7px; color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase}
.name{font-weight:500; font-size:14.5px}
.name small{display:block; font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted)}
.num{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; text-align:right; font-size:14px}
.num small{display:block; font-size:10.5px; color:var(--muted); margin-top:1px}
.pct{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; text-align:right;
     font-size:14px; font-weight:500}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--flat)}
.bar{position:relative; height:9px; background:linear-gradient(var(--line),var(--line)) center/1px 100% no-repeat}
.bar i{position:absolute; top:0; height:9px; border-radius:1px; display:block}
.verdict{font-size:11.5px; color:var(--muted); text-align:right; line-height:1.4}
.tech{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums; font-size:12.5px; text-align:right}
.tech small{display:block; font-family:"IBM Plex Sans KR",sans-serif; font-size:10px; color:var(--muted)}
.band{font-size:11.5px; text-align:center; white-space:nowrap}
.band.up,.band.down{font-weight:600}
.tabs{display:flex; gap:4px; border-bottom:1px solid var(--line); margin-bottom:18px}
.tabs button{font:inherit; font-size:13px; font-weight:500; color:var(--muted); background:none;
  border:0; border-bottom:2px solid transparent; padding:8px 14px; cursor:pointer; margin-bottom:-1px}
.tabs button:hover{color:var(--ink)}
.tabs button[aria-selected="true"]{color:var(--rule); border-bottom-color:var(--rule)}
.tabs button:focus-visible{outline:2px solid var(--rule); outline-offset:-2px}
.tabs .count{font-family:"IBM Plex Mono",monospace; font-size:11px; opacity:.7; margin-left:5px}
.panel{display:flex; flex-direction:column; gap:26px}
.panel[hidden]{display:none}
.subhead{font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted);
  margin:0 0 8px; font-weight:600}
.watch-row{display:grid; grid-template-columns:minmax(140px,1.5fr) 88px 100px 88px 80px 74px 82px;
  gap:12px; align-items:center; padding:11px 4px; border-bottom:1px solid var(--line)}
.watch-row.head{padding-bottom:7px; color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase}
.controls{display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:6px}
.controls button{font:inherit; font-size:12px; color:var(--ink); background:var(--surface);
  border:1px solid var(--line); border-radius:3px; padding:5px 12px; cursor:pointer}
.controls button:hover{border-color:var(--rule); color:var(--rule)}
.controls button:focus-visible{outline:2px solid var(--rule); outline-offset:2px}
.holding{border-bottom:1px solid var(--line)}
.holding:last-child{border-bottom:0}
.holding-head{display:flex; flex-wrap:wrap; align-items:center; gap:10px; padding:14px 4px;
  cursor:pointer; list-style:none; user-select:none}
.holding-head::-webkit-details-marker{display:none}
.holding-head::before{content:"▸"; color:var(--muted); font-size:11px; width:12px; flex:none;
  transition:transform .15s ease}
.holding[open] > .holding-head::before{transform:rotate(90deg)}
.holding-head:hover{background:var(--chip)}
.holding-head:focus-visible{outline:2px solid var(--rule); outline-offset:-2px}
.holding-head h3{font-family:"Gowun Batang",serif; font-size:17px; font-weight:700; margin:0}
.holding-head .code{font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted)}
.holding-head .quote{font-family:"IBM Plex Mono",monospace; font-size:13px; font-variant-numeric:tabular-nums}
.holding-head .quote.muted{color:var(--muted); font-size:12px}
.holding-head .quote.muted b.up{color:var(--up)} .holding-head .quote.muted b.down{color:var(--down)}
.holding-head .spacer{flex:1 1 auto}
.tallies{display:flex; gap:5px; flex-wrap:wrap}
.tallies .stance{font-size:10px; padding:2px 7px; letter-spacing:.02em}
.stories{display:flex; flex-direction:column; gap:16px; padding:4px 4px 22px 16px}
.empty{font-size:13px; color:var(--muted)}
.story{display:flex; flex-direction:column; gap:6px}
.story + .story{padding-top:16px; border-top:1px dashed var(--line)}
.story a{color:var(--ink); text-decoration:none; font-size:15.5px; font-weight:500;
         border-bottom:1px solid var(--line); align-self:flex-start; text-wrap:balance}
.story a:hover{border-bottom-color:var(--rule); color:var(--rule)}
.story a:focus-visible{outline:2px solid var(--rule); outline-offset:3px}
.meta{display:flex; flex-wrap:wrap; gap:8px; align-items:center; font-size:12px; color:var(--muted)}
.chip{background:var(--chip); color:var(--chipink); border-radius:3px; padding:2px 7px; font-size:11px; white-space:nowrap}
.chip.confirmed{color:var(--rule); border:1px solid var(--rule); background:transparent; font-weight:500}
.stance{font-size:11px; font-weight:600; letter-spacing:.06em; padding:3px 9px; border-radius:2px; white-space:nowrap}
.stance.good{background:var(--up); color:#fff}
.stance.bad{background:var(--down); color:#fff}
.stance.neutral{background:var(--chip); color:var(--chipink)}
.time{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums}
.take{display:flex; flex-direction:column; gap:5px; padding-left:12px;
      border-left:2px solid var(--line); font-size:13.5px; max-width:76ch; line-height:1.6}
.take b{font-weight:600; font-size:11px; letter-spacing:.08em; color:var(--muted); margin-right:6px}
.take .care{color:var(--muted)}
.notes{background:var(--surface); border:1px solid var(--line); border-radius:4px; padding:20px 22px; box-shadow:var(--shadow)}
.notes h2{border:0; padding:0; margin:0 0 10px; font-size:16px}
.notes ul{margin:0; padding-left:18px; display:flex; flex-direction:column; gap:6px;
          font-size:13px; color:var(--muted); max-width:72ch}
.notes li b{color:var(--ink); font-weight:500}
.warn{border-left:3px solid var(--up); padding-left:12px; margin-top:14px; font-size:13px}
.alert{border:1px solid var(--line); border-left:3px solid var(--down); border-radius:3px;
  background:var(--surface); padding:16px 18px; display:flex; flex-direction:column; gap:12px}
.alert + .alert{margin-top:14px}
.alert-head{display:flex; flex-wrap:wrap; align-items:baseline; gap:9px}
.alert-head h3{font-family:"Gowun Batang",serif; font-size:17px; font-weight:700; margin:0}
.alert-head .code{font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted)}
.alert-head .quote{font-family:"IBM Plex Mono",monospace; font-size:13px; font-variant-numeric:tabular-nums}
.metrics{display:flex; flex-wrap:wrap; gap:8px; font-family:"IBM Plex Mono",monospace;
  font-size:11.5px; font-variant-numeric:tabular-nums; color:var(--muted)}
.metrics span{background:var(--chip); border-radius:3px; padding:2px 8px}
.upper-note{font-size:13px; color:var(--muted); margin:0 0 14px; max-width:74ch}
.pick{border:1px solid var(--rule); border-radius:4px; background:var(--surface);
  padding:18px 20px; display:flex; flex-direction:column; gap:10px; box-shadow:var(--shadow)}
.pick-head{display:flex; flex-wrap:wrap; align-items:baseline; gap:9px}
.pick-head h3{font-family:"Gowun Batang",serif; font-size:19px; margin:0}
.pick-head .code{font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted)}
.pick-head .line{font-size:14px; color:var(--rule); font-weight:500}
.pick .body{display:flex; flex-direction:column; gap:6px; font-size:13.5px;
  line-height:1.65; max-width:76ch}
.pick .body b{font-size:11px; letter-spacing:.08em; color:var(--muted); margin-right:6px}
.pick .others{display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--muted);
  border-top:1px dashed var(--line); padding-top:9px}
.pick .others code{font-family:"IBM Plex Mono",monospace; color:var(--ink)}
.spot{display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px}
.spot section{border:1px solid var(--line); border-radius:4px; background:var(--surface);
  padding:13px 15px; gap:7px; box-shadow:var(--shadow)}
.spot h3{margin:0; font-size:12px; letter-spacing:.05em; color:var(--muted); font-weight:600}
.spot ul{margin:0; padding:0; list-style:none; display:flex; flex-direction:column; gap:5px}
.spot li{display:flex; justify-content:space-between; gap:10px; font-size:13px; align-items:baseline}
.spot li b{font-weight:500}
.spot .who{font-size:10px; color:var(--muted); margin-left:4px}
.spot .val{font-family:"IBM Plex Mono",monospace; font-variant-numeric:tabular-nums;
  font-size:12.5px; white-space:nowrap}
.upper-note b{color:var(--up)}
@media (max-width:720px){
  /* 좁은 화면에서는 표를 카드처럼 쌓는다. 열 7개를 가로로 욱여넣으면 읽을 수가 없다. */
  .page{padding:24px 16px 56px; gap:30px}
  .masthead{gap:8px; padding-bottom:16px}
  .dateline{gap:4px 14px; font-size:12px}
  .tally{gap:14px 20px}
  .tally b{font-size:22px}
  h2{font-size:18px}
  .tabs button{padding:8px 10px; font-size:12.5px}

  .row.head,.watch-row.head{display:none}
  .row,.watch-row{
    display:grid; grid-template-columns:minmax(0,1fr) auto;
    gap:2px 12px; padding:13px 2px; align-items:baseline;
  }
  .name{font-size:15px}
  .name small{font-size:11px}

  /* 보유 표: 이름 | 종가 / 등락률, 그 아래 막대와 지표. 자리를 명시해 배치가 흔들리지 않게 한다. */
  .row > :nth-child(1){grid-column:1; grid-row:1 / span 2}
  .row > :nth-child(2){grid-column:2; grid-row:1; text-align:right}
  .row > :nth-child(3){grid-column:2; grid-row:2; text-align:right; font-size:15px}
  .row > :nth-child(4){grid-column:1 / -1; grid-row:3; margin:6px 0 4px}
  .row > :nth-child(5){grid-column:1; grid-row:4; text-align:left}
  .row > :nth-child(6){grid-column:2; grid-row:4; text-align:right}
  .row > :nth-child(7){grid-column:1 / -1; grid-row:5; text-align:left; font-size:11px}

  /* 관심 표: 이름 | 종가 / 시총 | 변동·등락률 / RSI | 볼린저 */
  .watch-row > :nth-child(1){grid-column:1; grid-row:1}
  .watch-row > :nth-child(2){grid-column:1; grid-row:2; text-align:left;
    font-size:12px; color:var(--muted)}
  .watch-row > :nth-child(3){grid-column:2; grid-row:1; text-align:right}
  .watch-row > :nth-child(4){grid-column:2; grid-row:2; text-align:right; font-size:12px}
  .watch-row > :nth-child(5){grid-column:2; grid-row:3; text-align:right; font-size:15px}
  .watch-row > :nth-child(6){grid-column:1; grid-row:3; text-align:left}
  .watch-row > :nth-child(7){grid-column:1 / -1; grid-row:4; text-align:left; font-size:12px}

  .tech{font-size:12.5px}
  .tech::before{content:"RSI "; font-family:"IBM Plex Sans KR",sans-serif; color:var(--muted)}
  .tech small{display:inline; margin-left:4px}
  .band{font-size:12px}
  .verdict{margin-top:2px}

  .holding-head{padding:13px 2px; gap:6px 8px}
  .holding-head h3{font-size:16px}
  .holding-head .spacer{display:none}
  .tallies{width:100%; margin-top:2px}
  .stories{padding:2px 0 18px 10px; gap:14px}
  .story a{font-size:15px}
  .take{font-size:13px; padding-left:10px; max-width:none}
  .alert{padding:14px 14px}
  .notes{padding:16px 16px}
  .notes ul{max-width:none}
}
@media (max-width:400px){
  .row,.watch-row{grid-template-columns:minmax(0,1fr) auto}
  .name{font-size:14px}
  .row .pct,.watch-row .pct{font-size:14px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important; animation:none!important}}
</style>
"""


def bar(percent, scale):
    """0을 기준으로 좌우로 뻗는 막대. 상승은 빨강, 하락은 파랑(국내 증시 관례)."""
    if not percent:
        return '<span class="bar"></span>'
    width = min(abs(percent) / scale * 50, 50)
    side = "left:50%" if percent > 0 else "right:50%"
    color = "var(--up)" if percent > 0 else "var(--down)"
    return f'<span class="bar"><i style="{side};width:{width:.1f}%;background:{color}"></i></span>'


def nxt_cell(row):
    if "nxt_price" in row:
        return f'<small>NXT ₩{row["nxt_price"]:,.0f} {row["nxt_change_pct"]:+.2f}%</small>'
    return f'<small>NXT {esc(row.get("nxt_error", "-").split(":")[0])}</small>' if "nxt_error" in row else ""


def html_rows(rows, scale, with_nxt=False):
    head = (f'<div class="row head"><span>종목</span>'
            f'<span class="num">{"KRX 종가" if with_nxt else "종가"}</span>'
            f'<span class="pct">전일 대비</span><span class="bar"></span>'
            f'<span class="tech">RSI(14)</span><span class="band">볼린저(20,2σ)</span>'
            f'<span class="verdict">검증</span></div>')
    out = ['<div class="rows">', head]
    for row in sorted(rows, key=cap_key, reverse=True):
        label, tone = move(row)
        if "rsi" in row:
            tech = (f'<span class="tech {RSI_TONE.get(row["rsi_zone"], "flat")}">{row["rsi"]:.1f}'
                    f'<small>{esc(row["rsi_zone"])}</small></span>')
        else:
            tech = '<span class="tech">-</span>'
        band = (f'<span class="band {BAND_TONE.get(row.get("bb_position"), "flat")}">'
                f'{esc(band_text(row))}</span>')
        out.append(
            f'<div class="row"><span class="name">{esc(row["name"])}'
            f'<small>{esc(row["ticker"])}</small></span>'
            f'<span class="num">{amount(row)}{nxt_cell(row) if with_nxt else ""}</span>'
            f'<span class="pct {tone}">{label}</span>'
            f'{bar(row.get("change_pct"), scale)}{tech}{band}'
            f'<span class="verdict">{esc(verification(row))}</span></div>')
    return "\n".join(out + ["</div>"])


def html_watch(rows):
    out = ['<div class="rows">',
           '<div class="watch-row head"><span>종목</span><span class="num">시가총액</span>'
           '<span class="num">종가</span><span class="num">변동</span>'
           '<span class="pct">변동률</span>'
           '<span class="tech">RSI(14)</span><span class="band">볼린저</span></div>']
    for row in sorted(rows, key=cap_key, reverse=True):
        label, tone = move(row)
        if "rsi" in row:
            tech = (f'<span class="tech {RSI_TONE.get(row["rsi_zone"], "flat")}">{row["rsi"]:.1f}'
                    f'<small>{esc(row["rsi_zone"])}</small></span>')
        else:
            tech = '<span class="tech">-</span>'
        out.append(
            f'<div class="watch-row"><span class="name">{esc(row["name"])}'
            f'<small>{esc(row["ticker"])}</small></span>'
            f'<span class="num">{cap_text(row)}</span>'
            f'<span class="num">{amount(row)}</span>'
            f'<span class="num {tone}">{delta(row)}</span>'
            f'<span class="pct {tone}">{label}</span>{tech}'
            f'<span class="band {BAND_TONE.get(row.get("bb_position"), "flat")}">'
            f'{esc(band_text(row))}</span></div>')
    return "\n".join(out + ["</div>"])


def html_story(story, judged):
    confirmed = story["source_count"] > 1
    chip = ("confirmed", f"교차확인 · {'+'.join(story['sources'])}") if confirmed else (
        "", f"단일출처 · {'+'.join(story['sources'])}")
    verdict = judged.get(normalize(story["title"]))
    if verdict:
        badge = (f'<span class="stance {STANCE_TONE.get(verdict["stance"], "neutral")}">'
                 f'{esc(verdict["stance"])}</span>')
        care = (f'<span class="care"><b>유의</b>{esc(verdict["caution"])}</span>'
                if verdict.get("caution") else "")
        take = (f'<div class="take"><span><b>요약</b>{esc(verdict.get("summary", "-"))}</span>'
                f'<span><b>판단</b>{esc(verdict.get("impact", "-"))}</span>{care}</div>')
    else:
        badge = ""
        take = (f'<div class="take"><span class="care"><b>제목만 확보</b>'
                f'{esc(story.get("body_error", "본문 없음"))} · 요약·판단 보류</span></div>')
    return (f'<article class="story"><a href="{esc(primary_link(story))}" target="_blank" '
            f'rel="noopener">{esc(story["title"])}</a>'
            f'<div class="meta">{badge}<span>{esc(outlets(story))}</span>'
            f'<span class="time">{story["published_kst"][11:16]} KST</span>'
            f'<span class="chip {chip[0]}">{esc(chip[1])}</span></div>{take}</article>')


def tallies(stories, judged):
    """접힌 상태에서도 무슨 뉴스인지 보이도록 호재/악재 건수를 요약한다."""
    counts, pending = {}, 0
    for story in stories:
        verdict = judged.get(normalize(story["title"]))
        if verdict:
            counts[verdict["stance"]] = counts.get(verdict["stance"], 0) + 1
        else:
            pending += 1
    chips = [f'<span class="stance {STANCE_TONE.get(stance, "neutral")}">{esc(stance)} {count}</span>'
             for stance, count in sorted(counts.items(), key=lambda pair: -pair[1])]
    if pending:
        chips.append(f'<span class="stance neutral">판단보류 {pending}</span>')
    return f'<span class="tallies">{"".join(chips)}</span>' if chips else ""


def html_pick(pick):
    if not pick:
        return ""
    choice = pick["pick"]
    group = "보유" if choice.get("group", "holding") == "holding" else "관심"
    others = "".join(f'<div><code>{esc(row["ticker"])}</code> {esc(row["note"])}</div>'
                     for row in pick.get("others", []))
    return (f'<div class="pick"><div class="pick-head"><span class="chip">{group}</span>'
            f'<h3>{esc(choice["name"])}</h3>'
            f'<span class="code">{esc(choice["ticker"])}</span>'
            f'<span class="line">{esc(choice["headline"])}</span></div>'
            f'<div class="body"><span><b>왜</b>{esc(choice["reason"])}</span>'
            f'<span><b>유의</b>{esc(choice["risk"])}</span></div>'
            + (f'<div class="others">{others}</div>' if others else "") + '</div>')


def html_spotlight(prices):
    view = spotlight(prices)
    tag = view["tag"]
    blocks = [
        ("상승 상위", view["up"], lambda r: (f"{r['change_pct']:+.2f}%", "up")),
        ("하락 상위", view["down"], lambda r: (f"{r['change_pct']:+.2f}%", "down")),
        ("거래량 급증 · 10일 평균 대비", view["volume"],
         lambda r: (f"{r['volume_ratio']:.1f}배 {r['change_pct']:+.2f}%",
                    "up" if r["change_pct"] > 0 else "down")),
        ("52주 신고가 근접", view["near_high"], lambda r: (f"{r['from_year_high']:+.1f}%", "up")),
        ("52주 신저가 근접", view["near_low"], lambda r: (f"{r['from_year_low']:+.1f}%", "down")),
        ("RSI 과매수 70↑", view["hot"], lambda r: (f"{r['rsi']:.1f}", "up")),
        ("RSI 과매도 30↓", view["cold"], lambda r: (f"{r['rsi']:.1f}", "down")),
    ]
    out = ['<div class="spot">']
    for title, rows, render in blocks:
        if not rows:
            continue
        items = "".join(
            f'<li><b>{esc(row["name"])}'
            f'<span class="who">{tag(row)} · {cap_text(row)}</span></b>'
            f'<span class="val {render(row)[1]}">{render(row)[0]}</span></li>' for row in rows)
        out.append(f'<section><h3>{esc(title)}</h3><ul>{items}</ul></section>')
    return "\n".join(out + ["</div>"])


def html_alerts(prices, news, judged):
    alerts = news.get("alerts", {})
    lower = [row for row in prices["holdings"] if row.get("bb_position") == "하단 이탈"]
    upper = [row for row in prices["holdings"] if row.get("bb_position") == "상단 이탈"]
    out = []
    if upper:
        names = ", ".join(f'{esc(row["name"])}({esc(row["ticker"])} %B {row["bb_percent_b"]:.2f})'
                          for row in sorted(upper, key=lambda r: -r["bb_percent_b"]))
        out.append(f'<p class="upper-note"><b>상단 이탈 {len(upper)}종목</b> — {names}</p>')
    if not lower:
        out.append('<p class="empty">볼린저밴드 하단을 벗어난 종목이 없습니다. '
                   '하단을 벗어난 종목이 생기면 관련 뉴스를 찾아 여기에 싣습니다.</p>')
        return "\n".join(out)
    for row in sorted(lower, key=lambda r: r["bb_percent_b"]):
        label, tone = move(row)
        group = "보유" if row.get("group", "holding") == "holding" else "관심"
        stories = ((alerts.get(row["name"]) or news["news"].get(row["name"])) or {}).get("stories") or []
        body = "".join(html_story(story, judged) for story in stories) or (
            '<p class="empty">오늘자 관련 기사 없음</p>')
        out.append(
            f'<div class="alert"><div class="alert-head">'
            f'<span class="chip">{group}</span><h3>{esc(row["name"])}</h3>'
            f'<span class="code">{esc(row["ticker"])}</span>'
            f'<span class="quote {tone}">{amount(row)} · {label}</span></div>'
            f'<div class="metrics"><span>%B {row["bb_percent_b"]:.3f}</span>'
            f'<span>RSI {row["rsi"]:.1f} {esc(row["rsi_zone"])}</span>'
            f'<span>하단 밴드 {row["bb_lower"]:,.2f}</span>'
            f'<span>중심선 {row["bb_middle"]:,.2f}</span></div>'
            f'<div class="stories">{body}</div></div>')
    return "\n".join(out)


def news_off_note(prices_by_name, news):
    skipped = news_off(prices_by_name, news)
    if not skipped:
        return ""
    names = ", ".join(f'{esc(row["name"])}({esc(row["ticker"])})' for row in skipped)
    return (f'<p class="upper-note">뉴스를 수집하지 않는 보유 종목 {len(skipped)} — {names}. '
            f'가격·지표는 위 표에 그대로 나옵니다.</p>')


def html_news(prices_by_name, news, judged):
    out = []
    for name, row in news["news"].items():
        price = prices_by_name.get(name, {})
        label, tone = move(price)
        quote = (f'<span class="quote {tone}">{amount(price)} · {label}</span>'
                 if price.get("price") else "")
        tech = ""
        if "rsi" in price:
            band = price.get("bb_position", "")
            mark = "" if band == "밴드 내" else f' · <b class="{BAND_TONE.get(band, "")}">{esc(band)}</b>'
            tech = f'<span class="quote muted">RSI {price["rsi"]:.0f}{mark}</span>'

        stories = "".join(html_story(story, judged) for story in row["stories"]) or (
            f'<p class="empty">오늘자 기사 없음 (후보 {row["candidates"]}건, 과거 기사로 대체하지 않음)</p>')
        out.append(
            f'<details class="holding" data-key="{esc(row["ticker"])}">'
            f'<summary class="holding-head"><h3>{esc(name)}</h3>'
            f'<span class="code">{esc(row["ticker"])}</span>{quote}{tech}'
            f'<span class="spacer"></span>{tallies(row["stories"], judged)}</summary>'
            f'<div class="stories">{stories}</div></details>')
    return "\n".join(out)


def render_html(now, prices, news):
    judged, analyst = verdicts()
    pick = recommendation()
    pick_block = (f'<section><h2>오늘의 추천 종목</h2>'
                  f'<p class="byline">{esc(pick["disclaimer"])} 거래량·등락·뉴스 화제성·'
                  f'52주 위치·RSI·볼린저로 추린 상위 {len(pick["candidates"])}개 중에서 골랐습니다.</p>'
                  f'{html_pick(pick)}</section>') if pick else ""
    every = prices["holdings"]
    holdings = [row for row in every if row.get("group", "holding") == "holding"]
    watch = [row for row in every if row.get("group") == "watch"]
    by_name = {row["name"]: row for row in holdings}
    korea = [row for row in holdings if row["currency"] == "KRW"]
    world = [row for row in holdings if row["currency"] != "KRW"]
    # 막대 길이 기준은 보유·관심을 합쳐 잡아야 두 탭의 눈금이 같아진다.
    scored = [row["change_pct"] for row in holdings if row.get("change_pct") is not None]
    every_scored = [row["change_pct"] for row in every if row.get("change_pct") is not None]
    scale = max((abs(value) for value in every_scored), default=1) or 1
    up = sum(1 for value in scored if value > 0)
    down = sum(1 for value in scored if value < 0)
    average = sum(scored) / len(scored) if scored else 0
    trade_dates = sorted({row["date"] for row in every if "date" in row})
    problems = [row for row in every if row["status"] != "ok"]
    judged_count = sum(1 for row in news["news"].values() for story in row["stories"]
                       if normalize(story["title"]) in judged)

    warn = ""
    if problems:
        items = "".join(f"<div>{esc(row['name'])} ({esc(row['ticker'])}): {esc(row['status'])} · "
                        f"{esc(row.get('error', '교차검증 불일치'))}</div>" for row in problems)
        warn = f'<div class="warn"><b>확인 필요</b>{items}</div>'

    return f"""{TEMPLATE_HEAD}
<div class="page">
  <header class="masthead">
    <div class="eyebrow">Daily Portfolio Brief</div>
    <h1>보유 {len(holdings)}종목 브리핑</h1>
    <div class="dateline">
      <span>작성 <b>{now:%Y-%m-%d %H:%M} KST</b></span>
      <span>종가 기준일 <b>{esc(' / '.join(trade_dates) or '없음')}</b></span>
      <span>뉴스 <b>{esc(news['today_kst'])} 발행분 {news['counts']['기사']}건</b></span>
    </div>
  </header>

  <section>
    <h2>한눈에 보기</h2>
    <p class="lede">{esc(summarize(holdings))}</p>
    <div class="tally">
      <div><b class="up">{up}</b><span>상승</span></div>
      <div><b class="down">{down}</b><span>하락</span></div>
      <div><b class="flat">{len(scored) - up - down}</b><span>보합</span></div>
      <div><b>{average:+.2f}%</b><span>평균 등락률</span></div>
      <div><b>{news['counts']['기사']}</b><span>오늘 뉴스</span></div>
      <div><b>{judged_count}</b><span>요약 완료</span></div>
    </div>
    {warn}
  </section>

  <section>
    <div class="tabs" role="tablist">
      <button type="button" role="tab" id="tab-own" aria-controls="panel-own" aria-selected="true"
              data-panel="panel-own">보유 종목<span class="count">{len(holdings)}</span></button>
      <button type="button" role="tab" id="tab-watch" aria-controls="panel-watch" aria-selected="false"
              data-panel="panel-watch">관심 종목<span class="count">{len(watch)}</span></button>
    </div>
    <div class="panel" id="panel-own" role="tabpanel" aria-labelledby="tab-own">
      <div><p class="subhead">국내</p>{html_rows(korea, scale, with_nxt=True)}</div>
      <div><p class="subhead">해외</p>{html_rows(world, scale)}</div>
    </div>
    <div class="panel" id="panel-watch" role="tabpanel" aria-labelledby="tab-watch" hidden>
      <div><p class="subhead">관심 종목 · 가격과 지표만 봅니다 (뉴스는 보유 종목만 수집)</p>
        {html_watch(watch)}</div>
    </div>
  </section>

  {pick_block}

  <section><h2>오늘의 주목</h2>
    <p class="byline">가격 데이터만으로 뽑았습니다. 모델을 쓰지 않으므로 추가 비용이 없습니다.</p>
    {html_spotlight(prices)}
  </section>

  <section><h2>오늘의 특이점</h2>
    <p class="byline">볼린저밴드(20, 2σ)를 벗어난 종목입니다. 하단을 이탈하면 관심 종목이라도 관련 뉴스를 찾아 함께 싣습니다.</p>
    {html_alerts(prices, news, judged)}
  </section>

  <section><h2>종목별 오늘의 뉴스</h2>
    <p class="byline">{esc(analyst or '요약·판단 없음')} 종목을 눌러 펼쳐 보세요.</p>
    <div class="controls">
      <button type="button" data-all="open">모두 펼치기</button>
      <button type="button" data-all="close">모두 접기</button>
      <button type="button" data-all="notable">호재·악재만 펼치기</button>
    </div>
    {html_news(by_name, news, judged)}
    {news_off_note(by_name, news)}
  </section>
  <script>
  (function () {{
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs button'));
    tabs.forEach(function (tab) {{
      tab.addEventListener("click", function () {{
        tabs.forEach(function (other) {{
          var on = other === tab;
          other.setAttribute("aria-selected", on ? "true" : "false");
          document.getElementById(other.dataset.panel).hidden = !on;
        }});
      }});
    }});
  }})();
  (function () {{
    var KEY = "brief-open-holdings";
    var items = Array.prototype.slice.call(document.querySelectorAll("details.holding"));
    function saved() {{
      try {{ return JSON.parse(localStorage.getItem(KEY)) || null; }} catch (e) {{ return null; }} 
    }}
    function store() {{
      try {{
        localStorage.setItem(KEY, JSON.stringify(items.filter(function (d) {{ return d.open; }})
          .map(function (d) {{ return d.dataset.key; }})));
      }} catch (e) {{ /* 저장이 막힌 환경에서도 페이지는 그대로 동작한다 */ }}
    }}
    var remembered = saved();
    if (remembered) {{
      items.forEach(function (d) {{ d.open = remembered.indexOf(d.dataset.key) !== -1; }});
    }}
    items.forEach(function (d) {{ d.addEventListener("toggle", store); }});
    document.querySelectorAll(".controls button").forEach(function (button) {{
      button.addEventListener("click", function () {{
        var mode = button.dataset.all;
        items.forEach(function (d) {{
          d.open = mode === "open" ? true
                 : mode === "close" ? false
                 : !!d.querySelector(".stance.good, .stance.bad");
        }});
        store();
      }});
    }});
  }})();
  </script>

  <section class="notes">
    <h2>데이터 신뢰도</h2>
    <ul>
      <li>국내 종가는 <b>pykrx(KRX)</b>가 1차 출처이며 같은 거래일의 Yahoo·네이버 종가와 교차 검증합니다. 국내는 수정주가 기준입니다.</li>
      <li><b>NXT 종가</b>는 넥스트레이드(ATS) 최종 체결가입니다. 애프터마켓이 20:00까지라 15:30에 끝나는 KRX 종가와 다를 수 있고, 등락률로 역산한 전일 종가가 KRX 전일 종가와 맞을 때만 표시합니다. 표의 등락률·막대는 KRX 기준입니다.</li>
      <li>해외 종가는 <b>Yahoo 단일 출처</b>이며 독립 검증한 값이 아닙니다.</li>
      <li>장중 값이 섞이지 않도록 시장 현지 날짜 기준 당일 일봉은 제외합니다. <b>실시간·시간외 가격이 아닙니다.</b></li>
      <li>뉴스는 제공처 발행 시각(KST)이 오늘인 기사만 종목당 최대 3건까지 쓰고, 두 출처 이상에서 확인되면 <b>교차확인</b>으로 표시합니다. 오늘 기사 {news['counts']['기사']}건 중 교차확인 {news['counts']['교차확인']}건.</li>
      <li>요약·판단은 기사 <b>본문</b>을 읽고 씁니다. 네이버 원문, 구글 링크를 복원한 매체 원문, Yahoo 기사 순으로 추출합니다. 오늘 {news['counts']['기사']}건 중 본문 확보 {news['counts']['본문확보']}건이며, 못 읽은 기사는 <b>제목만 확보</b>로 두고 판단하지 않습니다.</li>
      <li><b>RSI(14)</b>는 와일더 방식, <b>볼린저밴드</b>는 이동평균 20일 · 표준편차 2배(모집단 기준)입니다. 종가와 같은 데이터로 계산했고 최근 약 100거래일을 씁니다. 종가가 상단 밴드보다 높으면 <b>상단 이탈</b>, 낮으면 <b>하단 이탈</b>입니다. 국내는 KRX 종가(수정주가) 기준이라 NXT 가격과는 무관합니다.</li>
      <li>막대는 상승 빨강 / 하락 파랑(국내 증시 관례)이며 길이는 최대 등락폭 {scale:.2f}%에 맞춰 그렸습니다. <b>지표는 참고용이며 투자 자문이 아닙니다.</b></li>
    </ul>
  </section>
</div>
"""


def main():
    now = datetime.now(KST)
    prices = load("prices.json", "prices.py", now.date())
    news = load("news.json", "news.py", now.date())
    judged, analyst = verdicts()
    every = prices["holdings"]
    holdings = [row for row in every if row.get("group", "holding") == "holding"]
    watch = [row for row in every if row.get("group") == "watch"]
    by_name = {row["name"]: row for row in holdings}
    korea = [row for row in holdings if row["currency"] == "KRW"]
    world = [row for row in holdings if row["currency"] != "KRW"]
    trade_dates = sorted({row["date"] for row in every if "date" in row})
    problems = [row for row in every if row["status"] != "ok"]

    lines = [f"# 포트폴리오 브리핑 · {now:%Y-%m-%d (%a) %H:%M} KST", "",
             f"보유 {len(holdings)}종목 · 관심 {len(watch)}종목 · "
             f"종가 기준일 {' / '.join(trade_dates) or '없음'} · "
             f"뉴스 {news['today_kst']} 발행분 {news['counts']['기사']}건", "",
             "## 한눈에 보기", "", summarize(holdings), "",
             "## 보유 종목 · 국내", ""] + table(korea, with_nxt=True)
    lines += ["", "## 보유 종목 · 해외", ""] + table(world)
    lines += ["", f"## 관심 종목 ({len(watch)})", "",
              "가격과 지표만 봅니다. 뉴스는 보유 종목만 수집합니다.", ""] + watch_table(watch)
    lines += [""] + pick_lines(recommendation())
    lines += spotlight_lines(prices)
    lines += alert_section(prices, news, judged)
    lines += ["## 종목별 오늘의 뉴스", ""]
    if analyst:
        lines += [f"_{analyst}_", ""]
    lines += news_section(by_name, news, judged)
    skipped = news_off(by_name, news)
    if skipped:
        lines += [f"뉴스를 수집하지 않는 보유 종목 {len(skipped)}: "
                  + ", ".join(f"{row['name']}({row['ticker']})" for row in skipped)
                  + " — `holdings.json`에서 `\"news\": true`로 바꾸면 다시 수집합니다.", ""]
    lines += ["## 데이터 신뢰도", "",
              "- 국내 종가는 pykrx(KRX)가 1차 출처이며 같은 거래일의 Yahoo·네이버 종가와 교차 검증합니다.",
              "- NXT 종가는 넥스트레이드(ATS) 최종 체결가로, 등락률로 역산한 전일 종가가 KRX 전일 종가와 맞을 때만 표시합니다.",
              "- 해외 종가는 Yahoo 단일 출처이며 독립 검증한 값이 아닙니다.",
              "- 시장 현지 날짜 기준 당일 일봉은 제외합니다. 실시간·시간외 가격이 아닙니다.",
              f"- 오늘 뉴스 {news['counts']['기사']}건 중 교차확인 {news['counts']['교차확인']}건, "
              f"본문 확보 {news['counts']['본문확보']}건. 본문을 못 읽은 기사는 요약·판단하지 않습니다.",
              "- RSI(14)는 와일더 방식, 볼린저밴드는 이동평균 20일·표준편차 2배(모집단 기준)이며 "
              "종가와 같은 데이터로 최근 약 100거래일을 써서 계산합니다. 국내는 KRX 종가 기준입니다.",
              "- 지표는 참고용이며 투자 자문이 아닙니다."]
    if problems:
        lines += ["", "### 확인 필요", ""] + [
            f"- {row['name']} ({row['ticker']}): {row['status']} · {row.get('error', '교차검증 불일치')}"
            for row in problems]
    if news.get("errors"):
        lines += [f"- 뉴스 수집 오류: {message}" for message in news["errors"]]

    text = "\n".join(lines) + "\n"
    (HERE / "briefing.md").write_text(text, encoding="utf-8")
    page = render_html(now, prices, news)
    (HERE / "briefing.html").write_text(page, encoding="utf-8")
    # GitHub Pages 루트 주소에서 바로 열리도록 같은 내용을 index.html로도 둔다.
    (HERE / "index.html").write_text(page, encoding="utf-8")
    print(text)
    print("저장: briefing.md, briefing.html, index.html", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
