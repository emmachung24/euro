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

NAVER_BASE = "https://api.stock.naver.com/marketindex/exchange"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]


# ---------- 유틸 ----------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8"))


def parse_naver(data):
    """NAVER 고시환율 응답에서 (환율, 고시시각) 추출."""
    info = data.get("exchangeInfo", data)
    raw = info.get("calcPrice") or info.get("closePrice")
    rate = float(str(raw).replace(",", ""))

    quoted = None
    stamp = info.get("localTradedAt")
    if stamp:
        try:
            quoted = datetime.fromisoformat(stamp).astimezone(KST)
        except Exception:
            quoted = None

    return rate, quoted


def fetch_rate():
    """(환율, 소스명, 고시시각) 반환. 앞의 소스가 실패하면 다음으로 넘어간다."""
    errors = []

    # 1~2순위: 은행별 고시환율 (수시 갱신)
    for name, code in (
        ("NAVER(신한)", "FX_EURKRW_SHB"),
        ("NAVER(하나)", "FX_EURKRW"),
    ):
        try:
            rate, quoted = parse_naver(get_json(f"{NAVER_BASE}/{code}"))
            if rate > 0:
                return rate, name, quoted
        except Exception as exc:
            errors.append(f"  - {name}: {exc!r}")
            print(f"[소스 실패] {name}: {exc!r}")

    # 3순위: 일별 종가
    try:
        data = get_json(f"{NAVER_BASE}/FX_EURKRW/prices?page=1&pageSize=1")
        rate = float(str(data[0]["closePrice"]).replace(",", ""))
        if rate > 0:
            return rate, "NAVER(종가)", None
    except Exception as exc:
        errors.append(f"  - NAVER(종가): {exc!r}")
        print(f"[소스 실패] NAVER(종가): {exc!r}")

    # 4순위: 해외 API (하루 1회 갱신)
    try:
        data = get_json("https://open.er-api.com/v6/latest/EUR")
        rate = float(data["rates"]["KRW"])
        if rate > 0:
            return rate, "er-api(일1회)", None
    except Exception as exc:
        errors.append(f"  - er-api(일1회): {exc!r}")
        print(f"[소스 실패] er-api(일1회): {exc!r}")

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
    rate, source, quoted = fetch_rate()
    now = datetime.now(KST)
    tail = f"{source} · {(quoted or now):%m/%d %H:%M} KST"

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
        print(f"[조용히 통과] {rate} (기준 {TARGET}, 소스 {source})")

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
