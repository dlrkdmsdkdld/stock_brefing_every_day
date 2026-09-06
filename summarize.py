"""수집한 기사 본문을 읽고 요약·호재/악재 판단을 만들어 verdicts.json에 쓴다.

OpenAI API를 호출한다. 에이전트 루프를 돌지 않으므로 비용과 실행 시간이 예측 가능하다.
본문이 없는 기사는 아예 보내지 않는다(제목만으로 판단하지 않기 위해).
기사 수가 많으면 여러 묶음으로 나눠 호출한다.

환경변수
  OPENAI_API_KEY   platform.openai.com에서 발급 (필수). .env에 적어둬도 된다.
  SUMMARY_MODEL    기본 gpt-5.6-luna
  BATCH_SIZE       한 번에 보낼 기사 수 (기본 12)
"""
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import OpenAI

KST = ZoneInfo("Asia/Seoul")
HERE = Path(__file__).parent
MODEL = os.getenv("SUMMARY_MODEL", "gpt-5.6-luna")
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "12"))
STANCES = ["호재", "약한 호재", "중립", "약한 악재", "악재"]
ANALYST = "기사 본문을 읽고 모델이 작성. 키워드 감성분석이 아니며 투자 자문이 아님."

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


def collect(news):
    """본문이 있는 기사만 골라 모델에 보낼 형태로 만든다."""
    targets = []
    for name, row in news["news"].items():
        for story in row["stories"]:
            if story.get("body"):
                targets.append(dict(id=len(targets), holding=name,
                                    title=story["title"], body=story["body"]))
    return targets


def ask(client, batch):
    payload = "\n\n".join(
        f"### id={row['id']} | 보유종목: {row['holding']}\n제목: {row['title']}\n본문:\n{row['body']}"
        for row in batch)
    response = client.responses.create(
        model=MODEL,
        instructions=INSTRUCTIONS,
        input=payload,
        text={"format": {"type": "json_schema", "name": "verdicts",
                         "schema": SCHEMA, "strict": True}},
    )
    usage = response.usage
    return (json.loads(response.output_text)["verdicts"],
            usage.input_tokens, usage.output_tokens)


def main():
    load_env()
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY 없음 (platform.openai.com에서 발급)")

    news = json.loads((HERE / "news.json").read_text(encoding="utf-8"))
    targets = collect(news)
    total = sum(len(row["stories"]) for row in news["news"].values())
    if not targets:
        sys.exit(f"본문 있는 기사가 없습니다 (수집 {total}건)")

    client = OpenAI()
    by_id = {row["id"]: row for row in targets}
    verdicts, tokens_in, tokens_out, failures = {}, 0, 0, []

    for start in range(0, len(targets), BATCH_SIZE):
        batch = targets[start:start + BATCH_SIZE]
        label = f"{start + 1}~{start + len(batch)}"
        try:
            items, used_in, used_out = ask(client, batch)
            tokens_in += used_in
            tokens_out += used_out
        except Exception as exc:
            # 한 묶음이 실패해도 나머지는 살린다.
            failures.append(f"{label}: {type(exc).__name__}: {str(exc)[:120]}")
            print(f"[경고] {label} 실패: {type(exc).__name__}", file=sys.stderr)
            continue
        for item in items:
            source = by_id.get(item["id"])
            if source:
                verdicts[normalize(source["title"])] = dict(
                    title=source["title"], holding=source["holding"], stance=item["stance"],
                    summary=item["summary"], impact=item["impact"], caution=item["caution"])
        print(f"  {label} 완료 ({len(items)}건)")

    done = {normalize(row["title"]) for row in targets} & set(verdicts)
    missing = [row["title"] for row in targets if normalize(row["title"]) not in done]

    (HERE / "verdicts.json").write_text(json.dumps(dict(
        generated_at=datetime.now(KST).isoformat(timespec="seconds"),
        analyst=ANALYST, model=MODEL,
        usage=dict(input_tokens=tokens_in, output_tokens=tokens_out),
        skipped=dict(본문없음=total - len(targets), 응답누락=missing, 호출실패=failures),
        verdicts=verdicts), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"요약 {len(verdicts)}건 / 대상 {len(targets)}건 (본문 없어 제외 {total - len(targets)}건)")
    print(f"모델 {MODEL} · 입력 {tokens_in:,} · 출력 {tokens_out:,} 토큰")
    if missing:
        print(f"[경고] 빠진 기사 {len(missing)}건: {missing[:3]}", file=sys.stderr)
    return 1 if missing or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
