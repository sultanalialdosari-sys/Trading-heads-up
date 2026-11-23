# -*- coding: utf-8 -*-
import os, sys
from datetime import date
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)
import intraday as I  # noqa: E402
import research as RS  # noqa: E402
from fake_market import FakeWorld, et, TODAY  # noqa: E402


def _walk(n=400, seed=1):
    r = np.random.default_rng(seed)
    c = 2 * np.exp(np.cumsum(r.normal(0, 0.04, n)))
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    o = c * (1 + r.normal(0, 0.01, n)); h = np.maximum(o, c) * 1.03; l = np.minimum(o, c) * 0.97
    v = r.integers(100_000, 1_000_000, n).astype(float)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def test_daily_features_no_lookahead():
    d = _walk()
    f1 = RS.daily_features(d, d)
    d2 = d.copy(); d2.iloc[300:, :4] *= 3          # تغيير المستقبل فقط
    f2 = RS.daily_features(d2, d2)
    feats = list(RS.BUCKETS) + RS.BINARY
    pd.testing.assert_frame_equal(f1.iloc[:299][feats], f2.iloc[:299][feats], check_dtype=False)
    # النتيجة تستخدم المستقبل (هذا مقصود) — وتختلف
    assert not np.allclose(f1.r5.iloc[290:299].values, f2.r5.iloc[290:299].values)


def test_split_flag_uses_raw_vs_adjusted():
    adj = _walk()
    raw = adj.copy(); raw.loc[raw.index[200]:, ["open", "high", "low", "close"]] *= 10   # تجميع عكسي 1:10
    f = RS.daily_features(raw, adj)
    assert f.split_60d.iloc[205] == 1 and f.split_60d.iloc[100] == 0


def test_intraday_study_on_fake_day():
    w = FakeWorld(); w.now = et("2026-11-21", 12, 0)
    c = I.AlpacaClient("k", "s", I.Config(), session=w); c.rl = I.RateLimiter(10**6)
    x = RS.intraday_study(c, [("EARL", date(2026, 11, 20), 1.7)])
    assert len(x) > 5 and {"score", "qualifies", "trade_R", "hit10_before5"} <= set(x.columns)
    # الصفوف قبل 12:00 ثم الحركة: صفقة واحدة على الأقل محسوبة
    assert x.trade_R.notna().any()
    s = RS.summarize_intraday(x)
    assert "qualifies" in s
