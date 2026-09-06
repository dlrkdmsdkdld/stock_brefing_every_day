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


def verification(row):
    checks = [CHECK_LABEL.get(row[key], row[key])
              for key in ("yahoo_check", "naver_check") if key in row]
    return f"KRX 기준 · Yahoo/네이버 {'/'.join(checks)}" if checks else "Yahoo 단일 출처"


# ---------------------------------------------------------------- 마크다운


def table(rows, with_nxt=False):
    head = ["종목", "KRX 종가" if with_nxt else "종가", "전일 대비"] + (["NXT 종가"] if with_nxt else []) + ["검증"]
    lines = ["| " + " | ".join(head) + " |",
             "| --- | ---: | ---: |" + (" ---: |" if with_nxt else "") + " --- |"]
    for row in sorted(rows, key=lambda row: row.get("change_pct") or 0, reverse=True):
        cells = [f"{row['name']} ({row['ticker']})", amount(row), move(row)[0]]
        if with_nxt:
            cells.append(nxt(row))
        cells.append(verification(row))
        lines.append("| " + " | ".join(cells) + " |")
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


def news_section(prices_by_name, news, judged):
    lines = []
    for name, row in news["news"].items():
        price = prices_by_name.get(name, {})
        label = f"{move(price)[0]} {amount(price)}" if price.get("price") else ""
        lines.append(f"### {name} ({row['ticker']}) {label}".rstrip())
        lines.append("")
        if not row["stories"]:
            lines.append(f"- 오늘자 기사 없음 (후보 {row['candidates']}건, 과거 기사로 대체하지 않음)")
        for story in row["stories"]:
            lines += story_lines(story, judged)
        lines.append("")
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
.row{display:grid; grid-template-columns:minmax(140px,1.5fr) 118px 88px minmax(110px,1fr) minmax(110px,auto);
     gap:14px; align-items:center; padding:11px 4px; border-bottom:1px solid var(--line)}
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
.holding{display:flex; flex-direction:column; gap:14px; padding:20px 0; border-bottom:1px solid var(--line)}
.holding:last-child{border-bottom:0}
.holding-head{display:flex; flex-wrap:wrap; align-items:baseline; gap:10px}
.holding-head h3{font-family:"Gowun Batang",serif; font-size:18px; font-weight:700; margin:0}
.holding-head .code{font-family:"IBM Plex Mono",monospace; font-size:12px; color:var(--muted)}
.holding-head .quote{font-family:"IBM Plex Mono",monospace; font-size:13px; font-variant-numeric:tabular-nums}
.empty{font-size:13px; color:var(--muted)}
.story{display:flex; flex-direction:column; gap:6px}
.story + .story{padding-top:14px; border-top:1px dashed var(--line)}
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
@media (max-width:720px){
  .row{grid-template-columns:1fr 104px 78px; row-gap:6px}
  .bar,.verdict{grid-column:1/-1; text-align:left}
  .row.head .bar,.row.head .verdict{display:none}
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
    out = ['<div class="rows">',
           f'<div class="row head"><span>종목</span><span class="num">'
           f'{"KRX 종가" if with_nxt else "종가"}</span><span class="pct">전일 대비</span>'
           f'<span class="bar"></span><span class="verdict">검증</span></div>']
    for row in sorted(rows, key=lambda row: row.get("change_pct") or 0, reverse=True):
        label, tone = move(row)
        out.append(
            f'<div class="row"><span class="name">{esc(row["name"])}<small>{esc(row["ticker"])}</small></span>'
            f'<span class="num">{amount(row)}{nxt_cell(row) if with_nxt else ""}</span>'
            f'<span class="pct {tone}">{label}</span>'
            f'{bar(row.get("change_pct"), scale)}'
            f'<span class="verdict">{esc(verification(row))}</span></div>')
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


def html_news(prices_by_name, news, judged):
    out = []
    for name, row in news["news"].items():
        price = prices_by_name.get(name, {})
        label, tone = move(price)
        quote = (f'<span class="quote {tone}">{amount(price)} · {label}</span>'
                 if price.get("price") else "")
        stories = "".join(html_story(story, judged) for story in row["stories"]) or (
            f'<p class="empty">오늘자 기사 없음 (후보 {row["candidates"]}건, 과거 기사로 대체하지 않음)</p>')
        out.append(f'<div class="holding"><div class="holding-head"><h3>{esc(name)}</h3>'
                   f'<span class="code">{esc(row["ticker"])}</span>{quote}</div>{stories}</div>')
    return "\n".join(out)


def render_html(now, prices, news):
    judged, analyst = verdicts()
    holdings = prices["holdings"]
    by_name = {row["name"]: row for row in holdings}
    korea = [row for row in holdings if row["currency"] == "KRW"]
    world = [row for row in holdings if row["currency"] != "KRW"]
    scored = [row["change_pct"] for row in holdings if row.get("change_pct") is not None]
    scale = max((abs(value) for value in scored), default=1) or 1
    up = sum(1 for value in scored if value > 0)
    down = sum(1 for value in scored if value < 0)
    average = sum(scored) / len(scored) if scored else 0
    trade_dates = sorted({row["date"] for row in holdings if "date" in row})
    problems = [row for row in holdings if row["status"] != "ok"]
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

  <section><h2>국내</h2>{html_rows(korea, scale, with_nxt=True)}</section>
  <section><h2>해외</h2>{html_rows(world, scale)}</section>

  <section><h2>종목별 오늘의 뉴스</h2>
    <p class="byline">{esc(analyst or '요약·판단 없음')}</p>
    {html_news(by_name, news, judged)}
  </section>

  <section class="notes">
    <h2>데이터 신뢰도</h2>
    <ul>
      <li>국내 종가는 <b>pykrx(KRX)</b>가 1차 출처이며 같은 거래일의 Yahoo·네이버 종가와 교차 검증합니다. 국내는 수정주가 기준입니다.</li>
      <li><b>NXT 종가</b>는 넥스트레이드(ATS) 최종 체결가입니다. 애프터마켓이 20:00까지라 15:30에 끝나는 KRX 종가와 다를 수 있고, 등락률로 역산한 전일 종가가 KRX 전일 종가와 맞을 때만 표시합니다. 표의 등락률·막대는 KRX 기준입니다.</li>
      <li>해외 종가는 <b>Yahoo 단일 출처</b>이며 독립 검증한 값이 아닙니다.</li>
      <li>장중 값이 섞이지 않도록 시장 현지 날짜 기준 당일 일봉은 제외합니다. <b>실시간·시간외 가격이 아닙니다.</b></li>
      <li>뉴스는 제공처 발행 시각(KST)이 오늘인 기사만 종목당 최대 3건까지 쓰고, 두 출처 이상에서 확인되면 <b>교차확인</b>으로 표시합니다. 오늘 기사 {news['counts']['기사']}건 중 교차확인 {news['counts']['교차확인']}건.</li>
      <li>요약·판단은 기사 <b>본문</b>을 읽고 씁니다. 네이버 원문, 구글 링크를 복원한 매체 원문, Yahoo 기사 순으로 추출합니다. 오늘 {news['counts']['기사']}건 중 본문 확보 {news['counts']['본문확보']}건이며, 못 읽은 기사는 <b>제목만 확보</b>로 두고 판단하지 않습니다.</li>
      <li>막대는 상승 빨강 / 하락 파랑(국내 증시 관례)이며 길이는 최대 등락폭 {scale:.2f}%에 맞춰 그렸습니다. <b>투자 자문이 아닙니다.</b></li>
    </ul>
  </section>
</div>
"""


def main():
    now = datetime.now(KST)
    prices = load("prices.json", "prices.py", now.date())
    news = load("news.json", "news.py", now.date())
    judged, analyst = verdicts()
    holdings = prices["holdings"]
    by_name = {row["name"]: row for row in holdings}
    korea = [row for row in holdings if row["currency"] == "KRW"]
    world = [row for row in holdings if row["currency"] != "KRW"]
    trade_dates = sorted({row["date"] for row in holdings if "date" in row})
    problems = [row for row in holdings if row["status"] != "ok"]

    lines = [f"# 포트폴리오 브리핑 · {now:%Y-%m-%d (%a) %H:%M} KST", "",
             f"보유 {len(holdings)}종목 · 종가 기준일 {' / '.join(trade_dates) or '없음'} · "
             f"뉴스 {news['today_kst']} 발행분 {news['counts']['기사']}건", "",
             "## 한눈에 보기", "", summarize(holdings), "",
             "## 국내", ""] + table(korea, with_nxt=True) + ["", "## 해외", ""] + table(world)
    lines += ["", "## 종목별 오늘의 뉴스", ""]
    if analyst:
        lines += [f"_{analyst}_", ""]
    lines += news_section(by_name, news, judged)
    lines += ["## 데이터 신뢰도", "",
              "- 국내 종가는 pykrx(KRX)가 1차 출처이며 같은 거래일의 Yahoo·네이버 종가와 교차 검증합니다.",
              "- NXT 종가는 넥스트레이드(ATS) 최종 체결가로, 등락률로 역산한 전일 종가가 KRX 전일 종가와 맞을 때만 표시합니다.",
              "- 해외 종가는 Yahoo 단일 출처이며 독립 검증한 값이 아닙니다.",
              "- 시장 현지 날짜 기준 당일 일봉은 제외합니다. 실시간·시간외 가격이 아닙니다.",
              f"- 오늘 뉴스 {news['counts']['기사']}건 중 교차확인 {news['counts']['교차확인']}건, "
              f"본문 확보 {news['counts']['본문확보']}건. 본문을 못 읽은 기사는 요약·판단하지 않습니다.",
              "- 투자 자문이 아닙니다."]
    if problems:
        lines += ["", "### 확인 필요", ""] + [
            f"- {row['name']} ({row['ticker']}): {row['status']} · {row.get('error', '교차검증 불일치')}"
            for row in problems]
    if news.get("errors"):
        lines += [f"- 뉴스 수집 오류: {message}" for message in news["errors"]]

    text = "\n".join(lines) + "\n"
    (HERE / "briefing.md").write_text(text, encoding="utf-8")
    (HERE / "briefing.html").write_text(render_html(now, prices, news), encoding="utf-8")
    print(text)
    print(f"저장: briefing.md, briefing.html", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
