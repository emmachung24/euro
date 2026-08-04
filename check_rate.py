"""
유로 환율 알림 봇

알림 4종류
  1) 기준가 이하로 처음 내려왔을 때
  2) 기준가 아래에서 1원 이상 추가로 떨어질 때마다
  3) 기준가 위로 다시 올라갔을 때
  4) 매일 09/12/15/18/21시 정기 리포트

표준 라이브러리만 사용 (pip install 불필요)
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------- 설정 ----------
TARGET = float(os.environ.get("TARGET_RATE", "1640"))  # 기준 환율
STEP = float(os.environ.get("STEP_WON", "1"))          # 추가 알림 간격(원)
DIGEST_HOURS = {9, 12, 15, 18, 21}                     # 정기 리포트 시각(KST)
DIGEST_AFTER_MINUTE = 7                                # 정각 혼잡 회피

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
    """환율 소스를 순서대로 시도. 앞의 게 실패하면 다음으로 넘어간다."""
    sources = [
        (
            "NAVER(실시간)",
            "https://api.stock.naver.com/marketindex/exchange/FX_EURKRW",
            lambda d: float(str(d["closePrice"]).replace(",", "")),
        ),
        (
            "NAVER(종가)",
            "https://api.stock.naver.com/marketindex/exchange/FX_EURKRW"
            "/prices?page=1&pageSize=1",
            lambda d: float(str(d[0]["closePrice"]).replace(",", "")),
        ),
        (
            "er-api(일1회)",
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
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- 메인 ----------
def main():
    rate, source = fetch_rate()
    now = datetime.now(KST)
    tail = f"{source} · {now.strftime('%m/%d %H:%M')} KST"

    state = load_state()
    was_below = bool(state.get("below", False))
    last_alert = state.get("last_alert_price")
    last_digest = state.get("last_digest", "")

    is_below = rate <= TARGET

    # 1) 기준가 아래로 처음 진입
    if is_below and not was_below:
        send_telegram(
            f"하원아 환전하렴? 🐣\n\n"
            f"현재  {rate:,.2f}원\n"
            f"목표  {TARGET:,.0f}원\n\n"
            f"{tail}"
        )
        last_alert = rate
        print(f"[진입 알림] {rate}")

    # 2) 기준가 아래에서 추가 하락
    elif is_below and was_below:
        if last_alert is None:
            last_alert = rate
            print(f"[기준점 설정] {rate}")
        elif rate <= last_alert - STEP:
            send_telegram(
                f"하원이 얼른 환전하자 🫳🫳\n\n"
                f"현재  {rate:,.2f}원\n"
                f"{tail}"
            )
            last_alert = rate
            print(f"[추가 하락 알림] {rate}")
        else:
            print(f"[하락 유지] {rate} (직전 알림 {last_alert})")

    # 3) 기준가 위로 복귀
    elif not is_below and was_below:
        send_telegram(
            f"🥺 유로가 다시 올라갔어욤.\n\n"
            f"현재  {rate:,.2f}원\n"
            f"{tail}"
        )
        last_alert = None
        print(f"[복귀 알림] {rate}")

    else:
        print(f"[조용히 통과] {rate} (기준 {TARGET})")

    # 4) 정기 리포트
    slot = f"{now:%Y-%m-%d}-{now.hour:02d}"
    if (
        now.hour in DIGEST_HOURS
        and now.minute >= DIGEST_AFTER_MINUTE
        and last_digest != slot
    ):
        send_telegram(
            f"🐥{now:%Y/%m/%d} {now.hour:02d}:00\n\n"
            f"현재  {rate:,.2f}원\n"
            f"{tail}"
        )
        last_digest = slot
        print(f"[정기 리포트] {slot}")

    save_state(
        {
            "below": is_below,
            "last_alert_price": last_alert,
            "last_digest": last_digest,
        }
    )


if __name__ == "__main__":
    main()
