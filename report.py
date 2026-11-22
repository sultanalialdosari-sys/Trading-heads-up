# -*- coding: utf-8 -*-
"""تقرير التجربة (أسبوع أو أي فترة): python report.py [من YYYY-MM-DD] [إلى YYYY-MM-DD]
يكتب docs/report.md — أرقام فقط من قاعدة البيانات، بلا تجميل."""
import os
import sys
from collections import Counter
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import intraday as I  # noqa: E402
from hu import trades as TR  # noqa: E402
from hu.store import Store  # noqa: E402


def pct(a, b):
    return f"{a / b:.0%}" if b else "—"


def build(store: Store, d1: str, d2: str) -> str:
    W = "day BETWEEN ? AND ?"
    al = store.q(f"SELECT * FROM alerts WHERE {W}", (d1, d2))
    tr = store.q(f"SELECT * FROM trades WHERE {W}", (d1, d2))
    ms = store.q(f"SELECT * FROM missed WHERE {W}", (d1, d2))
    runs = store.q("SELECT * FROM runs WHERE substr(started_utc,1,10) BETWEEN ? AND ?", (d1, d2))
    days = sorted({r["started_utc"][:10] for r in runs})
    n = len(al)
    act = [a for a in al if a["state"] == "ACTIVATED"]
    inv = [a for a in al if a["state"] == "INVALIDATED"]
    exp = [a for a in al if a["state"] == "EXPIRED"]
    late = [a for a in al if (a["gain_from_open"] or 0) >= 15]
    leads = sorted(a["lead_min"] for a in act if a["lead_min"] is not None)
    sA, sB, sBase = (TR.summary([t for t in tr if t["kind"] == k]) for k in ("A", "B", "BASE"))
    scans = [r for r in runs if r["kind"] == "scan" and r["market_open"]]
    delays = sorted(r["delay_min"] for r in scans if r["delay_min"] is not None)
    expected = len({r["started_utc"][:10] for r in scans}) * 26  # كل 15 دقيقة: 9:32 … 15:47 نيويورك
    errs = [r for r in runs if r["errors"]]
    cats = Counter("تحرك بين فحصين" if "بين فحصين" in m["reason"] else
                   "كان مرشحاً لكن لم يستوفِ القواعد" if m["was_candidate"] else
                   "حجم/سيولة غير كافية قبل الحركة" if "لم يدخل" in m["reason"] else "أخرى" for m in ms)

    def srow(name, s):
        if not s.get("trades"):
            return f"| {name} | 0 | — | — | — | — | — |"
        return (f"| {name} | {s['trades']} | {s['win_rate']:.0%} | {s['avg_win']:+.2f}$ / {s['avg_loss']:+.2f}$ | "
                f"{s['avg_pct']:+.2f}% | {s['total_pnl']:+,.2f}$ | {s['max_drawdown']:,.2f}$ |")

    L = [f"# تقرير التنبيه المبكر — من {d1} إلى {d2}", "",
         f"أيام تداول فيها تشغيلات: {len(days)} • تشغيلات فحص: {len(scans)} من حوالي {expected} متوقعة "
         f"({pct(len(scans), expected)}) • تشغيلات فيها أخطاء: {len(errs)}", "",
         "## التنبيهات",
         f"- عدد التنبيهات المبكرة: **{n}**",
         f"- تفعّلت (اخترقت مستوى الدخول): **{len(act)}** ({pct(len(act), n)})",
         f"- أُلغيت قبل الاختراق (إنذار كاذب): **{len(inv)}** ({pct(len(inv), n)})",
         f"- انتهى اليوم بلا اختراق ولا إبطال: **{len(exp)}** ({pct(len(exp), n)})",
         f"- وصلت متأخرة (السهم كان مرتفعاً 15%+ عن إغلاق أمس وقت التنبيه): **{len(late)}** ({pct(len(late), n)})",
         ]
    if leads:
        L.append(f"- السبق قبل الاختراق: الوسيط **{leads[len(leads) // 2]:.0f} دقيقة**، الأقل {leads[0]:.0f}، الأكثر {leads[-1]:.0f}")
    L += ["", "## الصفقات الافتراضية",
          "| الطريقة | صفقات مغلقة | نسبة الربح | متوسط ربح / خسارة | متوسط الصفقة | الصافي | أكبر تراجع |",
          "|---|---|---|---|---|---|---|",
          srow("A: دخول عند التنبيه", sA), srow("B: دخول عند الاختراق", sB), srow("المقارنة: أي قفزة حجم", sBase), ""]
    bA, bBase = sA.get("avg_pct"), sBase.get("avg_pct")
    if bA is not None and bBase is not None and sA.get("trades") and sBase.get("trades"):
        verdict = "أفضل من" if bA > bBase else "ليست أفضل من"
        L.append(f"**الخلاصة:** صفقات التنبيه (A) متوسطها {bA:+.2f}% مقابل {bBase:+.2f}% للمقارنة — {verdict} مجرد شراء أي سهم قفز حجمه.")
        if min(sA["trades"], sBase["trades"]) < 30:
            L.append("⚠️ العينة أقل من 30 صفقة — لا تكفي لحكم نهائي.")
    L += ["", "## ما فات",
          f"أسهم تحت 5$ ارتفعت 30%+ بلا تنبيه: **{len(ms)}**"] + [f"- {k}: {v}" for k, v in cats.most_common()] + [
          "", "## تأخر التشغيل على GitHub"]
    if delays:
        L.append(f"- الوسيط {delays[len(delays) // 2]:.0f} دقيقة • الأكثر {delays[-1]:.0f} دقيقة • "
                 f"تأخر أكثر من 15 دقيقة: {sum(1 for x in delays if x > 15)} مرة")
    else:
        L.append("- لا بيانات")
    L += ["", "*التنبيهات ليست توصية شراء. الصفقات افتراضية. البيانات: السوق الكامل متأخر 15 دقيقة، والسعر اللحظي من IEX فقط.*"]
    return "\n".join(L)


if __name__ == "__main__":
    st = Store(os.path.join(os.environ.get("HEADSUP_DATA", os.path.join(BASE, "data")), "headsup.db"))
    rng = st.one("SELECT MIN(day) AS a, MAX(day) AS b FROM alerts") or {}
    d1 = sys.argv[1] if len(sys.argv) > 1 else (rng.get("a") or datetime.now(I.ET).strftime("%Y-%m-%d"))
    d2 = sys.argv[2] if len(sys.argv) > 2 else (rng.get("b") or d1)
    txt = build(st, d1, d2)
    os.makedirs(os.path.join(BASE, "docs"), exist_ok=True)
    open(os.path.join(BASE, "docs", "report.md"), "w", encoding="utf-8").write(txt)
    print(txt)
