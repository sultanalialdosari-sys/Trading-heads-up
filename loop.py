# -*- coding: utf-8 -*-
"""المشغّل المستمر على GitHub Actions — يحل مشكلة أن GitHub يُسقط كثيراً من التشغيلات المجدولة.

تشغيل واحد يبقى يعمل حتى 5 ساعات و40 دقيقة، ويفحص في المواعيد بنفسه (بتوقيت نيويورك):
  قبل الافتتاح  7:00 → الافتتاح        كل 15 دقيقة (الدقائق 02 و17 و32 و47)
  الجلسة        الافتتاح → الإغلاق     كل 5 دقائق (02، 07، 12 …)
  إغلاق اليوم   الإغلاق + 20 دقيقة      مرة واحدة: إغلاق الصفقات، «فاتت»، الملخص، التقرير
  بعد الإغلاق   الإغلاق → 20:00        كل 15 دقيقة
عدة تشغيلات مجدولة في اليوم تتسلّم من بعضها (concurrency)، فإذا أُسقط واحد يغطيه التالي.
بعد كل فحص تُحفظ قاعدة البيانات في فرع data، والموقع يُنشر كل 10 دقائق تقريباً أو فور وصول تنبيه.
"""
import os
import sys
import time
import traceback
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import intraday as I  # noqa: E402
import report  # noqa: E402
from hu import gitdata, site  # noqa: E402
from hu.engine import Engine, load_cfg  # noqa: E402
from hu.sec import SecClient  # noqa: E402
from hu.store import Store  # noqa: E402

ET = I.ET
DATA = os.environ.get("HEADSUP_DATA", os.path.join(BASE, "data"))
DB = os.path.join(DATA, "headsup.db")
REPORT = os.path.join(DATA, "report.md")
FILES = {"headsup.db": DB, "report.md": REPORT}
MAX_SEC = int(os.environ.get("LOOP_MAX_MIN", "340")) * 60
PUBLISH_EVERY = 600
PRE_START = (7, 0)
POST_END = (20, 0)


def hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def phase_of(et: datetime, open_hm, close_hm) -> str:
    t = (et.hour, et.minute)
    if t < PRE_START:
        return "EARLY"
    if t < open_hm:
        return "PRE"
    if t < close_hm:
        return "REG"
    if t < POST_END:
        return "POST"
    return "DONE"


def next_slot(et: datetime, phase: str) -> datetime:
    """الموعد التالي (بعد الآن) حسب الفترة: كل 5 دقائق في الجلسة، وكل 15 خارجها."""
    step = 5 if phase == "REG" else 15
    base = et.replace(second=0, microsecond=0)
    for k in range(1, 61):
        t = base + timedelta(minutes=k)
        if t.minute % step == 2:
            return t
    return base + timedelta(minutes=step)


def main() -> int:
    t_start = time.monotonic()
    cfg = load_cfg(os.path.join(BASE, "config.json"))
    restored = gitdata.restore(BASE, FILES)
    print("قاعدة البيانات:", "من فرع data" if restored else "من المستودع (أول مرة)")
    key, secret = os.environ.get("ALPACA_API_KEY", "").strip(), os.environ.get("ALPACA_API_SECRET", "").strip()
    if not key or not secret:
        print("❌ مفاتيح Alpaca غير موجودة")
        return 2
    client = I.AlpacaClient(key, secret, I.Config())
    tg = I.TelegramNotifier(os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
    sec = SecClient(cache_dir=DATA)
    names = ["ALPACA_API_KEY", "ALPACA_API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "SEC_USER_AGENT"]
    print("الأسرار:", " • ".join(f"{n} {'✅' if os.environ.get(n, '').strip() else '❌'}" for n in names))

    store = Store(DB)
    last_pub, dirty, final_done = -1e9, False, False
    sweep_cache: dict = {}
    session = None

    def publish(force=False):
        nonlocal last_pub, dirty
        if not dirty and not force:
            return
        store.commit()
        try:
            gitdata.publish(BASE, FILES)
            ok = gitdata.dispatch_site(BASE)
            print(f"نُشر ({'والموقع' if ok else 'بدون الموقع'}) {datetime.now(ET):%H:%M} ET")
            last_pub, dirty = time.monotonic(), False
        except Exception as e:
            print("⚠️ فشل النشر:", e)

    while True:
        left = MAX_SEC - (time.monotonic() - t_start)
        if left < 120:
            print("انتهت مدة هذا التشغيل — يتسلّم التشغيل التالي")
            break
        try:
            client.clock()
            now = client.now_utc()
            et = now.astimezone(ET)
            if session is None or session[0] != et.date():
                ok, o, c = I.todays_session(client)
                session = (et.date(), ok, hm(o), hm(c))
            _, trading, open_hm, close_hm = session
            if not trading:
                print("ليس يوم تداول — خروج")
                break
            ph = phase_of(et, open_hm, close_hm)
            if ph == "DONE":
                if not final_done and not store.one("SELECT key FROM sent WHERE key=?", (f"daily:{et:%Y-%m-%d}",)):
                    Engine(client, store, cfg, sec, tg).run("final", now)
                    final_done, dirty = True, True
                print("انتهى اليوم — خروج")
                break
            # إغلاق اليوم بعد الإغلاق بـ20 دقيقة (بيانات السوق الكامل للإغلاق متاحة)
            close_dt = et.replace(hour=close_hm[0], minute=close_hm[1], second=0, microsecond=0)
            if ph == "POST" and not final_done and et >= close_dt + timedelta(minutes=20):
                if not store.one("SELECT key FROM sent WHERE key=?", (f"daily:{et:%Y-%m-%d}",)):
                    info = Engine(client, store, cfg, sec, tg).run("final", now)
                    print("إغلاق اليوم:", {k: info.get(k) for k in ("alerts", "errors")})
                    with open(REPORT, "w", encoding="utf-8") as f:
                        f.write(report.build(store, (et - timedelta(days=6)).strftime("%Y-%m-%d"), et.strftime("%Y-%m-%d")))
                final_done, dirty = True, True
                publish(force=True)
            if ph in ("PRE", "REG", "POST"):
                kind = {"PRE": "pre", "REG": "scan", "POST": "post"}[ph]
                slot = et.replace(second=0, microsecond=0)
                info = Engine(client, store, cfg, sec, tg).run(kind, slot.astimezone(I.UTC), sweep_cache=sweep_cache)
                dirty = True
                print(f"{et:%H:%M} ET {kind}: مرشحون {info.get('shortlist')} • مؤهلون {info.get('qualified')} • "
                      f"تنبيهات {info.get('alerts')} • طلبات {info.get('api_calls')} • {info.get('duration_sec')} ث"
                      + (f" • أخطاء: {info['errors'][:150]}" if info.get("errors") else ""))
                if info.get("alerts") or time.monotonic() - last_pub >= PUBLISH_EVERY:
                    publish()
            # النوم حتى الموعد التالي
            now = client.now_utc()
            et = now.astimezone(ET)
            ph = phase_of(et, open_hm, close_hm)
            if ph == "EARLY":
                target = et.replace(hour=PRE_START[0], minute=2, second=0, microsecond=0)
            else:
                target = next_slot(et, "REG" if ph == "REG" else "X")
            wait = (target - et).total_seconds()
            left = MAX_SEC - (time.monotonic() - t_start)
            if wait > left - 60:
                print("الموعد التالي بعد نهاية هذا التشغيل — خروج")
                break
            time.sleep(max(5, wait + 3))
        except KeyboardInterrupt:
            break
        except Exception as e:      # لا يتوقف المشغّل بسبب خطأ واحد
            print("⚠️ خطأ في الدورة:", type(e).__name__, str(e)[:200])
            traceback.print_exc(limit=3)
            time.sleep(60)
    publish(force=dirty)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
