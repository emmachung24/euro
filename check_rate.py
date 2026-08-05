"""
유로 환율 알림 봇 (v2)

v1 대비 변경점
  - 정기 리포트가 밀려도 슬롯을 유실하지 않음 (밀린 목록을 함께 표시)
  - 실시간이 아닌 소스(종가/er-api)로는 가격 판정을 하지 않음
  - 소스 표시 이름을 바꿔도 "소스 변경" 오탐이 나지 않음 (내부 key로 비교)
  - 조회 장애가 지속되면 6시간마다 다시 알림 (v1은 최초 1회 후 영구 침묵)
  - 고시시각 파싱 시 타임존 누락 방어

[가격 알림]
  1) 기준가 이하로 처음 내려왔을 때
  2) 기준가 아래에서 STEP원 이상 추가로 떨어질 때마다
  3) 기준가 위로 다시 올라갔을 때
  4) 매일 09/12/15/18/21시 정기 리포트
  5) 급락 감지 (지정 시간 안에 지정 폭 이상 하락)
  6) 최저가 경신 (기준가 위일 때만)

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
from datetime import datetime, time, timedelta, timezone

# ---------- 초기값 (state.json이 비어 있을 때만 사용) ----------
INIT_TARGET = float(os.environ.get("TARGET_RATE", "1640"))
INIT_STEP = float(os.environ.get("STEP_WON", "1"))

# ---------- 고정 설정 ----------
PLUNGE_WON = float(os.environ.get("PLUNGE_WON", "3"))         # 급락 기준(원)
PLUNGE_MINUTES = int(os.environ.get("PLUNGE_MINUTES", "30"))  # 급락 관측 구간(분)
DIGEST_HOURS = {9, 12, 15, 18, 21}                            # 정기 리포트 시각(KST)
DIGEST_AFTER_MINUTE = 7                                       # 정각 혼잡 회피
FAIL_RENOTIFY_HOURS = 6                                       # 장애 재알림 간격
TARGET_MIN, TARGET_MAX = 500.0, 5000.0                        # 오타 방지 범위

STATE_FILE = "state.json"
KST = timezone(timedelta(hours=9))
NAVER_BASE = "https://api.stock.naver.com/marketindex/exchange"
TG_BASE = "https://api.telegram.org/bot"

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = str(os.environ["CHAT_ID"]).strip()

# 내부 key -> (표시 이름, 실시간 여부)
# 표시 이름은 마음대로 바꿔도 되지만, key는 절대 바꾸지 말 것
SOURCES = {
    "naver_shb":   ("NAVER 신한 고시", True),
    "naver_hana":  ("NAVER 하나 고시", True),
    "naver_close": ("NAVER 종가",      False),
    "er_api":      ("er-api",          False),
}


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
def parse_naver(data):
    """NAVER 고시환율 응답에서 (환율, 고시시각) 추출."""
    info = data.get("exchangeInfo", data)
    raw = info.get("calcPrice") or info.get("closePrice")
    rate = float(str(raw).replace(",", ""))

    quoted = None
    stamp = info.get("localTradedAt")
    if stamp:
        try:
            parsed = datetime.fromisoformat(stamp)
            # 오프셋이 없으면 KST로 간주 (러너가 UTC라 astimezone만 쓰면 9시간 밀림)
            if parsed.tzinfo is None:
                quoted = parsed.replace(tzinfo=KST)
            else:
                quoted = parsed.astimezone(KST)
        except Exception:
            quoted = None

    return rate, quoted


def fetch_rate():
    """(환율, 소스 key, 고시시각) 반환. 앞의 소스가 실패하면 다음으로 넘어간다."""
    errors = []

    for key, code in (("naver_shb", "FX_EURKRW_SHB"), ("naver_hana", "FX_EURKRW")):
        try:
            rate, quoted = parse_naver(get_json(f"{NAVER_BASE}/{code}"))
            if rate > 0:
                return rate, key, quoted
        except Exception as exc:
            errors.append(f"{SOURCES[key][0]}: {exc!r}")
            print(f"[소스 실패] {SOURCES[key][0]}: {exc!r}")

    try:
        data = get_json(f"{NAVER_BASE}/FX_EURKRW/prices?page=1&pageSize=1")
        rate = float(str(data[0]["closePrice"]).replace(",", ""))
        if rate > 0:
            return rate, "naver_close", None
    except Exception as exc:
        errors.append(f"NAVER 종가: {exc!r}")
        print(f"[소스 실패] NAVER 종가: {exc!r}")

    try:
        data = get_json("https://open.er-api.com/v6/latest/EUR")
        rate = float(data["rates"]["KRW"])
        if rate > 0:
            return rate, "er_api", None
    except Exception as exc:
        errors.append(f"er-api: {exc!r}")
        print(f"[소스 실패] er-api: {exc!r}")

    raise RuntimeError(" / ".join(errors))


# ---------- 정기 리포트 ----------
def digest_slots(now, last_slot):
    """지금까지 발송됐어야 할 슬롯을 오래된 순으로 반환. 형식: 'YYYY-MM-DD-HH'."""
    slots = []
    for day_offset in (1, 0):                    # 어제, 오늘
        day = (now - timedelta(days=day_offset)).date()
        for hour in sorted(DIGEST_HOURS):
            due = datetime.combine(day, time(hour, DIGEST_AFTER_MINUTE), tzinfo=KST)
            if due <= now:
                slots.append((f"{day:%Y-%m-%d}-{hour:02d}", day, hour))

    if last_slot:
        # 문자열이 제로패딩된 ISO 형식이라 사전순 비교가 시간순과 일치
        return [s for s in slots if s[0] > last_slot]
    return slots[-1:]                            # 최초 실행이면 스팸 방지로 1개만


def send_digest(state, now, rate, tail):
    slots = digest_slots(now, state.get("last_digest"))
    if not slots:
        return

    latest_id, day, hour = slots[-1]
    header = f"🐥{day:%Y/%m/%d} {hour:02d}:00"
    if len(slots) > 1:
        missed = ", ".join(f"{h:02d}:00" for _, _, h in slots[:-1])
        header += f"\n(밀린 슬롯: {missed})"

    send_telegram(f"{header}\n\n현재  {rate:,.2f}원\n{tail}")
    state["last_digest"] = latest_id
    print(f"[정기 리포트] {latest_id} (밀린 슬롯 {len(slots) - 1}개)")


# ---------- 장애 처리 ----------
def notify_failure(state, now, exc):
    """조회 실패 시. 최초 1회 + 이후 FAIL_RENOTIFY_HOURS 마다 재알림."""
    ts = now.timestamp()
    if state.get("fail_since") is None:
        state["fail_since"] = ts

    elapsed = ts - float(state.get("fail_notified_at", 0))
    if elapsed < FAIL_RENOTIFY_HOURS * 3600:
        print("[장애 지속] 재알림 대기 중")
        return

    hours = (ts - float(state["fail_since"])) / 3600
    note = f"\n{hours:.1f}시간째 실패 중이에요.\n" if hours >= 1 else ""
    try:
        send_telegram(f"⚠️ 환율을 못 가져오고 있어요.\n{note}\n{str(exc)[:300]}")
        state["fail_notified_at"] = ts
    except Exception as send_exc:
        print(f"[장애 알림 실패] {send_exc!r}")


def notify_recovery(state, now, rate, tail):
    if state.get("fail_since") is None:
        state.pop("fail_notified", None)         # v1 잔재 정리
        return
    hours = (now.timestamp() - float(state["fail_since"])) / 3600
    send_telegram(
        f"✅ 환율 조회가 복구됐어요.\n\n"
        f"{hours:.1f}시간 중단\n현재  {rate:,.2f}원\n{tail}"
    )
    state.pop("fail_since", None)
    state.pop("fail_notified_at", None)
    state.pop("fail_notified", None)


# ---------- 텔레그램 명령어 ----------
def fetch_commands(last_update_id):
    """내 CHAT_ID에서 온 명령어만 반환."""
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
        if sender != CHAT_ID:
            print(f"[외부 메시지 무시] chat_id={sender}")
            continue
        texts.append(text)
    return texts, newest


def handle_commands(texts, state, rate, tail):
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
    now = datetime.now(KST)

    # --- 환율 조회 ---
    try:
        rate, source_key, quoted = fetch_rate()
    except Exception as exc:
        notify_failure(state, now, exc)
        save_state(state)
        raise

    label, live = SOURCES[source_key]
    tail = f"{label} · {(quoted or now):%m/%d %H:%M} KST"
    if not live:
        tail += "\n⚠️ 실시간 시세가 아니에요"

    # --- 명령어 먼저 처리 ---
    texts, newest_id = fetch_commands(int(state.get("last_update_id", 0)))
    state["last_update_id"] = newest_id
    if handle_commands(texts, state, rate, tail):
        state["below"] = False              # 기준가가 바뀌면 판정 초기화
        state["last_alert_price"] = None

    # --- 장애 복구 알림 ---
    notify_recovery(state, now, rate, tail)

    # --- 소스 변경 알림 (표시 이름이 아닌 내부 key로 비교) ---
    prev_key = state.get("last_source_key")
    if prev_key is None:
        print(f"[소스 최초 기록] {source_key}")     # v1 state 마이그레이션: 조용히 통과
    elif prev_key != source_key:
        send_telegram(
            f"🔀 환율 소스가 바뀌었어요.\n\n"
            f"{SOURCES[prev_key][0]} → {label}\n\n"
            f"현재  {rate:,.2f}원\n{tail}"
        )
        print(f"[소스 변경] {prev_key} -> {source_key}")
    state["last_source_key"] = source_key
    state.pop("last_source", None)                  # v1 잔재 정리

    target = float(state["target"])
    step = float(state["step"])
    enabled = bool(state.get("enabled", True))

    # --- 정기 리포트 (실시간 여부와 무관하게 발송) ---
    if enabled:
        send_digest(state, now, rate, tail)

    # --- 여기부터는 실시간 소스일 때만 ---
    if not live:
        print(f"[비실시간 소스] {source_key} — 가격 판정 건너뜀 ({rate})")
        save_state(state)
        return

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
        print(f"[알림 꺼짐] {rate} (기준 {target}, 소스 {source_key})")
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
            print(f"[조용히 통과] {rate} (기준 {target}, 소스 {source_key})")

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
