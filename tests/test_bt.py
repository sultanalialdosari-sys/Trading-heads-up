# -*- coding: utf-8 -*-
"""اختبارات محرك التكاليف ومحاكاة التنفيذ والتوقفات. كل قاعدة في وثيقة البنية لها اختبار هنا."""
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from bt import costs as K  # noqa: E402
from bt.execution import Order, detect_halts, simulate, simulate_from_store, time_exit_for  # noqa: E402
from hu import config as C  # noqa: E402
from pit.models import ET, UTC  # noqa: E402
from pit.store import ParquetStore  # noqa: E402

APP = C.load(os.path.join(ROOT, "config.json"))
MODEL = K.CostModel(APP.costs)
DAY = date(2026, 3, 10)


def et(h, m=0, d=DAY):
    return datetime.combine(d, time(h, m), tzinfo=ET).astimezone(UTC)


def bars(rows, start=(10, 0), tf=5, gaps=None):
    """rows = [(o,h,l,c), ...] تبدأ من start كل tf دقيقة. gaps = {i: دقائق إضافية قبل الشمعة i}."""
    t, idx = et(*start), []
    for i in range(len(rows)):
        t += timedelta(minutes=(gaps or {}).get(i, 0))
        idx.append(t)
        t += timedelta(minutes=tf)
    return pd.DataFrame([{"open": o, "high": h, "low": l, "close": c, "volume": 1e4} for o, h, l, c in rows],
                        index=pd.DatetimeIndex(idx))


def flat(n, p=2.0, start=(9, 0)):
    return bars([(p, p * 1.002, p * 0.998, p)] * n, start=start)


def quote(price=2.0, scenario="base", before=None, rvol=5.0, dv=1e6):
    return MODEL.quote(price, before if before is not None else flat(20, price), rvol, dv, scenario)


def order(stop=1.80, target=2.40, sig=None):
    return Order("TST", sig or et(10, 0), 500, stop, target, time_exit_for(DAY, APP))


class TestCosts(unittest.TestCase):
    def test_corwin_schultz(self):
        self.assertEqual(K.corwin_schultz(1, 1, 1, 1), 0.0)
        self.assertEqual(K.corwin_schultz(1.0, 0.9, 1.5, 1.4), 0.0)              # قفزة اتجاه = سالب ← صفر
        self.assertGreater(K.corwin_schultz(1.02, 0.98, 1.02, 0.98), 0.0)        # تذبذب حول نفس المستوى = سبريد

    def test_cost_monotonic_in_price_and_liquidity(self):
        cheap = MODEL.quote(0.60, flat(20, 0.60), 5, 1e6, "base")
        dear = MODEL.quote(4.50, flat(20, 4.50), 5, 1e6, "base")
        self.assertGreater(cheap.model_round_trip_pct, dear.model_round_trip_pct)
        thin = MODEL.quote(4.50, flat(20, 4.50), 1.5, 1e5, "base")
        self.assertGreater(thin.spread_pct, dear.spread_pct)
        self.assertAlmostEqual(thin.liq_mult, 1.5 * 1.3)
        unknown = MODEL.quote(4.50, flat(20, 4.50), None, None, "base")         # مجهول = ضعيف
        self.assertEqual(unknown.liq_mult, thin.liq_mult)

    def test_scenarios_ordered(self):
        q = {s: quote(scenario=s).round_trip_pct for s in ("optimistic", "base", "pessimistic")}
        self.assertLessEqual(q["optimistic"], q["base"])
        self.assertLessEqual(q["base"], q["pessimistic"])
        self.assertGreaterEqual(q["pessimistic"], 0.01)
        with self.assertRaises(ValueError):
            quote(scenario="dream")

    def test_slip_capped(self):
        wild = bars([(2.0, 2.6, 1.5, 2.0)] * 20, start=(9, 0))
        q = MODEL.quote(2.0, wild, 5, 1e6, "base")
        self.assertEqual(q.slip_entry_pct, APP.costs.slip_cap_per_side.to_5)

    def test_tick_rounding_against_us(self):
        self.assertEqual(K.round_buy(0.51234, APP.costs), 0.5124)
        self.assertEqual(K.round_sell(0.51239, APP.costs), 0.5123)
        self.assertEqual(K.round_buy(2.001, APP.costs), 2.01)
        self.assertEqual(K.round_sell(2.009, APP.costs), 2.00)
        self.assertEqual(K.round_buy(2.00, APP.costs), 2.00)

    def test_fees(self):
        self.assertEqual(MODEL.sell_fees(2.0, 500), 0.13)           # SEC 0.03 (1000$ × 20.6/مليون مقربة لأعلى) + TAF 0.10
        self.assertEqual(MODEL.sell_fees(2.0, 10_000_000), round(412.0 + 9.79, 2))


class TestExecution(unittest.TestCase):
    def sim(self, rows, o=None, q=None, gaps=None, before=None, start=(10, 0)):
        return simulate(o or order(), bars(rows, start=start, gaps=gaps), q or quote(), MODEL, APP.execution, before)

    def test_entry_next_bar_open_with_costs(self):
        t = self.sim([(2.00, 2.05, 1.98, 2.02)] + [(2.02, 2.05, 1.95, 2.02)] * 3)
        q = t.quote
        self.assertEqual(t.entry.ref_price, 2.00)
        self.assertEqual(t.entry.price, K.round_buy(2.0 * (1 + q.spread_pct / 2 + q.slip_entry_pct), APP.costs))
        self.assertGreater(t.entry.price, 2.00)

    def test_bars_before_signal_rejected(self):
        with self.assertRaises(ValueError):
            simulate(order(sig=et(10, 5)), bars([(2, 2, 2, 2)] * 3), quote(), MODEL, APP.execution)

    def test_stop_first_when_both_hit(self):
        t = self.sim([(2.00, 2.05, 1.98, 2.0), (2.0, 2.50, 1.75, 2.1)])
        self.assertEqual(t.exit.reason, "stop")
        self.assertLess(t.r_multiple, -1.0)                       # الوقف + التكاليف أسوأ من −1R

    def test_target_needs_tick_through(self):
        t = self.sim([(2.00, 2.05, 1.98, 2.0), (2.0, 2.40, 1.95, 2.3), (2.3, 2.35, 2.2, 2.3)])
        self.assertNotEqual(t.exit.reason, "target")              # لمس 2.40 فقط لا يكفي
        t = self.sim([(2.00, 2.05, 1.98, 2.0), (2.0, 2.41, 1.95, 2.3)])
        self.assertEqual((t.exit.reason, t.exit.price), ("target", 2.40))

    def test_gap_through_stop_fills_at_open(self):
        t = self.sim([(2.00, 2.05, 1.98, 2.0), (1.50, 1.60, 1.45, 1.55)])
        self.assertEqual(t.exit.reason, "gap_stop")
        self.assertEqual(t.exit.ref_price, 1.50)
        self.assertLess(t.r_multiple, -2.0)                       # أسوأ بكثير من −1R

    def test_gap_above_target_fills_at_open(self):
        t = self.sim([(2.00, 2.05, 1.98, 2.0), (2.60, 2.70, 2.55, 2.6)])
        self.assertEqual((t.exit.reason, t.exit.price), ("gap_target", 2.60))

    def test_entry_gap_skips(self):
        self.assertEqual(self.sim([(1.70, 1.75, 1.65, 1.7)]).skipped, "gap_below_stop")
        self.assertEqual(self.sim([(2.50, 2.55, 2.45, 2.5)]).skipped, "gap_above_target")

    def test_time_exit(self):
        t = self.sim([(2.00, 2.05, 1.98, 2.0)] + [(2.0, 2.05, 1.95, 2.0)] * 80, start=(10, 0))
        self.assertEqual(t.exit.reason, "time")
        self.assertEqual(t.exit.ts, et(15, 55))

    def test_halt_resume_below_stop(self):
        rows = [(2.0, 2.02, 1.99, 2.0)] * 8 + [(1.40, 1.45, 1.35, 1.40)]
        t = self.sim(rows, start=(10, 0), gaps={8: 10})           # 10 دقائق بلا شموع ثم استئناف
        self.assertTrue(t.halted)
        self.assertEqual((t.exit.reason, t.exit.ref_price), ("halt_resume", 1.40))

    def test_illiquid_gap_is_not_a_halt(self):
        rows = [(2.0, 2.02, 1.99, 2.0)] * 8
        b = bars(rows, gaps={2: 10, 4: 10, 6: 10})                # فجوات متكررة = تداول متقطع، لا توقف
        self.assertEqual(detect_halts(b, "5Min", 5), [])

    def test_no_entry_right_after_halt(self):
        before = bars([(2.0, 2.02, 1.99, 2.0)] * 8, start=(9, 15))          # حتى 9:55 ثم توقف
        rows = [(2.1, 2.15, 2.05, 2.1)] * 5
        t = self.sim(rows, o=order(sig=et(10, 10)), before=before, start=(10, 10))
        self.assertEqual(t.entry.ts, et(10, 20))                  # تخطى شمعتين بعد الاستئناف

    def test_floor_applied(self):
        # عند 2$ يكفي تقريب السنت وحده لبلوغ 1%؛ عند 4.37$ لا يكفي فتعمل الأرضية
        tight = MODEL.quote(4.37, flat(20, 4.37), 5, 1e6, "pessimistic")
        t = self.sim([(4.37, 4.40, 4.35, 4.37)] * 82, o=order(stop=3.90, target=5.20), q=tight)
        self.assertGreaterEqual(t.cost_pct, 0.010 - 1e-6)
        self.assertGreater(t.floor_adj, 0)

    def test_pessimistic_never_better(self):
        rows = [(2.00, 2.05, 1.98, 2.0), (2.0, 2.41, 1.95, 2.3)]
        r = {s: self.sim(rows, q=quote(scenario=s)).r_multiple for s in ("optimistic", "base", "pessimistic")}
        self.assertGreaterEqual(r["optimistic"], r["base"])
        self.assertGreaterEqual(r["base"], r["pessimistic"])

    def test_costs_eat_r(self):
        t = self.sim([(2.00, 2.05, 1.98, 2.0), (2.0, 2.41, 1.95, 2.3)])
        self.assertGreater(t.r_gross, t.r_multiple)


class TestFromStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_quote_sees_only_past(self):
        full = bars([(2.0, 2.02, 1.98, 2.0)] * 12 + [(2.0, 2.45, 1.99, 2.4)] + [(2.4, 2.5, 2.3, 2.4)] * 5, start=(9, 30))
        seen = []

        def fetch(sym, day, tf):
            return full

        s = ParquetStore(self.root, intraday_fetcher=fetch)
        sig = et(10, 30)                                           # بعد 12 شمعة (9:30–10:25)
        orig = K.CostModel.quote

        def spy(self_, price, before, *a, **k):
            seen.append(before.index.max())
            return orig(self_, price, before, *a, **k)
        K.CostModel.quote = spy
        try:
            t = simulate_from_store(s, "TST", sig, stop=1.90, target=2.30, app=APP)
        finally:
            K.CostModel.quote = orig
        self.assertEqual(seen[0], pd.Timestamp(et(10, 25)))       # آخر شمعة أُغلقت عند 10:30
        self.assertEqual(t.entry.ts, et(10, 30))
        self.assertEqual(t.exit.reason, "target")


if __name__ == "__main__":
    unittest.main()
