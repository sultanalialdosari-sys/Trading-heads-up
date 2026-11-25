# -*- coding: utf-8 -*-
"""إعادة صفقات النظام الحي (A) عبر المحاكاة الواقعية، لمقارنة النتيجة الحية بالمحاكاة.

  python -m bt.replay_live --db data/headsup.db

يستخدم شموع 5 دقائق من جدول chart_bars (SIP). التسعير من الشموع قبل التنبيه فقط، والدخول على افتتاح
الشمعة التالية. يطبع لكل صفقة: R الحي، R المحاكاة بثلاثة سيناريوهات، وسبب الخروج.
"""
import argparse
import json
import os
import sys
from datetime import datetime

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from bt.costs import CostModel  # noqa: E402
from bt.execution import Order, simulate, time_exit_for  # noqa: E402
from hu import config as C  # noqa: E402
from hu.store import Store  # noqa: E402
from pit.models import ET  # noqa: E402

SCEN = ("optimistic", "base", "pessimistic")


def frame(rows) -> pd.DataFrame:
    idx = pd.to_datetime([r[0] for r in rows], unit="s", utc=True)
    return pd.DataFrame([r[1:6] for r in rows], index=idx, columns=["open", "high", "low", "close", "volume"])


def replay(db: Store, app) -> pd.DataFrame:
    model = CostModel(app.costs)
    out = []
    for t in db.q("SELECT * FROM trades WHERE kind='A' AND pnl IS NOT NULL ORDER BY day, id"):
        r = db.one("SELECT bars FROM chart_bars WHERE day=? AND symbol=?", (t["day"], t["symbol"]))
        if not r:
            continue
        df = frame(json.loads(r["bars"]))
        sig = pd.Timestamp(datetime.fromisoformat(t["entry_utc"]))
        sig = sig.ceil("5min")                                   # القرار يُعرف عند إغلاق الشمعة الجارية
        before, after = df[df.index + pd.Timedelta(minutes=5) <= sig], df[df.index >= sig]
        if len(after) == 0:
            continue
        a = db.one("SELECT data FROM alerts WHERE id=?", (t["alert_id"],)) or {}
        d = json.loads(a.get("data") or "{}")
        day = sig.tz_convert(ET).date()
        row = {"day": t["day"], "symbol": t["symbol"], "entry_live": t["entry"], "stop": t["stop"],
               "target": t["target"], "r_live": t["r_mult"], "status_live": t["status"]}
        for s in SCEN:
            q = model.quote(float(before["close"].iloc[-1]) if len(before) else t["ref_price"], before,
                            d.get("rvol"), d.get("dollar_volume"), s)
            o = Order(t["symbol"], sig.to_pydatetime(), int(t["shares"]), t["stop"], t["target"], time_exit_for(day, app))
            tr = simulate(o, after, q, model, app.execution, before)
            row[f"r_{s}"] = None if tr.skipped or tr.r_multiple is None else round(tr.r_multiple, 3)
            if s == "base":
                row.update(skipped=tr.skipped, exit_reason=None if tr.skipped else tr.exit.reason,
                           entry_sim=None if tr.skipped else tr.entry.price, r_gross=None if tr.skipped or tr.r_gross is None else round(tr.r_gross, 3),
                           cost_pct=None if tr.skipped else round(tr.cost_pct * 100, 2), halted=tr.halted,
                           spread_pct=round(q.spread_pct * 100, 2))
        out.append(row)
    return pd.DataFrame(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(BASE, "data", "headsup.db"))
    a = ap.parse_args(argv)
    x = replay(Store(a.db), C.load(os.path.join(BASE, "config.json")))
    pd.set_option("display.width", 200)
    print(x.to_string(index=False))
    f = x[x.skipped.isna()]
    print(f"\nصفقات: {len(x)} • نُفذت في المحاكاة: {len(f)} • تخطتها (فجوة): {x.skipped.notna().sum()}")
    print("متوسط R:", {k: round(f[k].mean(), 3) for k in ["r_live", "r_gross", *[f"r_{s}" for s in SCEN]]})
    print("متوسط التكلفة الفعلية %:", round(f.cost_pct.mean(), 2), "• أسباب الخروج:", f.exit_reason.value_counts().to_dict())


if __name__ == "__main__":
    main()
