#!/usr/bin/env python3
"""日光 空房監控：定期檢查官網 / 樂天 / じゃらん，有空房時發 Telegram 通知。

用法：
  python3 hotel_watch.py                 # 檢查一次（launchd 每幾分鐘呼叫一次）
  python3 hotel_watch.py --dry-run       # 只印結果，不發通知、不寫狀態
  python3 hotel_watch.py --date 2026-10-06 --dry-run   # 用別的日期測試偵測是否正常
  ./setup_telegram.sh                    # 設定 Telegram（token 存進 GitHub Secrets）
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
STATE = HERE / "state.json"
LOG = HERE / "watch.log"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# 花庵官網訂房系統（e-concierge）前端網頁內嵌的公開 key，與一般瀏覽器訪客相同
ECON_KEY = "e0rJYeYMwc7idj8yRB4R89PevcveKfPJ4COU8z5C"
ECON_API = "https://api.e-concierge.net/v1"

HOTELS = {
    "hanaan": {
        "name": "ホテル花庵",
        "econcierge": 20016,
        "rakuten": 54978,
        "jalan": 318487,
    },
    "nagomi": {
        "name": "旅籠なごみ",
        "jalan": 304988,
    },
}


# ---------------------------------------------------------------- utilities
def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(url, headers=None, encoding="utf-8", timeout=30):
    h = {"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode(encoding, "ignore")


def page_text(raw):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    return re.sub(r"\n\s*\n+", "\n", t)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- checkers
# 每個 checker 回傳 dict：{"available": bool, "detail": str, "url": str}
# 無法判斷時 raise，交給主程式記為錯誤。

def check_rakuten(hotel_id, night, adults):
    nxt = night + dt.timedelta(days=1)
    q = dict(f_nen1=night.year, f_tuki1=night.month, f_hi1=night.day,
             f_nen2=nxt.year, f_tuki2=nxt.month, f_hi2=nxt.day,
             f_otona_su=adults, f_heya_su=1)
    url = f"https://hotel.travel.rakuten.co.jp/hotelinfo/plan/{hotel_id}?" + urllib.parse.urlencode(q)
    t = page_text(fetch(url))
    if "ご指定の条件での空室が見つかりませんでした" in t:
        return {"available": False, "detail": "滿房", "url": url}
    rooms = [int(x) for x in re.findall(r"残り(\d+)部屋", t)]
    prices = [int(x.replace(",", "")) for x in re.findall(r"合計\n([\d,]+)\n円", t)]
    if not prices and not rooms:
        raise RuntimeError("樂天頁面結構無法辨識（可能被擋或改版）")
    detail = f"{len(prices)} 個方案有房"
    if prices:
        detail += f"，最低 ¥{min(prices):,}（2人合計）"
    if rooms:
        detail += f"，最少剩 {min(rooms)} 間"
    return {"available": True, "detail": detail, "url": url}


def check_jalan(yad_id, night, adults):
    q = dict(stayYear=night.year, stayMonth=night.month, stayDay=night.day,
             stayCount=1, roomCount=1, adultNum=adults)
    url = f"https://www.jalan.net/yad{yad_id}/plan/?" + urllib.parse.urlencode(q)
    t = page_text(fetch(url, encoding="cp932"))
    if "ご利用できるプランがない" in t:
        return {"available": False, "detail": "滿房", "url": url}
    m = re.search(r"(\d+)\n?件の宿泊プランがありました", t)
    if not m:
        raise RuntimeError("じゃらん頁面結構無法辨識（可能被擋或改版）")
    detail = f"{m.group(1)} 個方案有房"
    prices = [int(x.replace(",", "")) for x in re.findall(r"\n([\d,]{5,})\n円\n", t)]
    if prices:
        detail += f"，最低 ¥{min(prices):,}（2人合計）"
    if "空室わずか" in t:
        detail += "，空室わずか"
    return {"available": True, "detail": detail, "url": url}


def check_econcierge(hotel_id, night, adults):
    hdr = {"x-api-key": ECON_KEY, "Origin": "https://app.e-concierge.net"}
    d = night.isoformat()
    nxt = (night + dt.timedelta(days=1)).isoformat()
    rts = json.loads(fetch(
        f"{ECON_API}/hotels/{hotel_id}/room-types?limit=100&fields=plans"
        f"&arrival_date={d}&departure_date={nxt}&one_day_trip=false"
        f"&guests_count={adults}&min_rate=1&max_rate=1000000", hdr))["room_types"]
    if not rts:
        raise RuntimeError("官網 API 沒回傳任何房型")
    found = []
    for rt in rts:
        rid = rt["room_type"]["room_type_id"]
        name = next((l["name"] for l in rt["room_type"].get("localizations", [])
                     if l.get("lang") == "ja"), str(rid))
        for p in rt["plans"]:
            inv = json.loads(fetch(
                f"{ECON_API}/hotels/{hotel_id}/plans/{p['plan_id']}/room-type-inventories/{rid}"
                f"?min_calendar_date={d}&max_calendar_date={nxt}&guests_count={adults}", hdr))
            cal = [c for c in inv["inventory"]["calendars"] if c["date"] == d]
            if not cal or cal[0]["status"] != "available":
                continue  # 此方案當天不販售，換下一個方案
            c = cal[0]
            if c["inventory_status"] == "in-stock" and (c["quantity"] or 0) > 0:
                found.append((name, c["quantity"], c["total_amount"]))
            # 房數是房型共用的，有一個販售中的方案就能判斷此房型
            break
    url = f"https://app.e-concierge.net/v3a/hotels/{hotel_id}"
    if not found:
        return {"available": False, "detail": "滿房", "url": url}
    parts = [f"{n}（剩{q}間" + (f"，¥{a:,}起" if a else "") + "）" for n, q, a in found]
    return {"available": True, "detail": "；".join(parts), "url": url}


SOURCES = [
    ("hanaan", "官網", lambda h, n, a: check_econcierge(h["econcierge"], n, a)),
    ("hanaan", "樂天", lambda h, n, a: check_rakuten(h["rakuten"], n, a)),
    ("hanaan", "じゃらん", lambda h, n, a: check_jalan(h["jalan"], n, a)),
    ("nagomi", "じゃらん", lambda h, n, a: check_jalan(h["jalan"], n, a)),
]


# ---------------------------------------------------------------- telegram
def telegram(cfg, text):
    # 雲端執行時從 GitHub Secrets（環境變數）讀取
    tok = os.environ.get("TELEGRAM_TOKEN") or cfg.get("telegram_token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("telegram_chat_id")
    if not tok or not chat:
        log("⚠️ 尚未設定 Telegram，略過通知")
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", body, timeout=20)




# ---------------------------------------------------------------- main
def run(args, cfg):
    adults = cfg.get("adults", 2)
    target = dt.date.fromisoformat(args.date or cfg["target_night"])
    also = [dt.date.fromisoformat(x) for x in cfg.get("also_check_nights", [])] if not args.date else []
    nights = [target] + also

    results = {}  # key -> result
    errors = []
    for night in nights:
        for hkey, src, fn in SOURCES:
            key = f"{night}|{hkey}|{src}"
            try:
                results[key] = fn(HOTELS[hkey], night, adults)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{HOTELS[hkey]['name']} {src} {night:%m/%d}: {e}")
                log("ERROR " + key + " " + "".join(traceback.format_exception_only(e)).strip())
            time.sleep(1.5)

    for key, r in results.items():
        log(f"{key}: {'🟢 有房' if r['available'] else '⚪ 滿房'} {r['detail'] if r['available'] else ''}")

    if args.dry_run:
        return

    state = load_json(STATE, {"available": {}, "error_streak": 0, "last_heartbeat": ""})
    prev = state["available"]
    newly = [k for k, r in results.items()
             if r["available"] and not prev.get(k) and k.startswith(str(target))]
    gone = [k for k, was in prev.items()
            if was and k in results and not results[k]["available"] and k.startswith(str(target))]

    if newly:
        lines = [f"🚨 {target:%m/%d} 有空房了！快去訂！", ""]
        for k in newly:
            _, hkey, src = k.split("|")
            r = results[k]
            lines += [f"🏨 {HOTELS[hkey]['name']}｜{src}", f"   {r['detail']}", f"   {r['url']}", ""]
        # 附上同一間飯店隔晚的狀態，方便判斷能不能連住兩晚
        for n in also:
            for hkey in {k.split("|")[1] for k in newly}:
                avail = [src for (h, src, _) in SOURCES if h == hkey
                         and results.get(f"{n}|{hkey}|{src}", {}).get("available")]
                lines.append(f"ℹ️ {HOTELS[hkey]['name']} {n:%m/%d}: "
                             + (f"也有房（{'、'.join(avail)}）→ 可連住兩晚" if avail else "滿房"))
        telegram(cfg, "\n".join(lines).strip())
        log("已通知: " + ", ".join(newly))
    if gone:
        telegram(cfg, "⚠️ 剛剛的空房已被訂走：\n" + "\n".join(
            f"{HOTELS[k.split('|')[1]]['name']}｜{k.split('|')[2]}" for k in gone))

    # 連續錯誤過多時提醒（網站擋爬或改版）
    state["error_streak"] = state["error_streak"] + 1 if errors else 0
    if state["error_streak"] in (6, 60):
        telegram(cfg, "⚠️ 監控連續出錯，可能需要檢查：\n" + "\n".join(errors[:5]))

    # 每天早上 9 點後發一次心跳，確認程式還活著
    now = dt.datetime.now()
    if now.hour >= 9 and state["last_heartbeat"] != str(now.date()):
        summary = []
        for n in nights:
            for hkey, src, _ in SOURCES:
                r = results.get(f"{n}|{hkey}|{src}")
                mark = "❓" if r is None else ("🟢" if r["available"] else "⚪")
                summary.append(f"{mark} {n:%m/%d} {HOTELS[hkey]['name']}｜{src}")
        telegram(cfg, "☀️ 監控仍在運作中，目前狀態：\n" + "\n".join(summary))
        state["last_heartbeat"] = str(now.date())

    state["available"] = {k: r["available"] for k, r in results.items()}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="改查別的日期（測試用），格式 YYYY-MM-DD")
    args = ap.parse_args()
    cfg = load_json(CONFIG, {})
    if args.date and not args.dry_run:
        sys.exit("--date 只能搭配 --dry-run 使用")
    if not args.date and dt.date.today() > dt.date.fromisoformat(cfg["target_night"]):
        return log("目標日期已過，停止檢查")
    run(args, cfg)


if __name__ == "__main__":
    main()
