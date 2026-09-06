# 보유 주식 가격·뉴스 브리핑

매일 아침 보유 종목의 종가와 오늘자 뉴스를 모아 요약·판단까지 붙인 브리핑을 만듭니다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./daily.sh          # 전체 파이프라인 (가격 -> 뉴스 -> 요약 -> 브리핑)
```

개별 실행:

```sh
.venv/bin/python prices.py   # 종가        -> prices.json
.venv/bin/python news.py     # 오늘자 뉴스  -> news.json
.venv/bin/python brief.py    # 브리핑       -> briefing.md, briefing.html
.venv/bin/python notify.py   # 텔레그램 전송
```

## 종목 추가·삭제 (`holdings.json`)

종목 목록은 `holdings.json` 한 곳에만 있습니다. 여기에 한 줄 추가하면
가격 조회, 뉴스 수집, 브리핑이 모두 따라갑니다.

```json
{"name": "엔비디아", "ticker": "NVDA", "currency": "USD",
 "keywords": ["Nvidia", "NVDA"], "query": "\"Nvidia\" stock"}
```

- `currency`가 `KRW`면 국내(pykrx + NXT + 네이버 뉴스), 아니면 해외(Yahoo)로 처리합니다.
- `keywords`는 기사 제목에 이 말이 있으면 "직접 언급"으로 봅니다.
  영문 키워드는 대소문자를 지켜 단어 단위로 찾습니다. 소문자까지 허용하면
  `First Solar`가 `the first solar panel recycling program` 같은 무관한 제목에 걸립니다.
- `query`는 구글 뉴스 RSS 검색어입니다. 해외는 큰따옴표로 묶어야 정확도가 오릅니다.

## 가격 (`prices.py`)

국내는 pykrx(KRX)가 1차 출처이고, 같은 거래일의 Yahoo·네이버 종가와 교차 검증해
`match` / `mismatch` / `unavailable`을 기록합니다. 해외는 Yahoo 단일 출처입니다.
시장 현지 날짜 기준 당일 일봉은 장중 값일 수 있어 항상 제외하며, 직전 거래일 대비
등락률(`change_pct`)을 함께 계산합니다. 7일 초과 데이터는 `stale`로 표시합니다.

국내 종목에는 넥스트레이드(NXT) 최종 체결가를 `nxt_price` / `nxt_change_pct`로 덧붙입니다.
NXT는 애프터마켓이 20:00까지라 15:30에 끝나는 KRX 종가와 다릅니다
(9/4 삼성전자 KRX 255,500 / NXT 257,000). pykrx와 네이버 종목 API 모두 NXT를 제공하지 않아
네이버 금융 종목 페이지의 NXT 탭을 파싱하는데, 그 화면에는 거래일이 없습니다.
그래서 등락률로 역산한 전일 종가가 KRX 전일 종가와 0.2% 이내로 맞을 때만 표시하고
어긋나면 `nxt_error`로 남깁니다. 등락률·정렬·검증의 기준은 KRX 종가입니다.

pykrx 1.2.8은 임포트 시 `KRX 로그인 실패...` 메시지를 출력하지만 종목별 일별 시세
(`get_market_ohlcv_by_date`) 조회에는 계정이 필요 없습니다. `adjusted=False`(원주가)와
전종목 시세는 계정이 필요해 쓰지 않으며, 국내 종가는 수정주가 기준입니다.

## 뉴스 (`news.py`)

**종목당 최대 3건**을 모읍니다. 출처는 국내가 네이버 금융 종목뉴스 + 구글 뉴스 RSS(ko),
해외가 Yahoo Finance + 구글 뉴스 RSS(en)입니다. 종목 단위로 병렬 처리하므로
종목이 늘어도 전체 소요 시간은 크게 늘지 않습니다(18종목 기준 약 6초).

날짜는 검색어가 아니라 제공처가 주는 발행 시각으로 확정합니다. 네이버는 `datetime`(KST),
Yahoo는 `pubDate`(UTC), 구글 RSS는 `pubDate`(GMT)를 KST로 변환해 비교하므로
미국 현지 기준 어제 저녁 기사도 한국시간 오늘이면 포함됩니다.

제목을 정규화해 유사도 0.72 이상이면 같은 기사로 묶고, 두 출처 이상에서 나타나면
`cross_confirmed`, 한 곳에서만 나오면 `single_source`로 표시합니다.
정렬은 **종목 직접 언급 > 본문 확보 가능 > 교차확인 > 최신순**입니다.
요약할 수 없는 기사는 값이 낮으므로 본문 확보 가능 여부를 교차검증보다 앞에 둡니다.

## 본문 추출 (`article.py`)

고른 기사에는 실제 본문을 붙입니다. 잘 읽히는 순서대로 시도하고, 실패는 `body_error`로 남깁니다.

1. **네이버 뉴스 원문** — 국내 기사
2. **구글 링크 복원 후 원문** — 구글 RSS 링크는 `AU_yqL...` 형태의 암호화 리다이렉트라
   그냥은 못 엽니다. 기사 페이지에서 `data-n-a-id` / `data-n-a-ts` / `data-n-a-sg`를 읽어
   구글의 `batchexecute` 엔드포인트로 실제 매체 URL을 복원한 뒤 그 페이지에서 본문을 뽑습니다.
3. **Yahoo 기사 URL에서 직접 추출**
4. **Yahoo가 주는 요약문** — 위가 모두 막혔을 때

본문 영역은 `#dic_area`(네이버), `#article-view-content-div`(국내 언론사 CMS 표준),
`itemprop="articleBody"`, `.entry-content`, `<article>` 등을 순서대로 찾고,
없으면 문단 태그를 모으고, 그것도 안 되면 `og:description`을 씁니다.
태그 중첩을 세어 본문 영역만 정확히 잘라내며 최대 2,500자로 자릅니다.

2026-09-06 기준 49건 중 **46건 본문 확보**(네이버 7, 복원 원문 38, 메타 설명 1).
못 읽은 기사는 브리핑에 `제목만 확보`로 표시하고 요약·판단을 하지 않습니다.

## 요약·호재/악재 판단 (`prompts/summarize.md` -> `verdicts.json`)

키워드 감성분석은 오답이 많아 쓰지 않습니다. `claude -p`로 Claude를 헤드리스 실행해
`news.json`의 본문을 읽히고 `verdicts.json`에 기사 제목 기준으로 기록합니다.
`claude` CLI의 기존 로그인을 쓰므로 `ANTHROPIC_API_KEY`가 따로 필요 없습니다.

각 항목은 `summary`(내용 2~3문장 요약), `stance`(호재/약한 호재/중립/약한 악재/악재),
`impact`(그렇게 보는 이유), `caution`(근거의 한계·반대 시각)을 가집니다.
**본문이 없는 기사는 판단하지 않습니다** — 제목만으로 호재·악재를 붙이지 않기 위해서입니다.
투자 자문이 아닙니다.

## 매일 자동 실행 (GitHub Actions)

`.github/workflows/daily-brief.yml`이 **월~금 07:30 KST**에 실행됩니다
(cron은 UTC라 `30 22 * * 0-4`). 내 컴퓨터가 꺼져 있어도 돌아갑니다.
GitHub의 예약 실행은 혼잡할 때 수십 분 늦어질 수 있습니다.

가격 -> 뉴스 -> Claude 요약 -> 브리핑 -> 텔레그램 전송 순으로 돌고, 결과(`briefing.md`, `briefing.html`,
`prices.json`, `news.json`, `verdicts.json`)를 저장소에 커밋하며 실행 아티팩트로도 첨부합니다.
각 단계는 `continue-on-error`라 한 단계가 실패해도 나머지는 진행합니다.

### 최초 설정

1. 인증 토큰 발급 (Claude 구독 계정 사용, API 키 불필요):

   ```sh
   claude setup-token
   ```

2. 저장소 → Settings → Secrets and variables → Actions → New repository secret
   - 이름: `CLAUDE_CODE_OAUTH_TOKEN`
   - 값: 1번에서 나온 토큰

   이 시크릿이 없으면 요약 단계만 건너뛰고 가격·뉴스·브리핑은 정상 생성됩니다.
   텔레그램 전송까지 하려면 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`도 같이 등록하세요.

3. Actions 탭에서 `데일리 브리핑` → `Run workflow`로 수동 실행해 한 번 확인하세요.

시각을 바꾸려면 워크플로의 `cron`을 고치면 됩니다 (UTC 기준, KST = UTC+9).

로컬에서 같은 파이프라인을 직접 돌리려면 `./daily.sh`를 쓰면 되고,
실행 기록은 `logs/날짜.log`에 남습니다.

## 텔레그램 전송 (`notify.py`)

브리핑 전문을 텔레그램으로 보냅니다. 가격 표와 종목별 뉴스 요약·판단을 모두 담고,
텔레그램의 4096자 제한에 맞춰 줄 단위로 나눠 여러 건으로 보낸 뒤 `briefing.html`을 파일로 첨부합니다.

카카오톡은 개인 계정으로는 **'나에게 보내기'** 밖에 안 됩니다. 친구에게 보내기와 카카오톡 채널
메시지는 사업자등록과 앱 심사가 필요합니다. 그래서 텔레그램을 씁니다.

### 최초 설정

1. 텔레그램에서 `@BotFather`에게 `/newbot` → 봇 토큰을 받습니다.
2. 만든 봇과 대화를 시작해 아무 메시지나 한 번 보냅니다(봇은 먼저 말을 걸 수 없습니다).
3. chat id 확인:

   ```sh
   TELEGRAM_BOT_TOKEN=봇토큰 .venv/bin/python notify.py --whoami
   ```

4. GitHub 저장소 시크릿에 `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_CHAT_ID`를 등록합니다.
   시크릿이 없으면 전송 단계만 건너뜁니다.

로컬 테스트:

```sh
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... .venv/bin/python notify.py
```

## 브리핑 (`brief.py`)

`prices.json` + `news.json` + `verdicts.json`을 합쳐 `briefing.md`와 `briefing.html`을 만듭니다.
상승/하락 종목 수와 평균 등락률, 국내·해외 종가 표(국내는 NXT 병기),
종목별 뉴스와 요약·판단, 데이터 신뢰도 주석을 한 문서에 담습니다.
오늘자 데이터가 없으면 `prices.py`·`news.py`를 먼저 실행하므로 이것만 돌려도 됩니다.

공식 사용 문서: https://github.com/sharebook-kr/pykrx , https://ranaroussi.github.io/yfinance/
