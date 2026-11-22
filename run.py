# -*- coding: utf-8 -*-
"""نقطة التشغيل (GitHub Actions أو جهازك).

  python run.py            فحص عادي (كل 15 دقيقة أثناء الجلسة)
  python run.py --final    تشغيل ما بعد الإغلاق: إغلاق الصفقات، «فاتت»، ملخص اليوم
  python run.py --site     إعادة بناء الموقع فقط

المفاتيح من متغيرات البيئة فقط:
  ALPACA_API_KEY, ALPACA_API_SECRET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, SEC_USER_AGENT ("الاسم email@example.com")
لا يرسل أي أمر تداول: عميل Alpaca للقراءة فقط.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import intraday as I  # noqa: E402
from hu import site  # noqa: E402
from hu.engine import Engine, load_cfg  # noqa: E402
from hu.sec import SecClient  # noqa: E402
from hu.store import Store  # noqa: E402

DATA = os.environ.get("HEADSUP_DATA", os.path.join(BASE, "data"))
DOCS = os.path.join(BASE, "docs")


def scheduled_time(now: datetime, minutes) -> datetime:
    """أقرب موعد مجدول سابق (لحساب تأخر GitHub)."""
    cands = []
    for m in minutes:
        s = now.replace(minute=m, second=0, microsecond=0)
        cands.append(s if s <= now else s - timedelta(hours=1))
    return max(cands)


def main(argv) -> int:
    cfg = load_cfg(os.path.join(BASE, "config.json"))
    store = Store(os.path.join(DATA, "headsup.db"))
    if "--site" in argv:
        site.build(store, os.path.join(DOCS, "index.html"), cfg)
        store.close()
        return 0
    key, secret = os.environ.get("ALPACA_API_KEY", "").strip(), os.environ.get("ALPACA_API_SECRET", "").strip()
    if not key or not secret:
        print("❌ ALPACA_API_KEY / ALPACA_API_SECRET غير موجودة في متغيرات البيئة")
        return 2
    client = I.AlpacaClient(key, secret, I.Config())
    tg = I.TelegramNotifier(os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
    sec = SecClient(cache_dir=DATA)
    kind = "final" if "--final" in argv else "scan"
    now = datetime.now(timezone.utc)
    # التشغيل اليدوي ليس له موعد، فلا نحسب له تأخيراً
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    sched = None if manual else scheduled_time(now, [40] if kind == "final" else [2, 17, 32, 47])
    # فحص وجود الأسرار (الأسماء فقط — لا تُطبع القيم أبداً)
    names = ["ALPACA_API_KEY", "ALPACA_API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "SEC_USER_AGENT"]
    print("الأسرار:", " • ".join(f"{n} {'✅' if os.environ.get(n, '').strip() else '❌ فارغ/غير موجود'}" for n in names))
    ua = os.environ.get("SEC_USER_AGENT", "")
    if ua and "@" not in ua:
        print("⚠️ SEC_USER_AGENT يجب أن يحتوي بريداً (مثل: Rayan rayan@gmail.com)")
    eng = Engine(client, store, cfg, sec, tg)
    info = eng.run(kind, sched)
    site.build(store, os.path.join(DOCS, "index.html"), cfg)
    store.close()
    print({k: info.get(k) for k in ("kind", "market_open", "swept", "shortlist", "qualified", "alerts", "delay_min", "api_calls", "errors")})
    if not tg.ready:
        print("ℹ️ تيليجرام غير مضبوط — لن تُرسل تنبيهات")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
