"""보유 종목별로 '오늘자'(한국시간) 뉴스를 모아 상호검증하고 본문을 붙인다.

출처
  국내: 네이버 금융 종목뉴스 + 구글 뉴스 RSS(ko)
  해외: yfinance(Yahoo Finance) + 구글 뉴스 RSS(en)

날짜는 검색어가 아니라 제공처가 주는 발행 시각으로 확정하므로 어제 기사가 섞이지 않는다.
제목이 사실상 같은 기사는 한 묶음으로 합치고, 두 출처 이상에 나타나면 cross_confirmed로 표시한다.
종목 목록은 holdings.json에서 읽는다.
"""
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

import article
from holdings import HOLDINGS  # 뉴스는 보유 종목만 수집한다

KST = ZoneInfo("Asia/Seoul")
AGENT = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
           "Accept-Language": "ko,en;q=0.8"}
PER_HOLDING = 3        # 종목당 뉴스 건수
SAME_STORY = 0.72      # 제목 유사도가 이 이상이면 같은 기사로 본다.
FEED_LIMIT = 30
BODY_LIMIT = 2500


def clean(text):
    return html.unescape(re.sub("<[^>]+>", "", text or "")).strip()


def normalize(title):
    """제목 비교용 키. 공백·기호·대소문자 차이를 무시한다."""
    return re.sub(r"[^0-9a-z가-힣]", "", html.unescape(title or "").lower())


def relevance(title, keywords):
    """제목에 종목이 실제 언급됐는지. 제공처가 태그만 붙인 기사와 구분한다.

    영문 키워드는 대소문자를 지켜서 찾는다. 소문자까지 허용하면 'First Solar'가
    'the first solar panel recycling program' 같은 무관한 제목에 걸린다.
    """
    text = title or ""
    for word in keywords:
        if word.isascii():
            if re.search(rf"\b{re.escape(word)}\b", text):
                return "direct"
        elif word in text:
            return "direct"
    return "tagged"


def get(url, headers=AGENT):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=25) as body:
        return body.read()


def entry(item, source, title, publisher, when, url):
    return dict(source=source, title=title, publisher=publisher, url=url,
                published_kst=when.astimezone(KST).isoformat(timespec="minutes"),
                published_utc=when.astimezone(timezone.utc).isoformat(timespec="minutes"),
                relevance=relevance(title, item["keywords"]))


def naver_news(item):
    payload = json.loads(get(
        f"https://m.stock.naver.com/api/news/stock/{item['ticker']}?pageSize=60&page=1"))
    rows = []
    for group in payload:
        for article in group.get("items", []):
            when = datetime.strptime(article["datetime"], "%Y%m%d%H%M").replace(tzinfo=KST)
            rows.append(entry(item, "네이버 금융",
                              clean(article.get("titleFull") or article["title"]),
                              article.get("officeName"), when, article.get("mobileNewsUrl")))
    return rows


def yahoo_news(item, summaries):
    rows = []
    for article in yf.Ticker(item["ticker"]).get_news(count=40) or []:
        content = article.get("content") or article
        stamp = content.get("pubDate") or content.get("displayTime")
        if not stamp:
            continue
        title = clean(content.get("title"))
        summary = clean(content.get("summary"))
        if summary:
            summaries[normalize(title)] = summary
        link = (content.get("clickThroughUrl") or content.get("canonicalUrl") or {}).get("url")
        rows.append(entry(item, "Yahoo Finance", title,
                          (content.get("provider") or {}).get("displayName"),
                          datetime.fromisoformat(stamp.replace("Z", "+00:00")), link))
    return rows


def google_news(item):
    locale = "hl=ko&gl=KR&ceid=KR:ko" if item["market"] == "국내" else "hl=en-US&gl=US&ceid=US:en"
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(f"{item['query']} when:1d") + "&" + locale)
    feed = ET.fromstring(get(url, {"User-Agent": BROWSER["User-Agent"]}))
    rows = []
    for node in feed.findall(".//item")[:FEED_LIMIT]:
        stamp = node.findtext("pubDate")
        if not stamp:
            continue
        source = node.find("source")
        publisher = source.text if source is not None else None
        title = clean(node.findtext("title"))
        # 구글 RSS 제목은 "제목 - 매체명" 형태라 매체명 꼬리를 떼어 다른 출처와 비교한다.
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].strip()
        rows.append(entry(item, "구글 뉴스 RSS", title, publisher,
                          parsedate_to_datetime(stamp), node.findtext("link")))
    return rows


def collect(item, summaries):
    """한 종목의 모든 출처를 훑는다. 출처 하나가 죽어도 나머지는 살린다."""
    rows, errors = [], []
    primary = (lambda: naver_news(item)) if item["market"] == "국내" else (
        lambda: yahoo_news(item, summaries))
    for label, worker in (("주출처", primary), ("구글", lambda: google_news(item))):
        try:
            rows.extend(worker())
        except Exception as exc:
            errors.append(f"{item['name']}/{label}: {type(exc).__name__}: {exc}")
    return rows, errors


def cluster(rows):
    """제목이 사실상 같은 기사를 한 묶음으로 합쳐 출처별로 정리한다."""
    stories = []
    for row in sorted(rows, key=lambda row: row["published_kst"]):
        key = normalize(row["title"])
        if not key:
            continue
        for story in stories:
            if SequenceMatcher(None, key, story["key"]).ratio() >= SAME_STORY:
                break
        else:
            story = dict(key=key, title=row["title"], published_kst=row["published_kst"],
                         relevance="tagged", sources={})
            stories.append(story)
        if row["relevance"] == "direct":
            story["relevance"] = "direct"
        # 같은 출처의 중복은 첫 기사만 남긴다.
        story["sources"].setdefault(row["source"], dict(
            publisher=row["publisher"], url=row["url"], published_kst=row["published_kst"]))
    for story in stories:
        story.pop("key")
        story["source_count"] = len(story["sources"])
        story["verification"] = "cross_confirmed" if story["source_count"] > 1 else "single_source"
        # 구글 RSS 링크는 자바스크립트 리다이렉트라 본문을 못 읽는다. 본문을 붙일 수 있는 기사를 우대한다.
        story["explainable"] = any(key in story["sources"] for key in ("네이버 금융", "Yahoo Finance"))
    return stories


def pick(rows, today):
    """오늘자만 남기고 종목 직접 언급 > 본문 확보 가능 > 상호검증 > 최신순으로 고른다.

    요약할 수 없는 기사는 브리핑에서 값이 낮으므로, 본문을 붙일 수 있는지를 교차검증보다 앞에 둔다.
    """
    stories = [story for story in cluster(rows) if story["published_kst"][:10] == today.isoformat()]
    stories.sort(key=lambda story: (story["relevance"] == "direct", story["explainable"],
                                    story["source_count"] > 1, story["published_kst"]), reverse=True)
    return stories


def attach_body(story, summaries):
    """기사 본문을 붙인다. 잘 읽히는 순서대로 시도하고 실패는 숨기지 않는다.

    1) 네이버 뉴스 원문  2) 구글 링크를 실제 매체 URL로 복원해 원문 추출
    3) Yahoo가 준 기사 URL에서 직접 추출  4) Yahoo 요약문
    """
    failures = []
    naver = story["sources"].get("네이버 금융")
    if naver and naver.get("url"):
        try:
            text, _ = article.extract_body(naver["url"])
            story.update(body=text, body_source="네이버 뉴스 본문")
            return story
        except Exception as exc:
            failures.append(f"네이버: {type(exc).__name__}")

    google = story["sources"].get("구글 뉴스 RSS")
    if google and google.get("url"):
        try:
            text, kind, url = article.body_from_google(google["url"])
            story.update(body=text, body_source=kind, article_url=url)
            return story
        except Exception as exc:
            failures.append(f"구글복원: {type(exc).__name__}")

    yahoo = story["sources"].get("Yahoo Finance")
    if yahoo and yahoo.get("url"):
        try:
            text, kind = article.extract_body(yahoo["url"])
            story.update(body=text, body_source=f"{kind} · Yahoo 링크")
            return story
        except Exception as exc:
            failures.append(f"야후: {type(exc).__name__}")

    summary = summaries.get(normalize(story["title"]))
    if summary:
        story.update(body=summary, body_source="Yahoo Finance 기사 요약")
    else:
        story["body_error"] = "본문 추출 실패 · " + ", ".join(failures or ["원문 링크 없음"])
    return story


def for_holding(item, summaries, today):
    rows, errors = collect(item, summaries)
    stories = pick(rows, today)[:PER_HOLDING]
    with ThreadPoolExecutor(max_workers=PER_HOLDING) as pool:
        stories = list(pool.map(lambda story: attach_body(story, summaries), stories))
    return item, stories, len(rows), errors


def main():
    yf.set_tz_cache_location(str(Path(__file__).parent / ".cache" / "yfinance"))
    now = datetime.now(KST)
    today = now.date()
    summaries, results, errors = {}, {}, []

    # 종목이 늘어나도 전체 시간이 늘지 않도록 종목 단위로 병렬 처리한다.
    with ThreadPoolExecutor(max_workers=5) as pool:
        for item, stories, candidates, failures in pool.map(
                lambda item: for_holding(item, summaries, today), HOLDINGS):
            results[item["name"]] = dict(ticker=item["ticker"], market=item["market"],
                                         candidates=candidates, stories=stories)
            errors.extend(failures)

    total = sum(len(row["stories"]) for row in results.values())
    confirmed = sum(story["source_count"] > 1 for row in results.values() for story in row["stories"])
    bodied = sum("body" in story for row in results.values() for story in row["stories"])
    output = Path(__file__).parent / "news.json"
    output.write_text(json.dumps(dict(
        fetched_at=now.isoformat(timespec="seconds"), today_kst=today.isoformat(),
        rule=f"발행 시각(KST) 기준 오늘 기사만, 종목당 최대 {PER_HOLDING}건. "
             "2개 이상 출처에 나타나면 cross_confirmed.",
        sources=dict(국내=["네이버 금융", "구글 뉴스 RSS"], 해외=["Yahoo Finance", "구글 뉴스 RSS"]),
        counts=dict(종목=len(HOLDINGS), 기사=total, 교차확인=confirmed, 본문확보=bodied),
        errors=errors, news=results), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"오늘(KST) {today} · {len(HOLDINGS)}종목 / 기사 {total}건 "
          f"(교차확인 {confirmed} · 본문 {bodied})")
    for name, row in results.items():
        if not row["stories"]:
            print(f"  {name}: 오늘자 기사 없음 (후보 {row['candidates']}건, 임의 대체 없음)")
            continue
        print(f"  {name} ({row['ticker']})")
        for story in row["stories"]:
            mark = "교차확인" if story["source_count"] > 1 else "단일출처"
            tag = "" if story["relevance"] == "direct" else " [태그만]"
            print(f"    - {story['published_kst'][11:16]} {story['title'][:58]}{tag} · {mark}")
    for message in errors:
        print(f"수집 오류: {message}", file=sys.stderr)
    print(f"저장: {output}")
    empty = [name for name, row in results.items() if not row["stories"]]
    return 1 if empty or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
