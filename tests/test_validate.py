# -*- coding: utf-8 -*-
"""اختبارات التحقق الإحصائي: معايرة p، تصحيح التعدد، قفل بيانات الاختبار، والحكم."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from bt import stats as S  # noqa: E402
from bt.registry import Registry, RegistryError, TestSetLocked  # noqa: E402
from bt.validate import build_report, param_stability, split, to_markdown, walk_forward_windows  # noqa: E402
from hu import config as C  # noqa: E402

APP = C.load(os.path.join(ROOT, "config.json"))


def days(n, start=date(2023, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def trades(mu, n_days=150, per_day=2, seed=0, sd=1.0, cost_gap=0.1):
    rng = np.random.default_rng(seed)
    ds = [d for d in days(n_days) for _ in range(per_day)]
    r = rng.normal(mu, sd, len(ds))
    return pd.DataFrame({"day": ds, "r_optimistic": r + cost_gap, "r_base": r, "r_pessimistic": r - cost_gap})


class TestStats(unittest.TestCase):
    def test_bootstrap_calibrated_under_null(self):
        """بلا ميزة: p < 0.05 في نحو 5% فقط من العوالم (لا إيجابيات كاذبة زائدة)."""
        hits = 0
        for seed in range(300):
            v = np.random.default_rng(seed).normal(0, 1, 120)
            hits += S.block_bootstrap_p(pd.Series(v), n=2000, seed=seed) < 0.05
        self.assertLess(hits / 300, 0.09)
        self.assertGreater(hits / 300, 0.02)

    def test_bootstrap_detects_real_edge(self):
        v = np.random.default_rng(1).normal(0.4, 1, 150)
        self.assertLess(S.block_bootstrap_p(pd.Series(v), n=5000), 0.01)

    def test_daily_blocks(self):
        d = S.r_by_day([date(2023, 1, 2)] * 3 + [date(2023, 1, 3)], [1, 1, 1, -1])
        self.assertEqual(list(d.values), [3.0, -1.0])

    def test_bh_known_values(self):
        q = S.benjamini_hochberg({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5, "e": None})
        self.assertAlmostEqual(q["a"], 0.04)
        self.assertAlmostEqual(q["c"], 0.04 * 4 / 3 if False else 0.05333333, places=6)
        self.assertAlmostEqual(q["b"], 0.05333333, places=6)
        self.assertAlmostEqual(q["d"], 0.5)
        self.assertIsNone(q["e"])

    def test_more_trials_deflate_sharpe(self):
        v = pd.Series(np.random.default_rng(3).normal(0.15, 1, 250))
        one, many = S.deflated_sharpe(v, 1), S.deflated_sharpe(v, 200, sr_var_trials=0.01)
        self.assertGreater(one, many)

    def test_without_top5_exposes_luck(self):
        r = [-0.3] * 60 + [8, 7, 6, 5, 5]
        self.assertGreater(np.mean(r), 0)
        self.assertLess(S.without_top(r, 5), 0)

    def test_random_entry_p(self):
        sig = pd.Series([0.3] * 50)
        rand = [pd.Series(np.random.default_rng(s).normal(0, 0.5, 50)) for s in range(200)]
        self.assertLess(S.random_entry_p(sig, rand), 0.05)
        self.assertGreater(S.random_entry_p(pd.Series([0.0] * 50), rand), 0.3)

    def test_sensitivity_flags_sharp_peak(self):
        peak = lambda p: 0.5 if abs(p["stop"] - 1.5) < 1e-9 else -0.2   # noqa: E731
        smooth = lambda p: 0.3 - 0.01 * abs(p["stop"] - 1.5)              # noqa: E731
        self.assertTrue(S.sensitivity(peak, {"stop": 1.5})[0].flips)
        self.assertFalse(S.sensitivity(smooth, {"stop": 1.5})[0].flips)

    def test_metrics(self):
        m = S.metrics([date(2023, 1, 2)] * 2 + [date(2023, 1, 3)] * 2, [1.0, -1.0, -1.0, -1.0])
        self.assertEqual((m["n_trades"], m["n_days"], m["max_consec_losses"]), (4, 2, 3))
        self.assertAlmostEqual(m["profit_factor"], 1 / 3)
        self.assertAlmostEqual(m["max_drawdown_r"], -2.0)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "h.jsonl")
        self.r = Registry(self.path)

    def test_test_set_touched_once(self):
        p = {"stop_atr": 1.5, "target_r": 2}
        with self.assertRaises(RegistryError):
            self.r.touch_test("h1", p, "cfg", {"p_bootstrap": 0.2})           # غير مسجلة
        self.r.register("h1", "زخم مع وقف واسع", p, "cfg")
        self.r.touch_test("h1", p, "cfg", {"p_bootstrap": 0.2})
        with self.assertRaises(TestSetLocked):
            self.r.touch_test("h1", p, "cfg", {"p_bootstrap": 0.01})          # لا محاولة ثانية

    def test_changed_params_need_new_name(self):
        self.r.register("h1", "x", {"a": 1}, "cfg")
        self.r.register("h1", "x", {"a": 1}, "cfg")                            # نفسها: لا شيء
        with self.assertRaises(RegistryError):
            self.r.register("h1", "x", {"a": 2}, "cfg")
        with self.assertRaises(RegistryError):
            self.r.touch_test("h1", {"a": 2}, "cfg", {})

    def test_failed_hypotheses_count(self):
        for i in range(5):
            self.r.register(f"h{i}", "", {"i": i})
        self.r.touch_test("h0", {"i": 0}, None, {"p_bootstrap": 0.03})
        self.assertEqual(self.r.n_trials, 5)
        pv = self.r.test_pvalues()
        self.assertEqual((pv["h0"], pv["h3"]), (0.03, None))


class TestValidate(unittest.TestCase):
    def test_split_with_embargo(self):
        ds = days(22, date(2022, 12, 1)) + days(10, date(2023, 1, 3))     # 1–30 ديسمبر ثم يناير
        df = pd.DataFrame({"day": ds, "r_base": 0.0})
        tr, te = split(df, APP, trading_days=ds)
        self.assertEqual(max(tr["day"]), [d for d in ds if d <= date(2022, 12, 31)][-6])
        self.assertEqual(min(te["day"]), date(2023, 1, 3))

    def test_walk_forward(self):
        w = walk_forward_windows(date(2016, 1, 1), date(2022, 12, 31), 24, 6)
        self.assertEqual(w[0], (date(2016, 1, 1), date(2017, 12, 31), date(2018, 1, 1), date(2018, 6, 30)))
        self.assertLessEqual(w[-1][3], date(2022, 12, 31))
        self.assertEqual(len(w), 10)
        self.assertEqual(param_stability([{"s": 1}, {"s": 1}, {"s": 2}])["changes"], 1)

    def test_no_edge_is_rejected_or_inconclusive(self):
        rep = build_report("null", trades(0.0, seed=4), APP)
        self.assertIn(rep.verdict, ("reject", "inconclusive"))
        self.assertNotEqual(rep.verdict, "candidate")

    def test_negative_rejected(self):
        rep = build_report("neg", trades(-0.2, seed=2), APP)
        self.assertEqual(rep.verdict, "reject")

    def test_real_edge_candidate(self):
        t = trades(0.35, n_days=200, seed=5)
        rand = [S.r_by_day(t["day"], np.random.default_rng(s).normal(0, 1, len(t))) for s in range(200)]
        rep = build_report("edge", t, APP, random_draws=rand)
        self.assertEqual(rep.verdict, "candidate", rep.reasons)
        self.assertIn("مرشحة", to_markdown(rep))

    def test_edge_without_random_test_is_not_candidate(self):
        rep = build_report("edge2", trades(0.35, n_days=200, seed=5), APP)
        self.assertEqual(rep.verdict, "inconclusive")
        self.assertTrue(any("العشوائي" in r for r in rep.reasons))

    def test_small_sample_inconclusive(self):
        t = trades(0.6, n_days=20, seed=6)
        rand = [S.r_by_day(t["day"], np.random.default_rng(s).normal(0, 1, len(t))) for s in range(100)]
        rep = build_report("small", t, APP, random_draws=rand)
        self.assertEqual(rep.verdict, "inconclusive")
        self.assertTrue(any("عدد الصفقات" in r for r in rep.reasons))

    def test_many_trials_kill_marginal_result(self):
        reg = Registry(os.path.join(tempfile.mkdtemp(), "h.jsonl"))
        for i in range(40):
            reg.register(f"h{i}", "", {"i": i})
            reg.touch_test(f"h{i}", {"i": i}, None, {"p_bootstrap": 0.5})
        reg.register("marg", "", {"m": 1})
        t = trades(0.13, n_days=200, seed=8)
        rand = [S.r_by_day(t["day"], np.random.default_rng(s).normal(0, 1, len(t))) for s in range(200)]
        alone = build_report("marg", t, APP, random_draws=rand)
        crowd = build_report("marg", t, APP, registry=reg, random_draws=rand)
        self.assertLess(alone.q_bh, crowd.q_bh)
        self.assertEqual(crowd.n_trials, 41)


if __name__ == "__main__":
    unittest.main()
