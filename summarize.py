"""수집한 기사 본문을 읽고 요약·호재/악재 판단을 만들어 verdicts.json에 쓴다.

에이전트 루프를 돌지 않고 API를 직접 호출하므로 비용과 실행 시간이 예측 가능하다.
본문이 없는 기사는 아예 보내지 않는다(제목만으로 판단하지 않기 위해).

제공처는 순서대로 시도한다. 앞 제공처가 한도(429)에 걸리면 다음으로 넘어가고,
한 번 넘어가면 남은 묶음도 계속 그 제공처를 쓴다.
  1) OpenAI            OPENAI_API_KEY (기본 gpt-5.6-luna, Responses API)
  2) 예비 제공처        OpenAI 호환 엔드포인트면 무엇이든. 기본 설정은 Google Gemini 무료 티어.
                       FALLBACK_API_KEY 또는 GEMINI_API_KEY가 있어야 활성화된다.

환경변수
  OPENAI_API_KEY     platform.openai.com 발급. .env에 적어둬도 된다.
  SUMMARY_MODEL      기본 gpt-5.6-luna
  GEMINI_API_KEY     aistudio.google.com 발급(무료, 카드 불필요)
  FALLBACK_API_KEY   예비 제공처 키. 없으면 GEMINI_API_KEY를 쓴다.
  FALLBACK_BASE_URL  기본 https://generativelanguage.googleapis.com/v1beta/openai/
  FALLBACK_MODEL     기본 gemini-2.5-flash
  BATCH_SIZE         한 번에 보낼 기사 수 (기본 8)
  BATCH_PAUSE        묶음 사이 대기 초 (기본 8)

인자
  --alerts-only      볼린저 하단 이탈 종목의 기사만 요약한다.
"""
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI, RateLimitError

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
MODEL = os.getenv("SUMMARY_MODEL", "gpt-5.6-luna")
FALLBACK_BASE_URL = os.getenv(
    "FALLBACK_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "gemini-2.5-flash")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "8"))
BATCH_PAUSE = float(os.getenv("BATCH_PAUSE", "8"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "2"))
STANCES = ["호재", "약한 호재", "중립", "약한 악재", "악재"]

INSTRUCTIONS = f"""너는 개인 투자자의 보유 종목 뉴스를 정리한다.
주어진 기사들을 읽고 각 기사마다 다음을 한국어로 작성해라.

- summary: 기사 내용 2~3문장 요약. 본문에 있는 숫자·기업명·날짜 같은 구체적 사실을 반드시 포함한다.
- stance: 해당 보유 종목 관점에서 {" / ".join(STANCES)} 중 하나.
- impact: 왜 그 stance인지 1~2문장. 보유 종목에 영향을 주는 경로를 구체적으로 쓴다.
- caution: 근거의 한계나 반대로 볼 여지. 없으면 빈 문자열.

규칙:
- 본문에 없는 수치나 사건을 지어내지 않는다. 확신이 없으면 "중립"으로 두고 caution에 이유를 쓴다.
- 기사 주제가 보유 종목이 아니라 다른 회사이고 보유 종목은 언급만 된 경우 "중립"으로 하고 그 사실을 caution에 적는다.
- 본문이 광고·사이트 안내문만 있고 기사 내용이 없으면 "중립"으로 두고 caution에 그렇게 적는다.
- 입력으로 준 모든 기사에 대해 하나씩, id를 그대로 써서 결과를 낸다. 빠뜨리지 않는다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "stance": {"type": "string", "enum": STANCES},
                    "summary": {"type": "string"},
                    "impact": {"type": "string"},
                    "caution": {"type": "string"},
                },
                "required": ["id", "stance", "summary", "impact", "caution"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def load_env():
    """로컬 실행 편의를 위해 .env가 있으면 읽는다. 이미 설정된 환경변수가 우선."""
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def normalize(title):
    return re.sub(r"[^0-9a-z가-힣]", "", html.unescape(title or "").lower())


def collect(news, only_alerts=False):
    """본문이 있는 기사만 골라 모델에 보낼 형태로 만든다."""
    targets = []
    groups = ((news.get("alerts", {}),) if only_alerts
              else (news.get("news", {}), news.get("alerts", {})))
    for group in groups:
        for name, row in group.items():
            for story in row["stories"]:
                if story.get("body"):
                    targets.append(dict(id=len(targets), holding=name,
                                        title=story["title"], body=story["body"]))
    return targets


def providers():
    """쓸 수 있는 제공처를 우선순위대로 만든다."""
    chain = []
    if os.getenv("OPENAI_API_KEY"):
        chain.append(dict(label=f"OpenAI/{MODEL}", model=MODEL, style="responses",
                          client=OpenAI(max_retries=3, timeout=180.0)))
    spare = os.getenv("FALLBACK_API_KEY") or os.getenv("GEMINI_API_KEY")
    if spare:
        chain.append(dict(label=f"예비/{FALLBACK_MODEL}", model=FALLBACK_MODEL, style="chat",
                          client=OpenAI(api_key=spare, base_url=FALLBACK_BASE_URL,
                                        max_retries=3, timeout=180.0)))
    return chain


def parse(text):
    """모델이 코드펜스를 붙여 보내는 경우까지 감안해 JSON을 꺼낸다."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.S)
    return json.loads(text)["verdicts"]


def once(provider, payload):
    """제공처 한 곳에 한 번 요청한다."""
    client, model = provider["client"], provider["model"]
    if provider["style"] == "responses":
        response = client.responses.create(
            model=model, instructions=INSTRUCTIONS, input=payload,
            text={"format": {"type": "json_schema", "name": "verdicts",
                             "schema": SCHEMA, "strict": True}})
        usage = response.usage
        return parse(response.output_text), usage.input_tokens, usage.output_tokens

    # OpenAI 호환 채팅 API. json_schema를 거부하는 제공처가 있어 json_object로 물러선다.
    messages = [{"role": "system", "content": INSTRUCTIONS},
                {"role": "user", "content": payload}]
    formats = [{"type": "json_schema",
                "json_schema": {"name": "verdicts", "schema": SCHEMA, "strict": True}},
               {"type": "json_object"}]
    last = None
    for response_format in formats:
        try:
            response = client.chat.completions.create(
                model=model, messages=messages, response_format=response_format)
            usage = response.usage
            return (parse(response.choices[0].message.content),
                    usage.prompt_tokens, usage.completion_tokens)
        except RateLimitError:
            raise
        except Exception as exc:
            last = exc
    raise last


def ask(chain, index, batch):
    """앞 제공처부터 시도한다. 한도(429)면 다음 제공처로 넘어간다.

    돌아오는 index는 다음 묶음부터 쓸 제공처 위치다.
    """
    payload = "\n\n".join(
        f"### id={row['id']} | 보유종목: {row['holding']}\n제목: {row['title']}\n본문:\n{row['body']}"
        for row in batch)
    errors = []
    while index < len(chain):
        provider = chain[index]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                items, used_in, used_out = once(provider, payload)
                return items, used_in, used_out, index, provider["label"]
            except RateLimitError as exc:
                if attempt == MAX_ATTEMPTS:
                    errors.append(f"{provider['label']}: 한도 초과")
                    print(f"    {provider['label']} 한도 초과", file=sys.stderr)
                    break
                delay = min(30, 5 * 2 ** (attempt - 1)) + random.uniform(0, 2)
                print(f"    {provider['label']} 한도, {delay:.0f}초 후 재시도", file=sys.stderr)
                time.sleep(delay)
        index += 1
        if index < len(chain):
            print(f"    -> {chain[index]['label']}(으)로 전환", file=sys.stderr)
    raise RuntimeError("모든 제공처 실패: " + "; ".join(errors))


def main():
    load_env()
    chain = providers()
    if not chain:
        sys.exit("쓸 수 있는 제공처가 없습니다. OPENAI_API_KEY 또는 GEMINI_API_KEY를 설정하세요.")
    print("제공처 순서: " + " -> ".join(item["label"] for item in chain))

    news = json.loads((HERE / "news.json").read_text(encoding="utf-8"))
    targets = collect(news, "--alerts-only" in sys.argv)
    total = sum(len(row["stories"]) for group in ("news", "alerts")
                for row in news.get(group, {}).values())
    if not targets:
        sys.exit(f"본문 있는 기사가 없습니다 (수집 {total}건)")

    by_id = {row["id"]: row for row in targets}
    verdicts, tokens_in, tokens_out, failures, used = {}, 0, 0, [], set()
    index = 0

    for start in range(0, len(targets), BATCH_SIZE):
        batch = targets[start:start + BATCH_SIZE]
        label = f"{start + 1}~{start + len(batch)}"
        if start and BATCH_PAUSE:
            time.sleep(BATCH_PAUSE)
        try:
            items, used_in, used_out, index, provider = ask(chain, index, batch)
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {str(exc)[:160]}")
            print(f"[중단] {label} 실패 - 남은 묶음은 시도하지 않음", file=sys.stderr)
            break
        tokens_in += used_in or 0
        tokens_out += used_out or 0
        used.add(provider)
        for item in items:
            source = by_id.get(item["id"])
            if source:
                verdicts[normalize(source["title"])] = dict(
                    title=source["title"], holding=source["holding"], stance=item["stance"],
                    summary=item["summary"], impact=item["impact"], caution=item["caution"])
        print(f"  {label} 완료 ({len(items)}건, {provider})")

    if not verdicts and failures:
        print("[중단] 요약을 하나도 받지 못해 기존 verdicts.json을 유지합니다.", file=sys.stderr)
        for message in failures:
            print(f"  {message}", file=sys.stderr)
        return 1

    # 이번에 못 받은 기사는 이전 결과가 있으면 그대로 살린다.
    path = HERE / "verdicts.json"
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8")).get("verdicts", {})
        for key, value in previous.items():
            verdicts.setdefault(key, value)

    done = {normalize(row["title"]) for row in targets} & set(verdicts)
    missing = [row["title"] for row in targets if normalize(row["title"]) not in done]

    path.write_text(json.dumps(dict(
        generated_at=datetime.now(KST).isoformat(timespec="seconds"),
        analyst="기사 본문을 읽고 작성. 키워드 감성분석이 아니며 투자 자문이 아님.",
        model=" + ".join(sorted(used)) or "없음",
        usage=dict(input_tokens=tokens_in, output_tokens=tokens_out),
        skipped=dict(본문없음=total - len(targets), 응답누락=missing, 호출실패=failures),
        verdicts=verdicts), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"요약 {len(verdicts)}건 / 대상 {len(targets)}건 (본문 없어 제외 {total - len(targets)}건)")
    print(f"사용 제공처 {', '.join(sorted(used)) or '없음'} · 입력 {tokens_in:,} · 출력 {tokens_out:,} 토큰")
    if missing:
        print(f"[경고] 빠진 기사 {len(missing)}건", file=sys.stderr)
    return 1 if missing or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
