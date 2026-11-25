# -*- coding: utf-8 -*-
"""التنفيذ الورقي الدقيق بالثانية (hu/ticks.py)."""
import os
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hu import ticks as TK  # noqa: E402

UTC = timezone.utc
DAY = "2026-11-20"
T0 = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)        # 11:00 نيويورك
CUT = datetime(2026, 11, 20, 21, 30, tzinfo=UTC)      # 16:30 نيويورك (بعد 15:55)


def z(sec):
    return (T0 + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Stub:
    def __init__(self, trades=(), quotes=(), fail=False):
        self.tr = [{"t": z(s), "p": p, "s": sz, "c": c} for s, p, sz, c in trades]
        self.qu = [{"t": z(s), "bp": b, "ap": a} for s, b, a in quotes]
        self.fail, self.calls = fail, 0

    def _paged(self, base, path, params, key):
        self.calls += 1
        if self.fail:
            raise RuntimeError("403")
        a, b = pd.Timestamp(params["start"]), pd.Timestamp(params["end"])
        rows = self.tr if key == "trades" else self.qu
        return [r for r in rows if a <= pd.Timestamp(r["t"]) < b]


def bars(rows):
    idx = [pd.Timestamp(T0 + timedelta(minutes=i)) for i in range(len(rows))]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def trade(kind="A", **kw):
    t = {"symbol": "XYZ", "kind": kind, "day": DAY, "created_utc": T0.isoformat(), "stop": 1.90,
         "trigger": 2.10, "shares": 500, "status": "OPEN" if kind == "A" else "PENDING", "entry": 2.0, "entry_utc": T0.isoformat()}
    t.update(kw)
    return t


def setup_function(_):
    TK._CACHE.clear()


def test_entry_at_prevailing_ask_and_stop_slips_to_bid():
    c = Stub(trades=[(70, 1.89, 300, ["@"])],
             quotes=[(0.5, 1.99, 2.00), (1.2, 2.00, 2.02), (70.2, 1.86, 1.88)])
    r = TK.simulate(trade(), bars([[2.0, 2.01, 1.99, 2.0], [1.95, 1.96, 1.85, 1.87]]), CUT, c, 0.5, 2.0)
    assert r["fill_src"] == "tick"
    # العرض الساري لحظة وصول الأمر (16:00:01) هو تحديث 16:00:00.5، لا التحديث اللاحق
    assert r["entry"] == 2.00 and r["entry_utc"].endswith("16:00:01+00:00")
    assert r["target"] == 2.20                                                     # 2.00 + 2×0.10
    assert r["status"] == "STOP" and r["exit"] == 1.86                             # انزلاق حقيقي تحت الوقف 1.90
    assert r["exit_utc"].endswith("16:01:10.250000+00:00")                         # صفقة التفعيل + 250ms
    assert "1.86" in r["note"]


def test_same_minute_target_then_stop_resolved_by_ticks():
    # شمعة واحدة لمست الهدف والوقف: محاكي الشموع كان سيحسبها وقفاً، والصفقات تقول إن الهدف جاء أولاً
    c = Stub(trades=[(20, 2.27, 200, ["@"]), (50, 1.88, 200, ["@"])],
             quotes=[(1.1, 1.99, 2.00), (50.1, 1.87, 1.89)])
    r = TK.simulate(trade(), bars([[2.0, 2.27, 1.88, 1.9]]), CUT, c, 0.5, 2.0)
    assert r["entry"] == 2.00 and r["target"] == 2.20
    assert r["status"] == "TARGET" and r["exit"] == 2.20 and r["pnl"] > 0


def test_touching_target_is_not_a_fill_and_odd_lots_ignored():
    c = Stub(trades=[(20, 2.20, 200, ["@"]),          # ملامسة الهدف فقط
                     (30, 1.70, 5, ["@", "I"])],      # كسر وهمي بصفقة كسور: لا يفعّل الوقف
             quotes=[(1.1, 1.99, 2.00), (59, 2.10, 2.12)])
    r = TK.simulate(trade(), bars([[2.0, 2.20, 1.70, 2.1]]), datetime(2026, 11, 20, 17, 0, tzinfo=UTC), c, 0.5, 2.0)
    assert r["status"] == "OPEN" and r["exit"] is None


def test_buy_stop_fills_at_ask_after_trigger_print():
    c = Stub(trades=[(65, 2.11, 400, ["@"]), (200, 2.40, 300, ["@"])],
             quotes=[(65.3, 2.10, 2.13)])
    r = TK.simulate(trade("B", entry=None, entry_utc=None),
                    bars([[2.0, 2.05, 1.95, 2.0], [2.05, 2.15, 2.04, 2.12], [2.12, 2.2, 2.1, 2.2], [2.2, 2.4, 2.2, 2.38]]),
                    CUT, c, 0.5, 2.0)
    assert r["entry"] == 2.13 and r["entry_utc"].endswith("16:01:05.300000+00:00")
    assert r["target"] == 2.59
    assert r["status"] == "TIME" and r["exit"] == 2.38      # لا عروض عند 15:55 في هذا المثال ← آخر إغلاق


def test_cancel_when_stop_breaks_before_trigger():
    c = Stub(trades=[(10, 1.89, 300, ["@"])], quotes=[])
    r = TK.simulate(trade("B", entry=None, entry_utc=None), bars([[2.0, 2.0, 1.85, 1.9]]), CUT, c, 0.5, 2.0)
    assert r["status"] == "CANCELLED" and r.get("pnl") is None


def test_falls_back_when_ticks_unavailable_and_fees():
    assert TK.simulate(trade(), bars([[2.0, 2.0, 1.9, 2.0]]), CUT, Stub(fail=True), 0.5, 2.0) is None
    fees = {"sec_per_million": 20.60, "taf_per_share": 0.000195, "taf_max": 9.79}
    assert TK.sell_fees(2.0, 500, fees) == round(1000 * 20.6e-6 + 500 * 0.000195, 2)
    assert TK.sell_fees(2.0, 10_000_000, fees) >= 9.79


def test_entry_is_a_marketable_limit_not_a_blind_market_order():
    # عرض شاذ 2.50 بعد التنبيه مباشرة (سبريد منفجر): لا نشتري به، ننتظر عرضاً ≤ 2.04 (سعر التنبيه + 2%)
    c = Stub(quotes=[(1.1, 1.99, 2.50), (30, 2.00, 2.03)])
    r = TK.simulate(trade(), bars([[2.0, 2.02, 1.99, 2.0]]), CUT, c, 0.5, 2.0, 2.0)
    assert r["entry"] == 2.03 and r["entry_utc"].endswith("16:00:30+00:00")
    # لم يأتِ أي عرض ضمن الحد خلال دقيقتين ← لا صفقة
    TK._CACHE.clear()
    c2 = Stub(quotes=[(1.1, 2.40, 2.50), (100, 2.45, 2.55)])
    r2 = TK.simulate(trade(), bars([[2.4, 2.6, 2.4, 2.5]]), CUT, c2, 0.5, 2.0, 2.0)
    assert r2["status"] == "CANCELLED" and r2.get("pnl") is None and "لم يتنفذ" in r2["note"]


def test_base_stop_follows_actual_entry():
    t = trade("BASE", entry=1.00, stop=0.96)                 # وقف 4% تحت الدخول
    c = Stub(quotes=[(1.1, 0.99, 1.01)])
    r = TK.simulate(t, bars([[1.0, 1.01, 0.99, 1.0]]), datetime(2026, 11, 20, 17, 0, tzinfo=UTC), c, 0.5, 2.0, 2.0)
    assert r["entry"] == 1.01 and r["stop"] == 0.9696


def test_quiet_stock_uses_standing_quote():
    # سهم هادئ: آخر تحديث عروض قبل التنبيه بـ40 ثانية ولا تحديث بعده — العرض ما زال قائماً وقابلاً للتنفيذ
    c = Stub(quotes=[(-40, 1.98, 2.01)])
    r = TK.simulate(trade(), bars([[2.0, 2.01, 1.99, 2.0]]), datetime(2026, 11, 20, 17, 0, tzinfo=UTC), c, 0.5, 2.0, 2.0)
    assert r["fill_src"] == "tick" and r["entry"] == 2.01 and r["status"] == "OPEN"
