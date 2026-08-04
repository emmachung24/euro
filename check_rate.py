"""
유로 환율이 목표가 아래로 떨어지면 텔레그램으로 알림을 보낸다.
GitHub Actions에서 1시간마다 실행됨.

- 외부 라이브러리 없음 (표준 라이브러리만 사용 → pip install 불필요)
- 목표 환율은 워크플로 yml 파일의 TARGET_RATE 값으로 조절
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------- 설정 ----------
TARGET = float(os.environ.get("TARGET_RATE", "1580"))  # 이 값 이하로 내려가면 알림
STATE_FILE = "state.json"
KST = timezone(timedelta(hours=9))

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


# ---------- 유틸 ----------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8"))


def fetch_rate():
    """환율 소스를 순서대로 시도. 앞의 게 실패하면 다음 걸로 넘어간다."""
    sources = [
        (
            "NAVER(수시)",
            "https://api.stock.naver.com/marketindex/exchange/FX_EURKRW",
            lambda d: float(str(d["closePrice"]).replace(",", "")),
        ),
        (
            "NAVER(종가)",
            "https://api.stock.naver.com/marketindex/exchange/FX_EURKRW/prices?page=1&pageSize=1",
            lambda d: float(str(d[0]["closePrice"]).replace(",", "")),
        ),
        (
            "er-api(일 1회)",
            "https://open.er-api.com/v6/latest/EUR",
            lambda d: float(d["rates"]["KRW"]),
        ),
    ]

    errors = []
    for name, url, parse in sources:
        try:
            rate = parse(get_json(url))
            if rate > 0:
                return rate, name
        except Exception as exc:
            errors.append(f"  - {name}: {exc}")

    raise RuntimeError("모든 환율 소스 실패:\n" + "\n".join(errors))


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as res:
        res.read()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"below": False}


# ---------- 메인 ----------
def main():
    rate, source = fetch_rate()
    state = load_state()

    was_below = state.get("below", False)
    is_below = rate <= TARGET
    now = datetime.now(KST).strftime("%m/%d %H:%M")

    # 목표가 아래로 "처음 내려왔을 때"만 알림 → 중복 알림 방지
    if is_below and not was_below:
        send_telegram(
            f"하원아 환전하렴? 🐣\n\n"
            f"현재  {rate:,.2f}원\n"
            f"목표  {TARGET:,.0f}원\n\n"
            f"{source} · {now} KST"
        )
        print(f"[알림 전송] {rate}")
    elif was_below and not is_below:
        send_telegram(
            f"🥺 유로가 다시 올라갔어욤.\n\n"
            f"현재  {rate:,.2f}원\n"
            f"{source} · {now} KST"
        )
        print(f"[복귀 알림] {rate}")
    else:
        print(f"[조용히 통과] {rate} (목표 {TARGET}, 하락상태={is_below})")

    state.update(
        {
            "below": is_below,
            "last_rate": rate,
            "target": TARGET,
            "source": source,
            "checked_at": now,
        }
    )
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
