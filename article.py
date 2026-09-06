"""기사 원문 본문 추출.

구글 뉴스 RSS 링크는 암호화된 리다이렉트라 그대로는 본문을 읽을 수 없다.
구글이 쓰는 URL 복원 엔드포인트(batchexecute)로 실제 매체 URL을 얻은 뒤,
언론사 페이지에서 본문 영역을 찾아 텍스트만 남긴다.
"""
import html
import json
import re
import os
import urllib.parse
import urllib.request

BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
           "Accept-Language": "ko,en;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
BODY_LIMIT = int(os.getenv("BODY_LIMIT", "900"))
MIN_BODY = 200
DECODE_API = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# 본문 영역 후보. 앞에 있는 것부터 시도한다.
# #dic_area는 네이버, #article-view-content-div는 국내 언론사 CMS에서 가장 흔한 표준이다.
CONTAINERS = [
    r'id="dic_area"', r'id="newsct_article"', r'id="article-view-content-div"',
    r'itemprop="articleBody"', r'id="articleBody"', r'id="articleBodyContents"',
    r'class="[^"]*article[-_]?(?:body|content|view)[^"]*"',
    r'class="[^"]*news[-_]?(?:body|content|end)[^"]*"', r'class="[^"]*entry-content[^"]*"',
    r'class="[^"]*post-content[^"]*"', r'<article[ >]',
]


def fetch(url, timeout=20):
    request = urllib.request.Request(url, headers=BROWSER)
    with urllib.request.urlopen(request, timeout=timeout) as body:
        raw = body.read()
    charset = re.search(rb'charset=["\']?([\w-]+)', raw[:3000])
    encoding = charset.group(1).decode("ascii", "ignore").lower() if charset else "utf-8"
    if encoding in ("euc-kr", "ks_c_5601-1987"):
        encoding = "cp949"
    try:
        return raw.decode(encoding, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def decode_google_url(link, timeout=20):
    """구글 뉴스 링크를 실제 매체 URL로 복원한다."""
    page = fetch(link, timeout)
    article_id = re.search(r'data-n-a-id="([^"]+)"', page)
    stamp = re.search(r'data-n-a-ts="(\d+)"', page)
    signature = re.search(r'data-n-a-sg="([^"]+)"', page)
    if not (article_id and stamp and signature):
        raise ValueError("구글 페이지에서 복원 서명을 못 찾음")
    inner = json.dumps(["garturlreq",
                        [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                          None, None, None, None, None, 0, 1],
                         "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                        article_id.group(1), int(stamp.group(1)), signature.group(1)])
    payload = urllib.parse.urlencode(
        {"f.req": json.dumps([[["Fbv4je", inner, None, "1"]]])}).encode()
    request = urllib.request.Request(
        DECODE_API, data=payload,
        headers={**BROWSER, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    with urllib.request.urlopen(request, timeout=timeout) as body:
        raw = body.read().decode("utf-8", "replace")
    for line in raw.splitlines():
        if "garturlres" in line:
            return json.loads(json.loads(line)[0][2])[1]
    raise ValueError("복원 응답에 URL 없음")


def strip(fragment):
    text = re.sub(r"<(script|style|figcaption|aside|nav|footer)[^>]*>.*?</\1>", " ",
                  fragment, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def slice_container(page, pattern):
    """여는 태그를 찾아 같은 종류의 태그 중첩을 세며 닫는 지점까지 잘라낸다."""
    match = re.search(pattern, page, re.I)
    if not match:
        return None
    start = page.rfind("<", 0, match.start())
    tag = re.match(r"<(\w+)", page[start:])
    if not tag:
        return None
    name = tag.group(1)
    depth, index = 0, start
    for token in re.finditer(rf"<{name}[ >]|</{name}>", page[start:], re.I):
        depth += 1 if token.group(0).startswith(f"</") is False else -1
        index = start + token.end()
        if depth == 0:
            return page[start:index]
    return page[start:start + 60000]


def extract_body(url, timeout=20):
    """언론사 페이지에서 본문을 뽑는다. 못 뽑으면 메타 설명이라도 돌려준다."""
    page = fetch(url, timeout)
    best = ""
    for pattern in CONTAINERS:
        fragment = slice_container(page, pattern)
        if not fragment:
            continue
        text = strip(fragment)
        if len(text) > len(best):
            best = text
        if len(best) >= MIN_BODY * 3:
            break
    if len(best) < MIN_BODY:
        # 본문 컨테이너를 못 찾으면 문단 태그를 모아 본다.
        paragraphs = [strip(block) for block in re.findall(r"<p[ >].*?</p>", page, re.S | re.I)]
        joined = "\n".join(part for part in paragraphs if len(part) > 40)
        if len(joined) > len(best):
            best = joined
    if len(best) < MIN_BODY:
        meta = re.search(
            r'<meta[^>]+(?:property="og:description"|name="description")[^>]+content="([^"]{40,})"',
            page, re.I)
        if meta:
            return html.unescape(meta.group(1)).strip()[:BODY_LIMIT], "메타 설명(본문 추출 실패)"
        raise ValueError(f"본문을 찾지 못함(추출 {len(best)}자)")
    return best[:BODY_LIMIT], "원문 본문"


def body_from_google(link, timeout=20):
    """구글 링크 -> 실제 URL -> 본문."""
    url = decode_google_url(link, timeout)
    text, kind = extract_body(url, timeout)
    return text, f"{kind} · {urllib.parse.urlparse(url).netloc}", url
