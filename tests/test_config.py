# -*- coding: utf-8 -*-
"""اختبارات ملف الإعدادات المركزي: لا تغيير في سلوك النظام الحي، والحراس تعمل."""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from hu import config as C  # noqa: E402
from hu import extended as X  # noqa: E402
from hu import rules as RL  # noqa: E402
from hu.engine import load_cfg  # noqa: E402

PATH = os.path.join(ROOT, "config.json")


def raw():
    return json.load(open(PATH, encoding="utf-8"))


def rejects(test, data, needle):
    with test.assertRaises(C.ConfigError) as cm:
        C.parse(data)
    test.assertIn(needle, str(cm.exception))


class TestConfig(unittest.TestCase):
    def test_repo_config_valid(self):
        app = C.load(PATH)
        self.assertEqual(app.version, 3)
        self.assertFalse(app.paper.live_orders)
        self.assertEqual(app.costs.scenarios["base"], 0.005)
        self.assertEqual(app.exits.same_bar_rule, "stop_first")

    def test_live_values_unchanged(self):
        """النظام الحي يجب أن يرى نفس القيم قبل الترحيل وبعده."""
        cfg = load_cfg(PATH)
        expected = dict(RL.DEFAULTS)
        expected.update(X.EXT_DEFAULTS)
        expected["max_price"] = 7.0          # تغيير مقصود (25 نوفمبر 2026): توسيع المراقبة إلى 7$
        for k, v in expected.items():
            self.assertEqual(cfg[k], v, k)
        self.assertEqual(len(cfg["_config_hash"]), 64)

    def test_old_flat_file_still_works(self):
        old = dict(RL.DEFAULTS)
        old["min_score"] = 70
        app = C.parse(old)
        self.assertEqual(app.live.min_score, 70)
        self.assertEqual(app.paper.shares_per_trade, 500)

    def test_missing_file_gives_defaults(self):
        app = C.load(os.path.join(tempfile.gettempdir(), "nope-config.json"))
        self.assertEqual(app.live_dict()["max_price"], 5.0)

    def test_live_orders_blocked(self):
        d = raw(); d["paper"]["live_orders"] = True
        rejects(self, d, "الأوامر الحقيقية ممنوعة")

    def test_secret_blocked(self):
        d = raw(); d["alpaca_api_key"] = "x"
        rejects(self, d, "GitHub Secrets")
        d = raw(); d["universe"]["exclude_types"].append("PKABCDEFGHIJKLMNOP12")
        rejects(self, d, "GitHub Secrets")

    def test_unknown_key_blocked(self):
        d = raw(); d["live"]["min_scroe"] = 60
        rejects(self, d, "مفتاح غير معروف")

    def test_test_overlaps_train(self):
        d = raw(); d["validation"]["test_from"] = "2022-12-20"
        rejects(self, d, "تسرّب")
        d = raw(); d["validation"]["test_from"] = "2022-12-31"   # نفس آخر يوم تدريب
        rejects(self, d, "تسرّب")

    def test_embargo_trims_train_end(self):
        from datetime import date, timedelta
        v = C.load(PATH).validation
        days = [date(2022, 12, 1) + timedelta(days=i) for i in range(31)]
        days = [d for d in days if d.weekday() < 5]          # أيام عمل ديسمبر 2022
        self.assertEqual(v.train_cutoff(days), days[-6])     # حُذفت آخر 5 أيام

    def test_cost_order_and_judge(self):
        d = raw(); d["costs"]["scenarios"]["base"] = 0.02
        rejects(self, d, "متفائل")
        d = raw(); d["costs"]["judge_on"] = "optimistic"
        rejects(self, d, "المتفائل ممنوع")

    def test_survivorship_and_same_bar(self):
        d = raw(); d["universe"]["include_inactive"] = False
        rejects(self, d, "الناجين")
        d = raw(); d["exits"]["same_bar_rule"] = "target_first"
        rejects(self, d, "same_bar_rule")

    def test_bad_ranges(self):
        d = raw(); d["live"]["min_price"] = 8          # فوق max_price (7)
        rejects(self, d, "min_price")
        d = raw(); d["filters"]["gap_pct"] = {"min": 50, "max": 10}
        rejects(self, d, "النطاق")
        d = raw(); d["filters"]["entry_window_et"] = ["08:00", "15:00"]
        rejects(self, d, "نافذة الدخول")

    def test_bad_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{ bad")
        try:
            with self.assertRaises(C.ConfigError):
                C.load(f.name)
        finally:
            os.remove(f.name)

    def test_hash_changes_with_file(self):
        d = raw()
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "c.json")
            json.dump(d, open(p, "w")); h1 = C.file_hash(p)
            d2 = copy.deepcopy(d); d2["live"]["min_score"] = 66
            json.dump(d2, open(p, "w")); h2 = C.file_hash(p)
        self.assertNotEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
