# -*- coding: utf-8 -*-
"""اختبارات الدراسة v3: النسخة السريعة = المرجعية، التكلفة = CostModel، الإشارة بلا نظر للمستقبل،
المجموعة الشاملة لا تفقد أي يوم إشارة، وتشغيل كامل صغير على بيانات مصطنعة."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import research_v3 as R3  # noqa: E402
from bt import fastsim as FS  # noqa: E402
from bt import signals as SG  # noqa: E402
from bt.costs import CostModel, CostQuote  # noqa: E402
from bt.execution import Order, simulate  # noqa: E402
from bt.registry import Registry, research_config_hash  # noqa: E402
from hu import config as C  # noqa: E402
from pit.models import ET, UTC  # noqa: E402
from pit.store import ParquetStore  # noqa: E402

APP = C.load(os.path.join(ROOT, "config.json"))
DAY = date(2024, 3, 12)


def rand_day(seed, n=78, p0=1.5, gaps=False, trend=0.0):
    rng = np.random.default_rng(seed)
    t = datetime.combine(DAY, time(9, 30), tzinfo=ET)
    idx, rows, p = [], [], p0
    for i in range(n):
        if gaps and i in (20, 45) and rng.random() < 0.8:
            t += timedelta(minutes=int(rng.choice([5, 10, 15])))
        o = p
        c = max(0.05, o * np.exp(rng.normal(trend, 0.012)))
        h = max(o, c) * (1 + abs(rng.normal(0, 0.006)))
        l = min(o, c) * (1 - abs(rng.normal(0, 0.006)))
        if rng.random() < 0.05:                        # فجوات سعرية أحياناً
            o = o * (1 + rng.normal(0, 0.05)); h, l = max(h, o), min(l, o)
        rows.append((o, h, l, c, float(rng.integers(1000, 90000))))
        idx.append(t.astimezone(UTC))
        t += timedelta(minutes=5)
        p = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))


class TestFastSimMatchesReference(unittest.TestCase):
    def test_equivalence_random(self):
        model = CostModel(APP.costs)
        f = APP.costs.fees
        mism, n = [], 0
        for seed in range(250):
            df = rand_day(seed, p0=float(np.random.default_rng(seed).choice([0.45, 1.3, 3.7])), gaps=seed % 3 == 0)
            d = SG.Day.from_frame(df)
            rng = np.random.default_rng(1000 + seed)
            j = int(rng.integers(3, len(d) - 2))
            ref = d.c[j - 1]
            dist = ref * float(rng.uniform(0.01, 0.08))
            stop, target = ref - dist, ref + float(rng.choice([1.5, 2, 3])) * dist
            sp, sl, fl = float(rng.uniform(0.002, 0.02)), float(rng.uniform(0, 0.01)), float(rng.choice([0.003, 0.005, 0.01]))
            q = CostQuote(ref, sp, sl, sl, fl, "base", "x", 1.0)
            sig = df.index[j].to_pydatetime()
            te = datetime.combine(DAY, time(15, 55), tzinfo=ET).astimezone(UTC)
            tr = simulate(Order("X", sig, 500, stop, target, te), df.iloc[j:], q, model, APP.execution, df.iloc[:j])
            resume = FS.halt_resumes(list(d.starts), 5, APP.execution.halt_gap_min)
            fr = FS.sim(d.o, d.h, d.l, d.c, d.starts, j, stop, target, 15 * 60 + 55, resume, sp / 2, sl, sl, fl, 500,
                        APP.costs.tick.lt_1, APP.costs.tick.ge_1, f.sec_per_million, f.taf_per_share, f.taf_max,
                        APP.execution.no_entry_bars_after_halt)
            n += 1
            if tr.skipped:
                if fr[0] is not None or fr[2] != tr.skipped:
                    mism.append((seed, "skip", tr.skipped, fr[2]))
            elif fr[0] is None or abs(fr[0] - tr.r_multiple) > 1e-9 or fr[2] != tr.exit.reason or fr[4] != tr.halted:
                mism.append((seed, tr.r_multiple, tr.exit.reason, fr[:3]))
        self.assertEqual(mism, [], f"{len(mism)}/{n} اختلاف")


class TestCostArrays(unittest.TestCase):
    def test_matches_costmodel(self):
        model = CostModel(APP.costs)
        for seed in range(30):
            df = rand_day(seed, p0=[0.6, 2.2, 4.4][seed % 3])
            d = SG.Day.from_frame(df)
            cs, rg = R3.cost_arrays(d)
            for k in (0, 1, 2, 5, 25, 60):
                rvol, dv = [1.5, 5][k % 2], [1e5, 1e6][seed % 2]
                q = model.quote(d.c[k], df.iloc[:k + 1], rvol, dv, "base")
                sp, sl = R3.quote_at(d.c[k], cs[k], rg[k], rvol, dv, APP.costs)
                self.assertAlmostEqual(sp, q.spread_pct, 12)
                self.assertAlmostEqual(sl, q.slip_entry_pct, 12)


class TestSignal(unittest.TestCase):
    P = SG.TriggerParams()

    def find(self):
        for seed in range(400):
            df = rand_day(seed, trend=0.004)
            d = SG.Day.from_frame(df)
            avg = float(d.v.sum()) / 6
            k = SG.first_trigger(d, SG.features(d), 1.0 * d.o[0] / 1.0 * 0.97, avg, self.P)
            if k is not None and k < len(d) - 5:
                return df, d, avg, k
        self.fail("لا إشارة في البيانات المصطنعة")

    def test_future_bars_do_not_change_signal(self):
        df, d, avg, k = self.find()
        prev = d.o[0] * 0.97
        g = df.copy()
        g.iloc[k + 1:, :] = g.iloc[k + 1:, :].values[::-1] * 3          # عبث كامل بالمستقبل
        d2 = SG.Day.from_frame(g)
        self.assertEqual(SG.first_trigger(d2, SG.features(d2), prev, avg, self.P), k)

    def test_signal_conditions_hold(self):
        df, d, avg, k = self.find()
        f, prev = SG.features(d), d.o[0] * 0.97
        self.assertTrue(0.05 <= d.c[k] / prev - 1 <= 0.25)
        self.assertGreaterEqual(f["cumv"][k], 3 * avg)
        self.assertGreater(d.c[k], f["vwap"][k])
        self.assertTrue(9 * 60 + 45 <= d.starts[k] + 5 <= 15 * 60)

    def test_superset_never_misses_a_signal_day(self):
        missed = 0
        for seed in range(300):
            df = rand_day(seed, trend=float(np.random.default_rng(seed).normal(0.002, 0.003)))
            d = SG.Day.from_frame(df)
            prev, avg = d.o[0] * 0.98, float(d.v.sum()) / float(np.random.default_rng(seed).uniform(2, 6))
            k = SG.first_trigger(d, SG.features(d), prev, avg, self.P)
            m = SG.superset_mask(np.array([prev]), np.array([prev]), np.array([d.h.max()]), np.array([d.v.sum()]),
                                 np.array([avg]), np.array([1e6]), self.P, 0.2, 5.0, 2.5e5)[0]
            missed += int(k is not None and not m)
        self.assertEqual(missed, 0)


class TestEndToEnd(unittest.TestCase):
    def test_small_pipeline(self):
        root, bars_dir = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            # يومي: 25 يوماً هادئاً ثم يوم الحدث
            days = [d for d in (DAY - timedelta(days=i) for i in range(40, -1, -1)) if d.weekday() < 5][-26:]
            rows = [dict(symbol="ZZZ", date=d, open=1.0, high=1.01, low=0.99, close=1.0, volume=400000.0, vwap=1.0,
                         trades=10, factor=1.0) for d in days[:-1]]
            ev_df = rand_day(7, p0=1.0, trend=0.006)
            ev_df["volume"] *= 40
            rows.append(dict(symbol="ZZZ", date=DAY, open=float(ev_df.open.iloc[0]), high=float(ev_df.high.max()),
                             low=float(ev_df.low.min()), close=float(ev_df.close.iloc[-1]), volume=float(ev_df.volume.sum()),
                             vwap=1.0, trades=10, factor=1.0))
            os.makedirs(os.path.join(root, "daily"))
            pd.DataFrame(rows).to_parquet(os.path.join(root, "daily", "2024.parquet"), index=False)
            pd.DataFrame([dict(asset_id="z", symbol="ZZZ", name="Zed Corp", exchange="NASDAQ", status="active",
                               symbol_shared=False, cik=None, first_bar=days[0], last_bar=DAY)]) \
                .astype({"cik": "Int64"}).to_parquet(os.path.join(root, "assets.parquet"), index=False)
            store = ParquetStore(root)
            reg = Registry(os.path.join(root, "h.jsonl"))
            main = Registry(os.path.join(ROOT, "research", "hypotheses.jsonl")).hypotheses()
            for hid in ("H1_mom_tight_2r", "H2_mom_wide_2r"):
                reg.register(hid, main[hid]["description"], main[hid]["params"], research_config_hash(APP))
            hyps = reg.hypotheses()
            ev = R3.select_events(store, hyps["H2_mom_wide_2r"]["params"])
            self.assertEqual(list(ev["date"]), [DAY])
            b = ev_df.reset_index().rename(columns={"index": "ts"})
            b.insert(0, "symbol", "ZZZ"); b.insert(1, "date", DAY)
            b.to_parquet(os.path.join(bars_dir, "2024-03.parquet"), index=False)
            trades, rand, cnt = R3.analyze(store, ev, bars_dir, APP, hyps, n_random=20, seed=1, sec_ok=False)
            self.assertEqual(cnt["stock_days"], 1)
            if cnt["triggers"]:
                self.assertEqual(sorted(trades["hid"]), ["H1_mom_tight_2r", "H2_mom_wide_2r"])
                self.assertTrue(all(trades["test"]))
                for hid in hyps:
                    if trades.loc[trades.hid == hid, "r_base"].notna().any():
                        self.assertEqual(len(rand[hid][0][1]), 20)
                self.assertTrue((trades["r_optimistic"].fillna(0) >= trades["r_pessimistic"].fillna(0) - 1e-12).all())
        finally:
            shutil.rmtree(root, ignore_errors=True); shutil.rmtree(bars_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
