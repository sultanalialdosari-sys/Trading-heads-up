# -*- coding: utf-8 -*-
"""اختبارات طبقة البيانات نقطية الزمن على بيانات مصطنعة. الهدف: لا يمر أي سجل بعد لحظة القرار."""
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from pit import build as B  # noqa: E402
from pit import edgar as E  # noqa: E402
from pit import release as R  # noqa: E402
from pit.models import ET, UTC, Filing, SplitEvent, daily_known_ts, et_dt  # noqa: E402
from pit.store import LookAheadError, ParquetStore, derive_splits  # noqa: E402


def et(d, h, m=0):
    return et_dt(d, time(h, m))


def bdays(a: date, b: date):
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = bdays(date(2022, 12, 1), date(2023, 2, 28))


def make_store(root: str) -> ParquetStore:
    """ثلاثة أصول: GOOD يتداول طوال الفترة مع تجميع عكسي 1:10 في 2023-01-17،
    DEAD شُطب في 2023-01-10، NEW أُدرج في 2023-02-01."""
    rows = []
    split_day = date(2023, 1, 17)
    for d in DAYS:
        raw = 0.5 if d < split_day else 5.0              # الخام يقفز ×10 يوم التجميع
        rows.append(dict(symbol="GOOD", date=d, open=raw, high=raw * 1.1, low=raw * 0.9, close=raw,
                         volume=1e6 if d < split_day else 1e5, vwap=raw, trades=100,
                         factor=10.0 if d < split_day else 1.0))
        if d <= date(2023, 1, 10):
            rows.append(dict(symbol="DEAD", date=d, open=1, high=1, low=1, close=1, volume=1e5, vwap=1, trades=5, factor=1.0))
        if d >= date(2023, 2, 1):
            rows.append(dict(symbol="NEW", date=d, open=2, high=2, low=2, close=2, volume=1e5, vwap=2, trades=5, factor=1.0))
    df = pd.DataFrame(rows)
    os.makedirs(os.path.join(root, "daily"))
    for y, g in df.groupby(pd.to_datetime(df["date"]).dt.year):
        g.to_parquet(os.path.join(root, "daily", f"{y}.parquet"), index=False)
    derive_splits(df).to_parquet(os.path.join(root, "splits.parquet"), index=False)
    spans = df.groupby("symbol")["date"].agg(first_bar="min", last_bar="max")
    assets = pd.DataFrame([
        dict(asset_id="a1", symbol="GOOD", name="Good Inc", exchange="NASDAQ", status="active", symbol_shared=False, cik=111),
        dict(asset_id="a2", symbol="DEAD", name="Dead Co", exchange="NYSE", status="inactive", symbol_shared=False, cik=None),
        dict(asset_id="a3", symbol="NEW", name="New Co", exchange="AMEX", status="active", symbol_shared=False, cik=None),
    ]).merge(spans, left_on="symbol", right_index=True)
    assets["cik"] = pd.to_numeric(assets["cik"]).astype("Int64")
    assets.to_parquet(os.path.join(root, "assets.parquet"), index=False)
    os.makedirs(os.path.join(root, "sec"))
    sub = {"accessionNumber": ["A1", "A2", "A3"], "form": ["8-K", "424B5", "10-Q"], "items": ["3.01", "", ""],
           "filingDate": ["2023-01-05", "2023-01-09", "2023-01-12"],
           "acceptanceDateTime": ["2023-01-05T16:05:00.000Z", "2023-01-09T08:30:00.000Z", "2023-01-12T17:00:00.000Z"]}
    filings = E.parse_filings(111, [sub])
    pd.DataFrame([{"cik": f.cik, "accession": f.accession, "form": f.form, "items": ",".join(f.items), "filed": f.filed,
                   "accepted_raw": f.accepted_raw, "accepted_at": f.accepted_at} for f in filings]) \
        .to_parquet(os.path.join(root, "sec", "filings.parquet"), index=False)
    concept = {"units": {"shares": [{"val": 50_000_000, "end": "2023-01-10", "filed": "2023-01-12", "accn": "A3"}]}}
    shares = E.parse_shares(111, concept, filings)
    pd.DataFrame([{"cik": s.cik, "value": s.value, "period_end": s.period_end, "known_ts": s.known_ts,
                   "accession": s.accession} for s in shares]).to_parquet(os.path.join(root, "sec", "shares.parquet"), index=False)
    return ParquetStore(root)


class TestEdgarParsing(unittest.TestCase):
    def test_accepted_read_as_new_york(self):
        t = E.parse_accepted("2024-02-02T16:06:46.000Z", "2024-02-02")
        self.assertEqual(t, datetime(2024, 2, 2, 21, 6, 46, tzinfo=UTC))           # EST = UTC-5
        t = E.parse_accepted("2024-07-02T16:06:46.000Z", None)
        self.assertEqual(t, datetime(2024, 7, 2, 20, 6, 46, tzinfo=UTC))           # EDT = UTC-4

    def test_reading_is_never_earlier_than_utc_reading(self):
        raw = "2024-03-15T10:00:00.000Z"
        self.assertGreater(E.parse_accepted(raw, None), datetime(2024, 3, 15, 10, 0, tzinfo=UTC))

    def test_filed_only_is_end_of_edgar_day(self):
        self.assertEqual(E.parse_accepted(None, "2024-02-02"), et(date(2024, 2, 2), 22))

    def test_parse_filings_pages_and_dedupe(self):
        recent = {"accessionNumber": ["X2", "X1"], "form": ["8-K", "S-3"], "items": ["3.01,5.03", ""],
                  "filingDate": ["2023-02-01", "2023-01-01"],
                  "acceptanceDateTime": ["2023-02-01T09:00:00.000Z", "2023-01-01T12:00:00.000Z"]}
        old = {"accessionNumber": ["X1", "X0"], "form": ["S-3", "S-1"], "items": ["", ""],
               "filingDate": ["2023-01-01", "2020-05-05"], "acceptanceDateTime": ["2023-01-01T12:00:00.000Z", ""]}
        f = E.parse_filings(7, [recent, old])
        self.assertEqual([x.accession for x in f], ["X0", "X1", "X2"])
        self.assertEqual(f[-1].items, ("3.01", "5.03"))
        self.assertEqual(f[0].accepted_at, et(date(2020, 5, 5), 22))

    def test_first_session(self):
        f = E.parse_filings(1, [{"accessionNumber": ["a", "b"], "form": ["8-K", "8-K"], "items": ["", ""],
                                 "filingDate": ["2023-01-05", "2023-01-05"],
                                 "acceptanceDateTime": ["2023-01-05T16:05:00.000Z", "2023-01-05T10:00:00.000Z"]}])
        by = {x.accession: x for x in f}
        self.assertEqual(by["b"].first_session(DAYS), date(2023, 1, 5))
        self.assertEqual(by["a"].first_session(DAYS), date(2023, 1, 6))       # بعد الإغلاق ← اليوم التالي
        g = E.parse_filings(1, [{"accessionNumber": ["c"], "form": ["8-K"], "items": [""], "filingDate": ["2023-01-06"],
                                 "acceptanceDateTime": ["2023-01-06T18:00:00.000Z"]}])[0]
        self.assertEqual(g.first_session(DAYS), date(2023, 1, 9))             # جمعة مساءً ← الاثنين

    def test_shares_known_and_split_adjusted(self):
        fil = E.parse_filings(9, [{"accessionNumber": ["Q"], "form": ["10-Q"], "items": [""], "filingDate": ["2023-01-12"],
                                   "acceptanceDateTime": ["2023-01-12T17:00:00.000Z"]}])
        facts = E.parse_shares(9, {"units": {"shares": [{"val": 5e7, "end": "2023-01-10", "filed": "2023-01-12", "accn": "Q"},
                                                        {"val": 5e7, "end": "2023-01-10", "filed": "2023-01-12", "accn": "Q"}]}}, fil)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].known_ts, fil[0].known_ts)
        sp = [SplitEvent("S", date(2023, 1, 17), 0.1)]
        self.assertIsNone(E.shares_as_of(facts, sp, et(date(2023, 1, 12), 16)))   # قبل القبول
        s = E.shares_as_of(facts, sp, et(date(2023, 1, 13), 10))
        self.assertEqual((s.value, s.split_adjusted), (5e7, False))
        s = E.shares_as_of(facts, sp, et(date(2023, 1, 17), 10))
        self.assertEqual((round(s.value), s.split_adjusted), (5e6, True))

    def test_diagnose_hours(self):
        fs = [Filing(1, str(i), "8-K", (), date(2023, 1, 2), f"2023-01-02T{h:02d}:00:00.000Z", et(date(2023, 1, 2), h))
              for i, h in enumerate([6, 7, 8, 9] + list(range(10, 22)) * 20)]
        self.assertEqual(E.diagnose_hours(fs)["verdict"], "et_wallclock")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.s = make_store(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_daily_bar_known_only_after_close(self):
        d = date(2023, 1, 9)
        before = self.s.view(et(d, 15, 59)).daily(["GOOD"], start=date(2023, 1, 1))
        after = self.s.view(et(d, 16, 0)).daily(["GOOD"], start=date(2023, 1, 1))
        self.assertEqual(before["date"].max(), date(2023, 1, 6))
        self.assertEqual(after["date"].max(), d)

    def test_universe_no_survivorship_and_masked(self):
        u = {a.symbol: a for a in self.s.view(et(date(2023, 1, 5), 10)).universe()}
        self.assertIn("DEAD", u)                                  # سيُشطب لاحقاً لكنه يتداول الآن
        self.assertEqual(u["DEAD"].status, "active")             # الحالة الحالية لا تتسرب
        self.assertIsNone(u["DEAD"].last_bar)                    # ولا تاريخ الشطب
        self.assertNotIn("NEW", u)
        u2 = {a.symbol for a in self.s.view(et(date(2023, 1, 20), 10)).universe()}
        self.assertNotIn("DEAD", u2)
        u3 = {a.symbol for a in self.s.view(et(date(2023, 2, 1), 10)).universe()}
        self.assertNotIn("NEW", u3)                               # يوم الإدراج نفسه: لا تاريخ سابق
        self.assertIn("NEW", {a.symbol for a in self.s.view(et(date(2023, 2, 2), 10)).universe()})

    def test_raw_vs_adjusted(self):
        v = self.s.view(et(date(2023, 1, 31), 17))
        raw = v.daily(["GOOD"], start=date(2023, 1, 1)).set_index("date")
        adj = v.daily(["GOOD"], start=date(2023, 1, 1), adjusted=True).set_index("date")
        self.assertEqual(raw.loc[date(2023, 1, 13), "close"], 0.5)     # الخام: تحت 1$ قبل التجميع
        self.assertEqual(adj.loc[date(2023, 1, 13), "close"], 5.0)     # المعدل: متصل
        self.assertEqual(adj.loc[date(2023, 1, 13), "volume"], 1e5)

    def test_derived_split(self):
        sp = self.s.splits("GOOD")
        self.assertEqual(len(sp), 1)
        self.assertEqual(sp[0].ex_date, date(2023, 1, 17))
        self.assertAlmostEqual(sp[0].ratio, 0.1)

    def test_filings_respect_lag(self):
        d = date(2023, 1, 5)
        self.assertEqual(self.s.view(et(d, 16, 10)).filings(111), [])            # قُبل 16:05 + 10 دقائق تأخير
        self.assertEqual([f.accession for f in self.s.view(et(d, 16, 15)).filings(111)], ["A1"])
        v = self.s.view(et(date(2023, 1, 20), 10))
        self.assertEqual([f.accession for f in v.filings(111, items=["3.01"])], ["A1"])
        self.assertEqual([f.accession for f in v.filings(111, forms=["424B5"])], ["A2"])
        self.assertEqual(v.filings(None), [])

    def test_shares_through_store(self):
        self.assertIsNone(self.s.view(et(date(2023, 1, 12), 12)).shares_outstanding(111, "GOOD"))
        self.assertEqual(self.s.view(et(date(2023, 1, 13), 12)).shares_outstanding(111, "GOOD").value, 5e7)
        self.assertAlmostEqual(self.s.view(et(date(2023, 1, 18), 12)).shares_outstanding(111, "GOOD").value, 5e6)

    def test_intraday_cache_and_cutoff(self):
        d = date(2023, 1, 9)
        idx = pd.date_range(et(d, 9, 30), et(d, 15, 55), freq="5min")
        fake = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
        calls = []
        s = ParquetStore(self.root, intraday_fetcher=lambda sym, day, tf: calls.append(1) or fake)
        got = s.view(et(d, 10, 7)).intraday("GOOD", d, "5Min")
        self.assertEqual(got.index.max(), et(d, 10, 0))                        # شمعة 10:00 أُغلقت 10:05
        s.view(et(d, 12)).intraday("GOOD", d, "5Min")
        self.assertEqual(len(calls), 1)                                        # الثانية من الذاكرة
        with self.assertRaises(LookAheadError):
            s.view(et(d, 12)).intraday("GOOD", d + timedelta(days=1))

    def test_view_is_immutable(self):
        v = self.s.view(et(date(2023, 1, 9), 12))
        with self.assertRaises(AttributeError):
            v.as_of = et(date(2023, 2, 1), 12)
        with self.assertRaises(ValueError):
            self.s.view(datetime(2023, 1, 9, 12))                             # بلا منطقة زمنية

    def test_random_as_of_never_leaks(self):
        rnd = random.Random(3)
        for _ in range(60):
            d = rnd.choice(DAYS)
            t = et(d, rnd.randint(4, 21), rnd.choice([0, 15, 30, 59]))
            v = self.s.view(t)
            df = v.daily(None, start=date(2022, 12, 1))
            self.assertTrue(all(daily_known_ts(x) <= t for x in df["date"]))
            self.assertTrue(all(f.known_ts <= t for f in v.filings(111)))
            sh = v.shares_outstanding(111, "GOOD")
            self.assertTrue(sh is None or sh.known_ts <= t)
            for a in v.universe():
                self.assertTrue(a.first_bar < t.astimezone(ET).date())


class TestBuildHelpers(unittest.TestCase):
    def test_assets_frame(self):
        df = B.assets_frame([
            {"id": "1", "symbol": "ABC", "exchange": "NASDAQ", "status": "active"},
            {"id": "2", "symbol": "ABC", "exchange": "NYSE", "status": "inactive"},
            {"id": "3", "symbol": "XYZ.W", "exchange": "NASDAQ", "status": "active"},
            {"id": "4", "symbol": "OTCX", "exchange": "OTC", "status": "active"},
        ])
        self.assertEqual(sorted(df["asset_id"]), ["1", "2"])
        self.assertTrue(df["symbol_shared"].all())

    def test_bars_and_factor(self):
        idx = pd.DatetimeIndex([pd.Timestamp("2023-01-12 05:00", tz="UTC"), pd.Timestamp("2023-01-13 05:00", tz="UTC")])
        raw = {"S": pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [0.5, 5.0], "volume": [10, 1],
                                  "trades": [1, 1], "bar_vwap": [0.5, 5.0]}, index=idx)}
        adj = {"S": pd.DataFrame({"open": [1, 1], "high": [1, 1], "low": [1, 1], "close": [5.0, 5.0], "volume": [1, 1],
                                  "trades": [1, 1], "bar_vwap": [5.0, 5.0]}, index=idx)}
        m = B.merge_factor(B.bars_frame(raw), B.bars_frame(adj))
        self.assertEqual(list(m["date"]), [date(2023, 1, 12), date(2023, 1, 13)])
        self.assertEqual(list(m["factor"]), [10.0, 1.0])

    def test_write_years(self):
        out, tmp = tempfile.mkdtemp(), tempfile.mkdtemp()
        try:
            rows = [dict(symbol="S", date=d, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0, vwap=1.0, trades=1,
                         factor=2.0 if d < date(2023, 1, 3) else 1.0) for d in bdays(date(2022, 12, 28), date(2023, 1, 5))]
            pd.DataFrame(rows).to_parquet(os.path.join(tmp, "part_000000.parquet"), index=False)
            spans = B.write_years(tmp, out)
            self.assertEqual(sorted(os.listdir(os.path.join(out, "daily"))), ["2022.parquet", "2023.parquet"])
            self.assertEqual(spans.loc["S", "first_bar"], date(2022, 12, 28))
            sp = pd.read_parquet(os.path.join(out, "splits.parquet"))
            self.assertAlmostEqual(float(sp["ratio"].iloc[0]), 0.5)
        finally:
            shutil.rmtree(out); shutil.rmtree(tmp)

    def test_release_names(self):
        for rel in ("daily/2016.parquet", "sec/filings.parquet", "assets.parquet", "manifest.json"):
            self.assertEqual(R.unflat(R.flat(rel)), rel)


if __name__ == "__main__":
    unittest.main()
