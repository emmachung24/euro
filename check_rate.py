"""
유로 환율 알림 봇

[가격 알림]
  1) 기준가 이하로 처음 내려왔을 때
  2) 기준가 아래에서 STEP원 이상 추가로 떨어질 때마다
  3) 기준가 위로 다시 올라갔을 때
  4) 매일 09/12/15/18/21시 정기 리포트
  5) 급락 감지 (지정 시간 안에 지정 폭 이상 하락)
  6) 최저가 경신 (기준가 위일 때만)

[안전장치]
  7) 모든 환율 소스 실패 시 알림
  8) 환율 소스가 바뀌었을 때 알림

[텔레그램 명령어]  ※ 본인 CHAT_ID에서 온 것만 처리
  /target 1635   기준가 변경
  /status        현재 설정 및 환율 확인
  /off  /on      알림 끄기 / 켜기

표준 라이브러리만 사용 (pip install 불필요)
"""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------- 초기값 (state.json이 비어 있을 때만 사용) ----------
INIT_TARGET = float(os.environ.get("TARGET_RATE", "1640"))
INIT_STEP = float(os.environ.get("STEP_WON", "1"))

# ---------- 고정 설정 ----------
PLUNGE_WON = float(os.environ.get("PLUNGE_WON", "3"))       # 급락 기준(원)
PLUNGE_MINUTES = int(os.environ.get("PLUNGE_MINUTES", "30"))  # 급락 관측 구간(분)
DIGEST_HOURS = {9, 12, 15, 18, 21}                          # 정기 리포트 시각(KST)
DIGEST_AFTER_MINUTE = 7                                     # 정각 혼잡 회피
TARGET_MIN, TARGET_MAX = 500.0, 5000.0                      # 오타 방지 범위

STATE_FILE = "state.json"
KST = timezone(timedelta(hours=9))
NAVER_BASE = "https://api.stock.naver.com/marketindex/exchange"
TG_BASE = "https://api.telegram.org/bot"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"]).strip()


# ---------- 공통 ----------
def get_json(url, data=None):
    headers = {"User-Agent": "Mozilla/5.0"}
    if data is not None:
        data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode("utf-8"))


def send_telegram(text):
    get_json(f"{TG_BASE}{BOT_TOKEN}/sendMessage",
             {"chat_id": CHAT_ID, "text": text})


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------- 환율 조회 ----------
def due_digest_slot(now):
    """지금 기준 '이미 지났어야 할' 가장 최근 리포트 슬롯 -> (날짜, 시)."""
    today = [
        h for h in sorted(DIGEST_HOURS)
        if h < now.hour or (h == now.hour and now.minute >= DIGEST_AFTER_MINUTE)
    ]
    if today:
        return now.date(), max(today)
    return (now - timedelta(days=1)).date(), max(DIGEST_HOURS)


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

    for name, code in (
        ("NAVER(신한 고시)", "FX_EURKRW_SHB"),
        ("NAVER(하나 고시)", "FX_EURKRW"),
    ):
        try:
            rate, quoted = parse_naver(get_json(f"{NAVER_BASE}/{code}"))
            if rate > 0:
                return rate, name, quoted
        except Exception as exc:
            errors.append(f"{name}: {exc!r}")
            print(f"[소스 실패] {name}: {exc!r}")

    try:
        data = get_json(f"{NAVER_BASE}/FX_EURKRW/prices?page=1&pageSize=1")
        rate = float(str(data[0]["closePrice"]).replace(",", ""))
        if rate > 0:
            return rate, "NAVER(종가)", None
    except Exception as exc:
        errors.append(f"NAVER(종가): {exc!r}")
        print(f"[소스 실패] NAVER(종가): {exc!r}")

    try:
        data = get_json("https://open.er-api.com/v6/latest/EUR")
        rate = float(data["rates"]["KRW"])
        if rate > 0:
            return rate, "er-api(일1회)", None
    except Exception as exc:
        errors.append(f"er-api(일1회): {exc!r}")
        print(f"[소스 실패] er-api(일1회): {exc!r}")

    raise RuntimeError(" / ".join(errors))


# ---------- 텔레그램 명령어 ----------
def fetch_commands(last_update_id):
    """내 CHAT_ID에서 온 명령어만 [(텍스트, ...)] 로 반환."""
    params = urllib.parse.urlencode(
        {"offset": last_update_id + 1, "timeout": 0, "limit": 50}
    )
    try:
        res = get_json(f"{TG_BASE}{BOT_TOKEN}/getUpdates?{params}")
    except Exception as exc:
        print(f"[명령 조회 실패] {exc!r}")
        return [], last_update_id

    if not res.get("ok"):
        return [], last_update_id

    texts = []
    newest = last_update_id
    for upd in res.get("result", []):
        newest = max(newest, int(upd.get("update_id", 0)))
        msg = upd.get("message") or upd.get("edited_message") or {}
        sender = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        if sender != CHAT_ID:          # 남이 보낸 건 조용히 무시
            print(f"[외부 메시지 무시] chat_id={sender}")
            continue
        texts.append(text)
    return texts, newest


def handle_commands(texts, state, rate, source, tail):
    """명령어 처리. 기준가가 바뀌면 True 반환."""
    target_changed = False

    for text in texts:
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/target":
            try:
                value = float(arg.replace(",", ""))
            except ValueError:
                send_telegram("사용법: /target 1635")
                continue
            if not (TARGET_MIN <= value <= TARGET_MAX):
                send_telegram(
                    f"{TARGET_MIN:,.0f}~{TARGET_MAX:,.0f}원 사이로 입력해주세요."
                )
                continue
            state["target"] = value
            target_changed = True
            send_telegram(f"✅ 기준가를 {value:,.2f}원으로 바꿨어요.")
            print(f"[명령] target -> {value}")

        elif cmd == "/status":
            low = state.get("low")
            low_line = "기록 없음"
            if low is not None:
                low_line = f"{low:,.2f}원"
                if state.get("low_at"):
                    low_line += f" ({state['low_at']})"
            send_telegram(
                f"📊 현재 설정\n\n"
                f"기준가  {state['target']:,.2f}원\n"
                f"간격    {state['step']:,.2f}원\n"
                f"알림    {'켜짐' if state.get('enabled', True) else '꺼짐'}\n\n"
                f"현재    {rate:,.2f}원\n"
                f"최저    {low_line}\n\n"
                f"{tail}"
            )
            print("[명령] status")

        elif cmd == "/off":
            state["enabled"] = False
            send_telegram("🔕 알림을 껐어요. /on 으로 다시 켤 수 있어요.")
            print("[명령] off")

        elif cmd == "/on":
            state["enabled"] = True
            send_telegram("🔔 알림을 켰어요.")
            print("[명령] on")

        elif cmd.startswith("/"):
            send_telegram("쓸 수 있는 명령어\n/target 1635\n/status\n/off\n/on")
            print(f"[명령] 알 수 없음: {cmd}")

    return target_changed


# ---------- 메인 ----------
def main():
    state = load_state()
    state.setdefault("target", INIT_TARGET)
    state.setdefault("step", INIT_STEP)
    state.setdefault("enabled", True)

    # --- 환율 조회 (실패 시 알림 후 종료) ---
    try:
        rate, source, quoted = fetch_rate()
    except Exception as exc:
        if not state.get("fail_notified"):
            try:
                send_telegram(
                    f"⚠️ 환율을 못 가져오고 있어요.\n\n"
                    f"{str(exc)[:300]}\n\n"
                    f"10분 뒤 다시 시도할게요."
                )
            except Exception as send_exc:
                print(f"[장애 알림 실패] {send_exc!r}")
            state["fail_notified"] = True
            save_state(state)
        raise

    now = datetime.now(KST)
    tail = f"{source} · {(quoted or now):%m/%d %H:%M} KST"

    # --- 명령어 먼저 처리 ---
    texts, newest_id = fetch_commands(int(state.get("last_update_id", 0)))
    state["last_update_id"] = newest_id
    if handle_commands(texts, state, rate, source, tail):
        state["below"] = False          # 기준가가 바뀌면 판정 초기화
        state["last_alert_price"] = None

    # --- 소스 복구 / 강등 알림 ---
    if state.get("fail_notified"):
        send_telegram(f"✅ 환율 조회가 복구됐어요.\n\n현재  {rate:,.2f}원\n{tail}")
        state["fail_notified"] = False
    elif state.get("last_source") and state["last_source"] != source:
        send_telegram(
            f"🔀 환율 소스가 바뀌었어요.\n\n"
            f"{state['last_source']} → {source}\n\n"
            f"현재  {rate:,.2f}원\n{tail}"
        )
    state["last_source"] = source

    target = float(state["target"])
    step = float(state["step"])
    enabled = bool(state.get("enabled", True))

    # --- 최근 관측치 (급락 감지용) ---
    cutoff = now.timestamp() - PLUNGE_MINUTES * 60
    recent = [p for p in state.get("recent", []) if p[0] >= cutoff]
    peak = max((p[1] for p in recent), default=None)
    recent.append([now.timestamp(), rate])
    state["recent"] = recent[-24:]

    was_below = bool(state.get("below", False))
    last_alert = state.get("last_alert_price")
    is_below = rate <= target

    if not enabled:
        print(f"[알림 꺼짐] {rate} (기준 {target}, 소스 {source})")
    else:
        # 1) 기준가 아래로 처음 진입
        if is_below and not was_below:
            send_telegram(
                f"하원아 환전하렴? 🐣\n\n"
                f"현재  {rate:,.2f}원\n"
                f"목표  {target:,.0f}원\n\n"
                f"{tail}"
            )
            last_alert = rate
            print(f"[진입 알림] {rate}")

        # 2) 기준가 아래에서 추가 하락
        elif is_below and was_below:
            if last_alert is None:
                last_alert = rate
                print(f"[기준점 설정] {rate}")
            elif rate <= last_alert - step:
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
            print(f"[조용히 통과] {rate} (기준 {target}, 소스 {source})")

        # 5) 급락 감지
        if peak is not None and peak - rate >= PLUNGE_WON:
            send_telegram(
                f"⚡ 유로 급락 중!\n\n"
                f"현재  {rate:,.2f}원\n"
                f"{PLUNGE_MINUTES}분 내  -{peak - rate:,.2f}원\n\n"
                f"{tail}"
            )
            state["recent"] = [[now.timestamp(), rate]]   # 중복 방지
            print(f"[급락 알림] {peak} -> {rate}")

        # 6) 최저가 경신 (기준가 위일 때만)
        low = state.get("low")
        if not is_below and (low is None or rate < low):
            notified = state.get("low_notified")
            if notified is None or rate <= notified - step:
                send_telegram(
                    f"🔻 최저 경신\n\n"
                    f"현재  {rate:,.2f}원\n"
                    f"기준  {target:,.2f}원\n\n"
                    f"{tail}"
                )
                state["low_notified"] = rate
                print(f"[최저 경신 알림] {rate}")

        # 4) 정기 리포트 (실행이 밀려도 지난 슬롯을 늦게라도 발송)
        slot_date, slot_hour = due_digest_slot(now)
        slot = f"{slot_date:%Y-%m-%d}-{slot_hour:02d}"
        if state.get("last_digest", "") != slot:
            send_telegram(
                f"🐥 {slot_date:%Y/%m/%d} {slot_hour:02d}:00\n\n"
                f"현재  {rate:,.2f}원\n"
                f"{tail}"
            )
            state["last_digest"] = slot
            print(f"[정기 리포트] {slot}")

    # --- 최저가 기록 (알림 여부와 무관하게 항상) ---
    low = state.get("low")
    if low is None or rate < low:
        state["low"] = rate
        state["low_at"] = f"{(quoted or now):%m/%d %H:%M}"

    state["below"] = is_below
    state["last_alert_price"] = last_alert
    save_state(state)


if __name__ == "__main__":
    main()
