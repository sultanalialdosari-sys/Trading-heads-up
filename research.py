# -*- coding: utf-8 -*-
"""دراسة تاريخية: ما الذي سبق ارتفاع الأسهم الأمريكية تحت 5$؟ (2016 → الآن)

تعمل على GitHub Actions (research.yml) بمفاتيح Alpaca من الأسرار. لا تُرسل أي أمر.

لماذا 2016؟ بيانات Alpaca التاريخية المجانية تبدأ من 2016 (لا يوجد مصدر مجاني موثوق لـ20 سنة بدقة الدقائق).

الجزء 1 — يومي: كل سهم مدرج أو مشطوب (لتقليل انحياز «الناجين») كان سعره الحقيقي (غير المعدّل) بين 0.20$ و5$
           وحجمه الدولاري ≥ 300 ألف. مؤشرات تُحسب حتى إغلاق اليوم فقط، والنتيجة من الأيام التالية فقط.
الجزء 2 — داخل اليوم (شموع 5 دقائق): عينة عشوائية من أيام أسهم سائلة تحت 5$ (الاختيار بمعلومات اليوم السابق فقط،
           حتى لا ننتقي أياماً نعرف أنها تحركت). نطبّق قواعد النظام الحالية كل 15 دقيقة، ونحاكي صفقته الفعلية.
التقسيم: تدريب حتى 2022، اختبار من 2023 — ولا يُعتمد إلا ما يصمد في الاختبار.
"""
import json
import math
import os
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import intraday as I  # noqa: E402
from hu import rules as RL  # noqa: E402

OUT = os.environ.get("RESEARCH_OUT", os.path.join(BASE, "research_out"))
START = datetime(2016, 1, 1, tzinfo=timezone.utc)
TRAIN_END = 2022           # تدريب ≤ 2022 • اختبار ≥ 2023
N_EVENTS = int(os.environ.get("RESEARCH_EVENTS", "6000"))
MODE = os.environ.get("RESEARCH_MODE", "full")          # full | intraday (يعيد أحداث الجولة الأولى مع طرق خروج وأخبار)
WITH_NEWS = os.environ.get("RESEARCH_NEWS", "1") == "1"
EXCH = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}
LOG = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


# ====================================================================== البيانات
def universe(c: I.AlpacaClient) -> list:
    syms = set()
    for status in ("active", "inactive"):
        try:
            assets = c._get(I.PAPER_BASE, "/v2/assets", {"status": status, "asset_class": "us_equity"})
        except I.ApiError as e:
            log("assets", status, e.message)
            continue
        for a in assets:
            s = a.get("symbol") or ""
            if a.get("exchange") in EXCH and s.isalpha() and len(s) <= 5:
                syms.add(s)
        log(f"الأسهم ({status}): {len(assets)}")
    return sorted(syms)


def fetch_daily(c, syms, adj, end) -> dict:
    out = {}
    for i in range(0, len(syms), 100):
        part = syms[i:i + 100]
        for attempt in range(3):
            try:
                b = c.bars(part, "1Day", START, end, feed="sip", adjustment=adj)
                break
            except I.ApiError as e:
                log("daily", adj, i, e.message)
                time.sleep(5)
                b = {}
        for s, df in b.items():
            if len(df) >= 60:
                out[s] = df[["open", "high", "low", "close", "volume"]].astype("float64")
        if i % 2000 == 0:
            log(f"  يومي {adj}: {i}/{len(syms)} • طلبات {c.call_count}")
    return out


# ====================================================================== الجزء 1: يومي
def rsi(x, n=14):
    d = x.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def daily_features(raw: pd.DataFrame, adj: pd.DataFrame) -> pd.DataFrame:
    d = adj.copy()
    d["raw_close"] = raw["close"].reindex(d.index)
    d["raw_vol"] = raw["volume"].reindex(d.index)
    c, h, l, o, v = d.close, d.high, d.low, d.open, d.volume
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    f = pd.DataFrame(index=d.index)
    f["dollar_vol"] = d.raw_close * d.raw_vol
    f["raw_close"] = d.raw_close
    f["rvol"] = v / v.shift(1).rolling(20).mean()
    f["vol_dryup"] = v.shift(1).rolling(5).mean() / v.shift(6).rolling(20).mean()
    f["ret1"] = c / pc - 1
    f["ret5"] = c / c.shift(5) - 1
    f["ret20"] = c / c.shift(20) - 1
    f["gap"] = o / pc - 1
    f["close_pos"] = ((c - l) / (h - l)).where(h > l)
    f["dist_hi20"] = c / h.rolling(20).max() - 1
    f["new_hi20"] = (c >= h.shift(1).rolling(20).max()).astype(float)
    f["dist_hi252"] = c / h.rolling(252, min_periods=60).max() - 1
    f["range10_atr"] = (h.rolling(10).max() - l.rolling(10).min()) / atr
    rng = h - l
    f["nr7"] = (rng <= rng.rolling(7).min()).astype(float)
    f["inside"] = ((h <= h.shift(1)) & (l >= l.shift(1))).astype(float)
    lows = [l.shift(k).rolling(3).min() for k in (6, 3, 0)]
    f["higher_lows"] = ((lows[0] < lows[1]) & (lows[1] < lows[2])).astype(float)
    sma20, sma50 = c.rolling(20).mean(), c.rolling(50).mean()
    f["above_sma20"] = (c > sma20).astype(float)
    f["above_sma50"] = (c > sma50).astype(float)
    f["sma20_gt_50"] = (sma20 > sma50).astype(float)
    f["rsi"] = rsi(c)
    m = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = m - m.ewm(span=9, adjust=False).mean()
    f["macd_rising"] = ((hist > hist.shift(1)) & (hist.shift(1) > hist.shift(2))).astype(float)
    bb = c.rolling(20).std() / sma20
    f["bb_width_pct"] = bb.rolling(120, min_periods=60).rank(pct=True)
    obv = (np.sign(c.diff()).fillna(0) * v).cumsum()
    f["obv_up10"] = (obv > obv.shift(10)).astype(float)
    f["atr_pct"] = atr / c
    # انعكاس سهم مقسوم: نسبة السعر الحقيقي للمعدّل تتغير = حدث تجزئة/تجميع خلال 60 يوماً
    ratio = (d.raw_close / c).round(3)
    f["split_60d"] = (ratio != ratio.shift(60)).astype(float)
    # النتائج (من الأيام التالية فقط)
    nh = pd.concat([h.shift(-k) for k in range(1, 6)], axis=1)
    nl = pd.concat([l.shift(-k) for k in range(1, 6)], axis=1)
    f["y_up20_next"] = (h.shift(-1) >= c * 1.20).astype(float)
    f["y_up30_5d"] = (nh.max(axis=1) >= c * 1.30).astype(float)
    f["r1"] = c.shift(-1) / c - 1
    f["r5"] = c.shift(-5) / c - 1
    f["mae5"] = nl.min(axis=1) / c - 1
    f.loc[f.index[-5:], ["y_up30_5d", "r5", "mae5"]] = np.nan
    f.loc[f.index[-1:], ["y_up20_next", "r1"]] = np.nan
    return f


BUCKETS = {
    "rvol": [0, 1, 1.5, 2, 3, 5, 10, np.inf],
    "vol_dryup": [0, 0.5, 0.8, 1.2, 2, np.inf],
    "ret1": [-1, -0.1, -0.03, 0.03, 0.1, 0.2, 0.4, np.inf],
    "ret5": [-1, -0.2, -0.05, 0.05, 0.2, 0.5, np.inf],
    "ret20": [-1, -0.3, -0.1, 0.1, 0.3, 1, np.inf],
    "gap": [-1, -0.05, -0.01, 0.01, 0.05, 0.15, np.inf],
    "close_pos": [-0.01, 0.2, 0.5, 0.8, 1.01],
    "dist_hi20": [-1, -0.3, -0.15, -0.05, -0.0001, 0.01],
    "dist_hi252": [-1, -0.8, -0.5, -0.2, -0.05, 0.01],
    "range10_atr": [0, 2, 3, 4, 6, np.inf],
    "rsi": [0, 30, 45, 55, 70, 80, 101],
    "bb_width_pct": [-0.01, 0.1, 0.25, 0.75, 1.01],
    "atr_pct": [0, 0.05, 0.1, 0.2, np.inf],
    "raw_close": [0.2, 0.5, 1, 2, 3, 5.0001],
    "dollar_vol": [3e5, 1e6, 5e6, 2e7, np.inf],
}
BINARY = ["new_hi20", "nr7", "inside", "higher_lows", "above_sma20", "above_sma50", "sma20_gt_50", "macd_rising",
          "obv_up10", "split_60d"]


def lift_tables(df: pd.DataFrame, target: str) -> list:
    out = []
    tr, te = df[df.year <= TRAIN_END], df[df.year > TRAIN_END]
    b_tr, b_te = tr[target].mean(), te[target].mean()
    def row(name, lab, mtr, mte):
        n1, n2 = int(mtr.sum()), int(mte.sum())
        if n1 < 300 or n2 < 100:
            return
        p1, p2 = tr.loc[mtr, target].mean(), te.loc[mte, target].mean()
        out.append({"feature": name, "bucket": lab, "n_train": n1, "n_test": n2,
                    "rate_train": round(p1, 4), "rate_test": round(p2, 4),
                    "lift_train": round(p1 / b_tr, 2) if b_tr else None, "lift_test": round(p2 / b_te, 2) if b_te else None,
                    "r1_test_mean": round(float(te.loc[mte, "r1"].mean()), 4),
                    "r1_test_median": round(float(te.loc[mte, "r1"].median()), 4)})
    for f, edges in BUCKETS.items():
        cut_tr, cut_te = pd.cut(tr[f], edges), pd.cut(te[f], edges)
        for iv in cut_tr.cat.categories:
            row(f, str(iv), cut_tr == iv, cut_te == iv)
    for f in BINARY:
        row(f, "1", tr[f] == 1, te[f] == 1)
        row(f, "0", tr[f] == 0, te[f] == 0)
    return out


def daily_study(c, end) -> dict:
    syms = universe(c)
    log(f"الكون: {len(syms)} سهماً (مدرج + مشطوب)")
    raw = fetch_daily(c, syms, "raw", end)
    log(f"يومي raw: {len(raw)} سهماً")
    pen = [s for s, d in raw.items() if ((d.close <= 5) & (d.close >= 0.2) & (d.close * d.volume >= 3e5)).any()]
    log(f"أسهم كانت تحت 5$ بسيولة: {len(pen)}")
    adj = fetch_daily(c, pen, "split", end)
    frames = []
    for s in pen:
        if s not in adj:
            continue
        try:
            f = daily_features(raw[s], adj[s])
        except Exception:
            continue
        f = f[(f.raw_close >= 0.2) & (f.raw_close <= 5) & (f.dollar_vol >= 3e5)]
        if len(f):
            f["symbol"] = s
            frames.append(f.astype({k: "float32" for k in f.columns if k != "symbol"}))
    df = pd.concat(frames)
    df["year"] = df.index.year
    df = df.dropna(subset=["rvol", "r1"])
    log(f"صفوف يومية: {len(df):,} • أسهم {df.symbol.nunique()}")
    res = {"rows": int(len(df)), "symbols": int(df.symbol.nunique()),
           "base": {t: {"train": round(float(df[df.year <= TRAIN_END][t].mean()), 4),
                        "test": round(float(df[df.year > TRAIN_END][t].mean()), 4)} for t in ("y_up20_next", "y_up30_5d")},
           "r1_mean": {"train": round(float(df[df.year <= TRAIN_END].r1.mean()), 4), "test": round(float(df[df.year > TRAIN_END].r1.mean()), 4)},
           "lift_up20_next": lift_tables(df, "y_up20_next"),
           "lift_up30_5d": lift_tables(df, "y_up30_5d")}
    # نموذج (للمقارنة فقط): هل مجموع المؤشرات يتنبأ أفضل من الصدفة على سنوات لم يرها؟
    try:
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.inspection import permutation_importance
        from sklearn.metrics import roc_auc_score
        feats = list(BUCKETS) + BINARY
        tr, te = df[df.year <= TRAIN_END], df[df.year > TRAIN_END]
        for target in ("y_up20_next", "y_up30_5d"):
            a, b = tr.dropna(subset=[target]), te.dropna(subset=[target])
            if len(a) > 400_000:
                a = a.sample(400_000, random_state=1)
            m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, max_leaf_nodes=31,
                                               min_samples_leaf=200, random_state=1)
            m.fit(a[feats], a[target])
            p = m.predict_proba(b[feats])[:, 1]
            auc = roc_auc_score(b[target], p)
            bb = b.assign(p=p).sort_values("p", ascending=False)
            top = {f"top_{q}%": round(float(bb.head(max(1, int(len(bb) * q / 100)))[target].mean()), 4) for q in (1, 5, 10)}
            top_r1 = {f"top_{q}%_r1": round(float(bb.head(max(1, int(len(bb) * q / 100))).r1.mean()), 4) for q in (1, 5, 10)}
            sub = b.sample(min(len(b), 60_000), random_state=1)
            imp = permutation_importance(m, sub[feats], sub[target], n_repeats=3, random_state=1, scoring="roc_auc")
            res[f"model_{target}"] = {"auc_test": round(float(auc), 4), "base_test": round(float(b[target].mean()), 4),
                                      **top, **top_r1,
                                      "importance": sorted([(f, round(float(v), 4)) for f, v in zip(feats, imp.importances_mean)],
                                                           key=lambda x: -x[1])[:12]}
            log(f"نموذج {target}: AUC اختبار {auc:.3f} • أعلى 1% {top['top_1%']:.3f} مقابل الأساس {b[target].mean():.3f}")
    except Exception as e:
        log("model:", type(e).__name__, e)
    return res, df


# ====================================================================== الجزء 2: داخل اليوم
def pick_events(df: pd.DataFrame, n: int) -> list:
    """أيام عشوائية: السهم كان تحت 5$ وسائلاً في الأيام السابقة (لا نعرف ما سيحدث في يوم الحدث)."""
    d = df.copy()
    d["date"] = d.index.date
    # معيار الاختيار من اليوم السابق فقط
    d = d.sort_values(["symbol", "date"])
    d["prev_close"] = d.groupby("symbol").raw_close.shift(1)
    d["prev_dv20"] = d.groupby("symbol").dollar_vol.transform(lambda x: x.shift(1).rolling(20, min_periods=10).mean())
    pool = d[(d.year >= 2018) & d.prev_close.between(0.3, 5) & (d.prev_dv20 >= 2e5)]
    per_year = max(1, n // max(1, pool.year.nunique()))
    ev = []
    for y, g in pool.groupby("year"):
        g = g.sample(min(len(g), per_year), random_state=y)
        ev += [(r.symbol, r.date, float(r.prev_close)) for r in g.itertuples()]
    random.Random(7).shuffle(ev)
    return ev[:n]


def sim_trade(day: pd.DataFrame, j: int, entry: float, stop: float, target: float):
    """صفقة A: الدخول عند إغلاق الشمعة j، وقف/هدف من الشموع التالية فقط، الوقف أولاً عند التعارض."""
    for k in range(j + 1, len(day)):
        r = day.iloc[k]
        if (day.index[k].hour, day.index[k].minute) >= (15, 55):
            return float(r.open), "TIME"
        if r.open <= stop:
            return float(r.open), "STOP"
        if r.low <= stop:
            return stop, "STOP"
        if r.high >= target:
            return max(float(r.open), target), "TARGET"
    return float(day.iloc[-1].close), "TIME"


EXITS = {"p5_10": (0.05, 0.10), "p8_16": (0.08, 0.16), "p10_30": (0.10, 0.30)}


def bracket(day: pd.DataFrame, j: int, entry: float, stop: float, target: float) -> float:
    """عائد % لصفقة بوقف وهدف ثابتين (الوقف أولاً عند التعارض، فجوة الافتتاح بسعرها، خروج 15:55)."""
    ex, _ = sim_trade(day, j, entry, stop, target)
    return ex / entry - 1


def news_items(c, sym: str, day) -> list:
    """أخبار السهم من إغلاق أمس حتى نهاية الجلسة (لكل صف نستخدم فقط ما نُشر قبل وقت القرار)."""
    d0 = datetime(day.year, day.month, day.day, tzinfo=I.ET)
    params = {"symbols": sym, "start": I.iso((d0 - timedelta(hours=8)).astimezone(I.UTC)),
              "end": I.iso((d0 + timedelta(hours=16)).astimezone(I.UTC)), "limit": 50, "sort": "asc"}
    try:
        raw = c._get(I.DATA_BASE, "/v1beta1/news", params).get("news") or []
    except I.ApiError:
        return None
    from hu import news as N
    out = []
    for it in I.normalize_news(raw) or []:
        if it.get("time_utc"):
            out.append((it["time_utc"], N.classify(it), it["headline"][:120]))
    return out


def first_touch(day: pd.DataFrame, j: int, px: float, up: float, dn: float) -> float:
    for k in range(j + 1, len(day)):
        r = day.iloc[k]
        if r.low <= px * (1 - dn):
            return 0.0
        if r.high >= px * (1 + up):
            return 1.0
    return 0.0


def intraday_study(c, events) -> pd.DataFrame:
    cfg = dict(RL.DEFAULTS)
    icfg = I.Config()
    rows = []
    t0 = time.time()
    for n, (sym, day, prev_close) in enumerate(events):
        try:
            d0 = datetime(day.year, day.month, day.day, tzinfo=I.ET)
            b = c.bars([sym], "5Min", (d0 - timedelta(days=16)).astimezone(I.UTC),
                       (d0 + timedelta(hours=20)).astimezone(I.UTC), feed="sip", adjustment="split").get(sym)
            if b is None or b.empty:
                continue
            d5 = I.regular_session(I.to_et(b))
            if len(d5) < 300 or d5.index[-1].date() != day:
                continue
            d5 = I.add_indicators(d5, icfg)
            dates = sorted(set(d5.index.date))
            if len(dates) < 8:
                continue
            idx_day = np.where(d5.index.date == day)[0]
            day_df = d5.iloc[idx_day]
            nitems = news_items(c, sym, day) if WITH_NEWS else None
            prev_c = float(d5[d5.index.date == dates[-2]].close.iloc[-1])
            for j_local in range(len(day_df)):
                ts = day_df.index[j_local]
                if ts.minute % 15 != 10 or not ((10, 0) <= (ts.hour, ts.minute) <= (15, 0)):
                    continue            # تقييم عند إغلاق شمعة xx:10 → وقت القرار xx:15
                i = idx_day[j_local]
                st = RL.evaluate_setup(sym, d5.iloc[:i + 1], cfg, None, prev_c)
                st.parts["الأخبار"] = 3          # لا أخبار تاريخية هنا: نفس قيمة «لا خبر» في النظام الحي
                st.score = int(max(0, min(100, sum(st.parts.values()))))
                px = float(day_df.close.iloc[j_local])
                row = {"symbol": sym, "date": str(day), "year": day.year, "tod": ts.hour * 60 + ts.minute + 5,
                       "price": px, "score": st.score, "stage": st.stage, "gates": "|".join(st.gates_failed)[:200],
                       "n_gates": len(st.gates_failed), "qualifies": int(st.stage == "EARLY" and not st.gates_failed and st.score >= cfg["min_score"]),
                       "rvol": st.rvol, "rvol_rising": int(bool(st.rvol and st.rvol_1h_ago and st.rvol > st.rvol_1h_ago * 1.1)),
                       "spike": st.spike, "range_pct": st.range_pct, "range_atr": st.range_atr, "higher_lows": int(st.higher_lows),
                       "above_vwap": int(st.above_vwap), "ema_ok": int(st.ema_ok), "macd_rising": int(st.macd_rising),
                       "rsi": st.rsi, "dist_res_pct": st.dist_res_pct, "day_change_pct": st.day_change_pct,
                       "dollar_volume": st.dollar_volume}
                row["hit10_before5"] = first_touch(day_df, j_local, px, 0.10, 0.05)
                ent = px * 1.005
                for name, (sl, tp) in EXITS.items():
                    row[f"x_{name}"] = bracket(day_df, j_local, ent, ent * (1 - sl), ent * (1 + tp))
                atr5 = float(d5.atr.iloc[i]) if not math.isnan(d5.atr.iloc[i]) else None
                if atr5:
                    row["x_atr3"] = bracket(day_df, j_local, ent, ent - 3 * atr5, ent + 6 * atr5)
                    row["atr5_pct"] = atr5 / px
                ex_c, _ = sim_trade(day_df, j_local, ent, -1.0, 1e9)
                row["x_close"] = ex_c / ent - 1
                if nitems is not None:
                    t_dec = ts.to_pydatetime() + timedelta(minutes=5)
                    kinds = [k for t, k, _ in nitems if t <= t_dec]
                    row["news_n"] = len(kinds)
                    row["news_best"] = ("RISK" if "RISK" in kinds else "MATERIAL" if "MATERIAL" in kinds else
                                        "PROMO" if "PROMO" in kinds else "OTHER" if kinds else "NONE")
                rest = day_df.iloc[j_local + 1:]
                row["ret_close"] = float(rest.close.iloc[-1] / px - 1) if len(rest) else 0.0
                row["mfe"] = float(rest.high.max() / px - 1) if len(rest) else 0.0
                row["mae"] = float(rest.low.min() / px - 1) if len(rest) else 0.0
                if st.invalidation and st.invalidation < px:
                    entry = px * 1.005
                    risk = entry - st.invalidation
                    ex, why = sim_trade(day_df, j_local, entry, st.invalidation, entry + 2 * risk)
                    row["trade_R"] = (ex - entry) / risk
                    row["trade_pct"] = ex / entry - 1
                    row["stop_pct"] = risk / entry
                    row["exit"] = why
                rows.append(row)
        except I.ApiError as e:
            log("event", sym, day, e.message)
        except Exception as e:
            log("event-err", sym, day, type(e).__name__, str(e)[:120])
        if n % 500 == 0:
            log(f"  داخل اليوم: {n}/{len(events)} • صفوف {len(rows)} • {time.time() - t0:.0f} ث")
    return pd.DataFrame(rows)


def summarize_intraday(x: pd.DataFrame) -> dict:
    if x.empty:
        return {}
    tr, te = x[x.year <= TRAIN_END], x[x.year > TRAIN_END]
    def stats(g):
        t = g.dropna(subset=["trade_R"])
        return {"n": int(len(g)), "hit10_before5": round(float(g.hit10_before5.mean()), 4) if len(g) else None,
                "ret_close_mean": round(float(g.ret_close.mean()), 4) if len(g) else None,
                "trades": int(len(t)), "avg_R": round(float(t.trade_R.mean()), 3) if len(t) else None,
                "win_rate": round(float((t.trade_R > 0).mean()), 3) if len(t) else None,
                "avg_pct": round(float(t.trade_pct.mean()), 4) if len(t) else None}
    res = {"all": {"train": stats(tr), "test": stats(te)},
           "qualifies": {"train": stats(tr[tr.qualifies == 1]), "test": stats(te[te.qualifies == 1])}}
    for lo, hi in ((0, 40), (40, 55), (55, 65), (65, 75), (75, 101)):
        res[f"score_{lo}_{hi}"] = {"train": stats(tr[(tr.score >= lo) & (tr.score < hi)]), "test": stats(te[(te.score >= lo) & (te.score < hi)])}
    return res


# ====================================================================== التشغيل
def main():
    os.makedirs(OUT, exist_ok=True)
    key, sec = os.environ.get("ALPACA_API_KEY", "").strip(), os.environ.get("ALPACA_API_SECRET", "").strip()
    # أبطأ من الحد (200/دقيقة) حتى لا يزاحم المشغّل الحي الذي يستخدم نفس المفتاح
    c = I.AlpacaClient(key, sec, I.Config(max_calls_per_min=int(os.environ.get("RESEARCH_RPM", "100"))))
    c.clock()
    end = c.now_utc() - timedelta(days=1)
    t0 = time.time()
    summary = {"generated_utc": datetime.now(timezone.utc).isoformat(), "start": START.date().isoformat(),
               "train_end_year": TRAIN_END}
    try:
        if MODE == "intraday":
            prev = pd.read_csv(os.path.join(OUT, "intraday_rows.csv.gz"), usecols=["symbol", "date"]).drop_duplicates()
            events = [(r.symbol, datetime.strptime(r.date, "%Y-%m-%d").date(), 0.0) for r in prev.itertuples()]
            log(f"إعادة الأحداث السابقة مع طرق خروج وأخبار: {len(events)}")
            x = intraday_study(c, events)
            x.to_csv(os.path.join(OUT, "intraday_rows_v2.csv.gz"), index=False, compression="gzip")
            summary["intraday_v2"] = summarize_intraday(x)
            summary["intraday_rows_v2"] = int(len(x))
            raise StopIteration
        dres, ddf = daily_study(c, end)
        summary["daily"] = dres
        log(f"الجزء 1 انتهى في {(time.time() - t0) / 60:.0f} د")
        events = pick_events(ddf, N_EVENTS)
        del ddf
        log(f"أحداث داخل اليوم: {len(events)}")
        x = intraday_study(c, events)
        x.to_csv(os.path.join(OUT, "intraday_rows.csv.gz"), index=False, compression="gzip")
        summary["intraday"] = summarize_intraday(x)
        summary["intraday_rows"] = int(len(x))
    except StopIteration:
        pass
    except Exception as e:
        log("خطأ:", type(e).__name__, e)
        log(traceback.format_exc()[-1500:])
    summary["api_calls"] = c.call_count
    summary["minutes"] = round((time.time() - t0) / 60, 1)
    summary["log"] = LOG[-200:]
    name = "results_v2.json" if MODE == "intraday" else "results.json"
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, default=str)
    log(f"انتهى في {summary['minutes']} دقيقة • طلبات {c.call_count}")


if __name__ == "__main__":
    main()
