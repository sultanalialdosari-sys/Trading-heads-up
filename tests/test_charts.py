# -*- coding: utf-8 -*-
"""اختبارات رسوم TradingView: تجميع الشموع، الحفظ والدمج، إكمال الأيام، وبيانات الصفحة."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import intraday as I  # noqa: E402
from hu import charts as CH  # noqa: E402
from hu.store import Store  # noqa: E402

DAY = "2026-11-24"


def minute_bars(start_et: str, n: int, p0=1.0) -> pd.DataFrame:
    idx = pd.date_range(pd.Timestamp(f"{DAY} {start_et}", tz=I.ET), periods=n, freq="1min").tz_convert("UTC")
    px = [p0 + 0.01 * i for i in range(n)]
    return pd.DataFrame({"open": px, "high": [p + 0.02 for p in px], "low": [p - 0.02 for p in px],
                         "close": [p + 0.005 for p in px], "volume": [100.0] * n}, index=idx)


class FakeClient:
    def __init__(self):
        self.calls = []

    def bars(self, syms, tf, start, end, feed=None):
        self.calls.append((tuple(syms), tf, start, end, feed))
        return {s: minute_bars("09:30", 30).resample("5min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}) for s in syms}


class TestCharts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Store(os.path.join(self.tmp, "h.db"))
        self.db.insert("alerts", {"day": DAY, "symbol": "ABC", "alert_utc": "2026-11-24T15:07:00+00:00", "kind": "EARLY",
                                  "score": 72, "trigger": 1.30, "invalidation": 1.05, "state": "OPEN"})
        self.db.insert("trades", {"day": DAY, "symbol": "ABC", "kind": "A", "entry": 1.20, "stop": 1.05, "target": 1.50,
                                  "entry_utc": "2026-11-24T15:07:00+00:00", "exit_utc": "2026-11-24T15:21:00+00:00",
                                  "pnl": -75.0, "r_mult": -1.0, "status": "STOP"})
        self.db.insert("trades", {"day": DAY, "symbol": "RND", "kind": "BASE", "entry": 2.0, "stop": 1.9, "status": "OPEN"})

    def test_resample_to_5min(self):
        rows = CH.to_rows(minute_bars("10:00", 12))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][0] - rows[0][0], 300)
        o, h, l, c, v = rows[0][1:]
        self.assertAlmostEqual(o, 1.0); self.assertAlmostEqual(c, 1.045); self.assertEqual(v, 500.0)
        self.assertEqual(CH.to_rows(None), [])

    def test_merge_prefers_new(self):
        old = [[1, 1, 1, 1, 1, 1], [2, 1, 1, 1, 1, 1]]
        new = [[2, 9, 9, 9, 9, 9], [3, 1, 1, 1, 1, 1]]
        self.assertEqual(CH.merge_rows(old, new), [[1, 1, 1, 1, 1, 1], [2, 9, 9, 9, 9, 9], [3, 1, 1, 1, 1, 1]])

    def test_save_merges_then_replaces(self):
        CH.save(self.db, DAY, "ABC", minute_bars("10:00", 10), "scan")
        CH.save(self.db, DAY, "ABC", minute_bars("10:10", 10), "follow")
        n = len(json.loads(self.db.one("SELECT bars FROM chart_bars WHERE day=? AND symbol='ABC'", (DAY,))["bars"]))
        self.assertEqual(n, 4)
        CH.save(self.db, DAY, "ABC", minute_bars("09:30", 5), "sip", replace=True)
        r = self.db.one("SELECT bars, source FROM chart_bars WHERE day=? AND symbol='ABC'", (DAY,))
        self.assertEqual((len(json.loads(r["bars"])), r["source"]), (1, "sip"))

    def test_chart_symbols_skip_baseline(self):
        self.assertEqual(CH.chart_symbols(self.db, DAY), ["ABC"])

    def test_finalize_and_backfill(self):
        c = FakeClient()
        now = datetime(2026, 11, 25, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(CH.backfill(c, self.db, now), {DAY: 1})
        syms, tf, start, end, feed = c.calls[0]
        self.assertEqual((syms, tf, feed), (("ABC",), "5Min", "sip"))
        self.assertEqual(start.astimezone(I.ET).hour, 4)
        self.assertLessEqual(end, now - timedelta(minutes=16))
        self.assertEqual(CH.backfill(c, self.db, now), {})             # اكتمل: لا طلب ثانٍ
        self.assertEqual(len(c.calls), 1)
        # اليوم الجاري لا يُكمل بالإكمال الخلفي
        self.db.insert("alerts", {"day": "2026-11-25", "symbol": "XYZ", "alert_utc": "2026-11-25T14:00:00+00:00"})
        self.assertNotIn("2026-11-25", CH.backfill(c, self.db, now))

    def test_items_and_html(self):
        CH.save(self.db, DAY, "ABC", minute_bars("09:30", 120), "sip", replace=True)
        CH.save(self.db, DAY, "RND", minute_bars("09:30", 60), "sip", replace=True)
        items = CH.build_items(self.db, self.db.q("SELECT * FROM alerts"), self.db.q("SELECT * FROM trades"))
        self.assertEqual([i["sym"] for i in items], ["ABC"])               # صفقة المقارنة بلا رسم
        it = items[0]
        titles = [ln["t"] for ln in it["lines"]]
        self.assertEqual(titles, ["التفعيل", "الإبطال", "دخول A", "هدف A"])   # الوقف = الإبطال فلا يتكرر
        self.assertEqual(it["pnl"], -75.0)
        utc0 = CH.to_rows(minute_bars("09:30", 5))[0][0]
        self.assertEqual(it["bars"][0][0], utc0 + CH.RIYADH_OFFSET)
        marks = {m["x"]: m["t"] - CH.RIYADH_OFFSET for m in it["marks"]}
        self.assertEqual(marks["تنبيه"], int(datetime(2026, 11, 24, 15, 5, tzinfo=timezone.utc).timestamp()))
        self.assertIn("A -1.0R", marks)
        page = CH.section_html(items)
        self.assertIn('id="chdata"', page)
        self.assertIn("tradingview.com", page)
        data = json.loads(page.split('id="chdata">')[1].split("</script>")[0])
        self.assertEqual(data[0]["id"], f"{DAY}_ABC")
        self.assertIn("لا رسوم بعد", CH.section_html([]))

    def test_vendor_file_present(self):
        p = os.path.join(os.path.dirname(HERE), "docs", CH.LIB)
        self.assertTrue(os.path.exists(p))
        self.assertIn("Lightweight Charts", open(p, encoding="utf-8").read(400))


if __name__ == "__main__":
    unittest.main()


class TestEquity(unittest.TestCase):
    def test_equity_series_and_detail(self):
        from hu import equity as EQ
        tr = [
            {"kind": "A", "symbol": "AAA", "pnl": 50.0, "r_mult": 1.0, "exit_utc": "2026-11-24T15:00:00+00:00", "status": "TARGET"},
            {"kind": "BASE", "symbol": "ZZZ", "pnl": -20.0, "r_mult": -1.0, "exit_utc": "2026-11-24T15:00:00+00:00", "status": "STOP"},
            {"kind": "PRE", "symbol": "PPP", "pnl": -10.0, "r_mult": None, "exit_utc": "2026-11-24T16:00:00+00:00", "status": "TIME"},
            {"kind": "A", "symbol": "OPEN", "pnl": None, "exit_utc": None},
        ]
        d = EQ.build(tr)
        keys = [s["k"] for s in d["series"]]
        self.assertEqual(keys, ["A", "EXT", "BASE"])                       # B غير مستخدم فلا يظهر
        a = next(s for s in d["series"] if s["k"] == "A")["d"]
        self.assertEqual([p[1] for p in a], [0.0, 50.0, 50.0])              # خط درجي يبدأ من الصفر
        t0 = int(datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc).timestamp()) + CH.RIYADH_OFFSET
        self.assertEqual(a[1][0], t0)
        self.assertEqual(sorted(x["s"] for x in d["detail"][t0]), ["AAA", "ZZZ"])
        self.assertEqual(d["n"], 3)
        html = EQ.section(tr)
        self.assertIn('id=\'eqdata\'', html)
        self.assertIn("eqwrap", html)
        self.assertIn("بعد أول صفقة", EQ.section([]))
