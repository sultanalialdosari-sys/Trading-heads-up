# -*- coding: utf-8 -*-
"""الدراسة v3: اختبار الفرضيات المسجلة مسبقاً (research/hypotheses.jsonl) بالبنية الكاملة.

  python research_v3.py --stage fetch    ينزّل شموع 5 دقائق لأيام «المجموعة الشاملة» (قابل للاستئناف)
  python research_v3.py --stage analyze  يقيّم الإشارات بالترتيب الزمني، يحاكي، يتحقق، يكتب التقرير

اختيار الأيام بلا نظر للنتيجة: نُنزّل كل يوم *يمكن* أن تنطلق فيه الإشارة (شروط ضرورية من الشمعة اليومية:
أعلى سعر ≥ +5% فوق إغلاق الأمس، وحجم اليوم ≥ 3× متوسط 20 يوماً)، ثم تُقيَّم الإشارة شمعة بشمعة.
الأيام التي «انتهت قوية» ليست هي المعيار — وهذا ما كان يُخلّ بالدراسة القديمة.
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import traceback
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import intraday as I  # noqa: E402
from bt import fastsim as FS  # noqa: E402
from bt import signals as SG  # noqa: E402
from bt import stats as S  # noqa: E402
from bt.costs import CS_LOOKBACK, DV_THIN, RVOL_THIN, SLIP_RANGE_FRAC, corwin_schultz  # noqa: E402
from bt.registry import Registry, research_config_hash  # noqa: E402
from bt.validate import build_report, to_markdown  # noqa: E402
from hu import config as C  # noqa: E402
from pit.models import ET  # noqa: E402
from pit.store import ParquetStore  # noqa: E402

SCEN = ("optimistic", "base", "pessimistic")
ETF_NAME = re.compile(r"\b(?:ETF|ETN|FUND|TRUST|PROSHARES|DIREXION|ISHARES|LEVERAGED|INVERSE|ULTRA|2X|3X|-1X|BULL|BEAR)\b", re.I)
LOG: List[str] = []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    LOG.append(s)


# ====================================================================== 1) الأيام المرشحة (يومي، نقطي الزمن)
def daily_panel(store: ParquetStore) -> pd.DataFrame:
    years = sorted(int(os.path.basename(p)[:4]) for p in glob.glob(store.p("daily", "*.parquet")))
    df = pd.concat([store._year(y) for y in years], ignore_index=True)
    df["symbol"] = df["symbol"].astype(str)
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = df.groupby("symbol", sort=False)
    df["vol_adj"] = df["volume"] / df["factor"]
    df["dv"] = df["close"] * df["volume"]
    df["prev_close_raw"] = g["close"].shift(1)
    df["prev_factor"] = g["factor"].shift(1)
    df["prev_equiv"] = df["prev_close_raw"] * df["prev_factor"] / df["factor"]     # إغلاق الأمس بوحدة سعر اليوم الخام
    df["avg20_vol_adj"] = g["vol_adj"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    df["avg20_dv"] = g["dv"].transform(lambda s: s.shift(1).rolling(20, min_periods=20).mean())
    return df


def select_events(store: ParquetStore, params: dict) -> pd.DataFrame:
    df = daily_panel(store)
    a = pd.DataFrame([{"symbol": x.symbol, "name": x.name, "cik": x.cik, "shared": x.symbol_shared}
                      for x in store.assets()]).drop_duplicates("symbol")
    bad = set(a.loc[a["name"].fillna("").str.contains(ETF_NAME) | a["shared"], "symbol"])
    u, tp = params["universe"], params["trigger"]
    p = SG.TriggerParams(min_up=tp["min_up"], max_up=tp["max_up"], rvol_min=tp["rvol_min"])
    m = SG.superset_mask(df["prev_close_raw"], df["prev_equiv"], df["high"], df["vol_adj"], df["avg20_vol_adj"],
                         df["avg20_dv"], p, u["prev_close_raw"][0], u["prev_close_raw"][1], u["min_avg_dollar_volume_20d"])
    ev = df[m.fillna(False) & ~df["symbol"].isin(bad)].copy()
    ev = ev.merge(a[["symbol", "cik"]], on="symbol", how="left")
    log(f"لوحة يومية {len(df):,} صف • أيام مرشحة (شروط ضرورية) {len(ev):,} • مستبعد ETF/رمز مشترك {len(bad)} رمزاً")
    return ev[["symbol", "date", "prev_close_raw", "prev_equiv", "factor", "avg20_vol_adj", "avg20_dv", "cik"]]


# ====================================================================== 2) تنزيل شموع 5 دقائق (قابل للاستئناف)
def fetch(c, ev: pd.DataFrame, bars_dir: str, max_minutes: float) -> dict:
    os.makedirs(bars_dir, exist_ok=True)
    ev = ev.assign(month=pd.to_datetime(ev["date"]).dt.strftime("%Y-%m"))
    have = {os.path.basename(p)[:7] for p in glob.glob(os.path.join(bars_dir, "*.parquet"))}
    todo = sorted(set(ev["month"]) - have)
    t0, done = time.time(), 0
    log(f"أشهر موجودة {len(have)} • مطلوبة {len(todo)}")
    for mo in todo:
        if (time.time() - t0) / 60 > max_minutes:
            log(f"توقف عند حد الوقت ({max_minutes} د) — يُستأنف في التشغيل التالي")
            break
        parts = []
        for d, g in ev[ev["month"] == mo].groupby("date"):
            syms = sorted(g["symbol"].unique())
            start = datetime.combine(d, dtime(9, 30), tzinfo=ET)
            end = datetime.combine(d, dtime(16, 0), tzinfo=ET)
            for i in range(0, len(syms), 100):
                for attempt in range(4):
                    try:
                        b = c.bars(syms[i:i + 100], "5Min", start, end, feed="sip", adjustment="raw")
                        break
                    except I.ApiError as e:
                        log(f"  إعادة {d} بعد خطأ: {e.message}")
                        time.sleep(5 * (attempt + 1))
                else:
                    raise RuntimeError(f"فشل تنزيل {d}")
                for s, x in b.items():
                    if len(x):
                        parts.append(pd.DataFrame({"symbol": s, "date": d, "ts": x.index, "open": x["open"].values,
                                                   "high": x["high"].values, "low": x["low"].values,
                                                   "close": x["close"].values, "volume": x["volume"].values}))
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
            columns=["symbol", "date", "ts", "open", "high", "low", "close", "volume"])
        out.to_parquet(os.path.join(bars_dir, f"{mo}.parquet"), index=False, compression="zstd")
        done += 1
        if done % 6 == 0:
            log(f"  {mo} • أشهر {done}/{len(todo)} • طلبات {c.call_count} • {(time.time() - t0) / 60:.0f} د")
    left = sorted(set(ev["month"]) - {os.path.basename(p)[:7] for p in glob.glob(os.path.join(bars_dir, "*.parquet"))})
    return {"months_total": int(ev["month"].nunique()), "months_left": len(left), "requests": c.call_count}


# ====================================================================== 3) التكلفة على مصفوفات اليوم (مطابقة لـ CostModel)
def cost_arrays(d: SG.Day) -> tuple:
    n = len(d)
    pair = np.zeros(n)
    for i in range(1, n):
        pair[i] = corwin_schultz(d.h[i - 1], d.l[i - 1], d.h[i], d.l[i])
    rng = np.where(d.c > 0, (d.h - d.l) / np.where(d.c > 0, d.c, 1), np.nan)
    cs, rg = np.full(n, np.nan), np.full(n, np.nan)
    for k in range(n):
        if k >= 2:                                         # cs_estimate يحتاج 3 شموع على الأقل
            cs[k] = pair[max(1, k - CS_LOOKBACK + 1):k + 1].mean()
        w = rng[max(0, k - CS_LOOKBACK + 1):k + 1]
        rg[k] = np.nanmean(w) if np.any(~np.isnan(w)) else np.nan
    return cs, rg


def quote_at(price, cs_k, rg_k, rvol, dv, costs) -> tuple:
    lt1 = price < 1.0
    floor = costs.spread_floor.lt_1 if lt1 else costs.spread_floor.to_5
    cap = costs.slip_cap_per_side.lt_1 if lt1 else costs.slip_cap_per_side.to_5
    spread = cs_k if (cs_k == cs_k and cs_k > floor) else floor          # cs_k == cs_k: ليس NaN
    m = 1.0
    if dv is None or not dv == dv or dv < DV_THIN:
        m *= costs.liquidity_mult.dv_lt_250k
    if rvol is None or not rvol == rvol or rvol < RVOL_THIN:
        m *= costs.liquidity_mult.rvol_lt_2
    slip = cap if not rg_k == rg_k else min(cap, SLIP_RANGE_FRAC * rg_k)
    return spread * m, slip


# ====================================================================== 4) التحليل
def sim_one(d, k_signal, stop, target, sp, slip, scen_floor, app, resume, step=5):
    c, ex, f = app.costs, app.execution, app.costs.fees
    return FS.sim(d.o, d.h, d.l, d.c, d.starts, k_signal + 1, stop, target, 15 * 60 + 55, resume,
                  sp / 2, slip, slip, scen_floor, app.paper.shares_per_trade, c.tick.lt_1, c.tick.ge_1,
                  f.sec_per_million, f.taf_per_share, f.taf_max, ex.no_entry_bars_after_halt)


def sec_blocked(store, cik, t_signal, blk) -> Optional[bool]:
    if cik is None or cik != cik:
        return None
    since = t_signal - timedelta(days=blk["window_days"])
    return bool(store.filings(int(cik), t_signal, forms=blk["forms"], items=blk["items"], since=since))


def analyze(store: ParquetStore, ev: pd.DataFrame, bars_dir: str, app, hyps: Dict[str, dict], n_random: int,
            seed: int, sec_ok: bool) -> tuple:
    p0 = next(iter(hyps.values()))["params"]["trigger"]
    tp = SG.TriggerParams(min_up=p0["min_up"], max_up=p0["max_up"], rvol_min=p0["rvol_min"])
    floors = app.costs.scenarios
    test_from = app.validation.test_from
    rows, rand = [], {h: [] for h in hyps}
    rng = np.random.default_rng(seed)
    n_days = n_trig = 0
    evi = ev.set_index(["symbol", "date"])
    for f in sorted(glob.glob(os.path.join(bars_dir, "*.parquet"))):
        bars = pd.read_parquet(f)
        if bars.empty:
            continue
        bars["date"] = pd.to_datetime(bars["date"]).dt.date
        for (sym, day), g in bars.groupby(["symbol", "date"], sort=False):
            if (sym, day) not in evi.index:
                continue
            e = evi.loc[(sym, day)]
            d = SG.Day.from_frame(g.set_index(pd.DatetimeIndex(g["ts"])))
            if len(d) < 4:
                continue
            n_days += 1
            fe = SG.features(d)
            avg_vol_raw = float(e["avg20_vol_adj"]) * float(e["factor"])
            k = SG.first_trigger(d, fe, float(e["prev_equiv"]), avg_vol_raw, tp)
            if k is None or k + 1 >= len(d):
                continue
            n_trig += 1
            cs, rg = cost_arrays(d)
            resume = FS.halt_resumes(list(d.starts), 5, app.execution.halt_gap_min)
            t_sig = datetime.combine(day, dtime(0), tzinfo=ET) + timedelta(minutes=int(d.starts[k]) + 5)
            is_test = day >= test_from
            for hid, h in hyps.items():
                P = h["params"]
                blk = None
                if "sec_block" in P:
                    if not sec_ok:
                        continue
                    blk = sec_blocked(store, e["cik"], t_sig, P["sec_block"])
                    if blk is None or blk:
                        continue                                 # بلا CIK أو محظور
                st = P["stop"]
                stop, target = SG.stop_target(d.c[k], fe["atr"][k], st["kind"], st["atr_mult"], st.get("min_pct", 0), P["target_r"])
                sp, slip = quote_at(d.c[k], cs[k], rg[k], fe["cumv"][k] / avg_vol_raw, float(e["avg20_dv"]), app.costs)
                res = {s: sim_one(d, k, stop, target, sp, slip, floors[s], app, resume) for s in SCEN}
                b = res["base"]
                rows.append({"hid": hid, "symbol": sym, "day": day, "test": is_test, "signal_et": f"{int(d.starts[k] + 5) // 60:02d}:{int(d.starts[k] + 5) % 60:02d}",
                             "price": round(float(d.c[k]), 4), "stop": round(stop, 4), "target": round(target, 4),
                             **{f"r_{s}": res[s][0] for s in SCEN}, "r_gross": b[1], "reason": b[2],
                             "cost_pct": b[3], "halted": b[4]})
                if is_test and n_random and b[0] is not None:
                    elig = np.flatnonzero(SG.eligible(d, 5, tp))
                    vals = []
                    for kk in rng.choice(elig, size=n_random, replace=True):
                        kk = int(kk)
                        if kk + 1 >= len(d):
                            vals.append(np.nan); continue
                        s2, t2 = SG.stop_target(d.c[kk], fe["atr"][kk], st["kind"], st["atr_mult"], st.get("min_pct", 0), P["target_r"])
                        sp2, sl2 = quote_at(d.c[kk], cs[kk], rg[kk], fe["cumv"][kk] / avg_vol_raw, float(e["avg20_dv"]), app.costs)
                        r2 = sim_one(d, kk, s2, t2, sp2, sl2, floors["base"], app, resume)[0]
                        vals.append(np.nan if r2 is None else r2)
                    rand[hid].append((day, vals))
        log(f"  {os.path.basename(f)[:7]} • أيام {n_days:,} • إشارات {n_trig:,} • صفقات {len(rows):,}")
    return pd.DataFrame(rows), rand, {"stock_days": n_days, "triggers": n_trig}


def random_daily_draws(rand_list) -> List[pd.Series]:
    if not rand_list:
        return []
    days = [d for d, _ in rand_list]
    mat = np.array([v for _, v in rand_list], dtype="float64")
    return [S.r_by_day(days, mat[:, j]) for j in range(mat.shape[1])]


# ====================================================================== 5) الإخراج
def spy_regime(c) -> Optional[dict]:
    try:
        b = c.bars(["SPY"], "1Day", datetime(2015, 9, 1, tzinfo=timezone.utc), c.now_utc() - timedelta(minutes=20),
                   feed="sip", adjustment="all").get("SPY")
    except Exception as e:
        log("SPY:", e)
        return None
    s = b["close"].copy()
    s.index = s.index.tz_convert(ET).date
    sma = s.rolling(50).mean().shift(1)
    prev = s.shift(1)
    return {d: ("صاعد" if p > m else "هابط") for d, p, m in zip(s.index, prev, sma) if m == m}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["fetch", "analyze", "all"], default="all")
    ap.add_argument("--pit", default=os.path.join(BASE, "pit_data"))
    ap.add_argument("--bars", default=os.path.join(BASE, "r3_bars"))
    ap.add_argument("--out", default=os.path.join(BASE, "research_out"))
    ap.add_argument("--registry", default=os.path.join(BASE, "research", "hypotheses.jsonl"))
    ap.add_argument("--max-fetch-min", type=float, default=300)
    ap.add_argument("--random", type=int, default=100)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    app = C.load(os.path.join(BASE, "config.json"))
    reg = Registry(a.registry)
    hyps = {k: v for k, v in reg.hypotheses().items() if k.startswith("H")}
    cfg_hash = research_config_hash(app)
    stale = [k for k, v in hyps.items() if v.get("config_hash") != cfg_hash]
    if stale:
        sys.exit(f"إعدادات البحث تغيرت بعد تسجيل {stale} — لا يجوز اختبارها. سجّل فرضيات جديدة.")
    store = ParquetStore(a.pit)
    sec_ok = os.path.exists(store.p("sec", "filings.parquet"))
    params = next(iter(hyps.values()))["params"]
    ev = select_events(store, params)
    key, sec = os.environ.get("ALPACA_API_KEY", "").strip(), os.environ.get("ALPACA_API_SECRET", "").strip()
    c = I.AlpacaClient(key, sec, I.Config(max_calls_per_min=int(os.environ.get("RESEARCH_RPM", "100")))) if key else None
    summary = {"generated_utc": datetime.now(timezone.utc).isoformat(), "config_hash": cfg_hash,
               "events_superset": int(len(ev)), "sec_available": sec_ok}
    if a.stage in ("fetch", "all"):
        summary["fetch"] = fetch(c, ev, a.bars, a.max_fetch_min)
        log("تنزيل:", summary["fetch"])
        if summary["fetch"]["months_left"]:
            json.dump(summary, open(os.path.join(a.out, "fetch_status.json"), "w"), ensure_ascii=False, indent=1, default=str)
            return
    if a.stage == "fetch":
        return
    months = {os.path.basename(p)[:7] for p in glob.glob(os.path.join(a.bars, "*.parquet"))}
    need = set(pd.to_datetime(ev["date"]).dt.strftime("%Y-%m"))
    if need - months:
        sys.exit(f"شموع ناقصة لـ {len(need - months)} شهراً — شغّل fetch أولاً (التحليل على بيانات ناقصة يحيز النتيجة)")
    t0 = time.time()
    trades, rand, cnt = analyze(store, ev, a.bars, app, hyps, a.random, app.validation.bootstrap.seed, sec_ok)
    summary.update(cnt)
    regime = spy_regime(c) if c else None
    trades.to_csv(os.path.join(a.out, "trades_v3.csv.gz"), index=False, compression="gzip")
    md = ["# الدراسة v3 — نتائج الفرضيات المسجلة مسبقاً", "",
          f"أُنشئ {summary['generated_utc'][:16]} UTC • بصمة إعدادات البحث `{cfg_hash}` • "
          f"أيام مرشحة {len(ev):,} • أيام بشموع {cnt['stock_days']:,} • إشارات {cnt['triggers']:,}", "",
          "التدريب (≤ 2022) للاطلاع فقط — المعاملات ثابتة مسبقاً ولم تُضبط عليه. الحكم من فترة الاختبار (2023+) وحدها، "
          "وكل فرضية تُختبر مرة واحدة.", ""]
    res, reps = {}, {}
    lab = (lambda d: regime.get(d, "غير معروف")) if regime else None
    for hid, h in hyps.items():                       # المرور الأول: التقارير بلا q
        t = trades[trades["hid"] == hid]
        if t.empty:
            md += [f"## {hid}", "", "لم تُختبر: " + ("بيانات SEC غير متوفرة" if "sec_block" in h["params"] and not sec_ok else "لا صفقات"), ""]
            continue
        if reg.test_result(hid):
            prev = reg.test_result(hid)["result"]
            md += [f"## {hid}", "", f"سبق اختبارها — النتيجة الأولى هي المعتمدة: {prev.get('verdict')}", ""]
            res[hid] = {"locked": True, "first_result": prev}
            continue
        tr, te = t[~t["test"]], t[t["test"]]
        rtr = build_report(hid + " (تدريب)", tr, app, regime=lab)
        rep = build_report(hid, te, app, registry=None, random_draws=random_daily_draws(rand[hid]), regime=lab)
        rep.n_trials = reg.n_trials
        reps[hid] = (rtr, rep)
    # تصحيح تعدد المحاولات على كل الفرضيات المسجلة معاً (السابقة + الجديدة) — لا يعتمد على الترتيب
    pv = reg.test_pvalues()
    pv.update({hid: rep.p_bootstrap for hid, (_, rep) in reps.items()})
    q = (S.benjamini_hochberg if app.validation.multiple_testing == "benjamini_hochberg" else S.bonferroni)(pv)
    for hid, (rtr, rep) in reps.items():
        rep.q_bh = q.get(hid)
        rep.deflated_sharpe = S.deflated_sharpe(S.r_by_day(pd.to_datetime(trades.loc[(trades.hid == hid) & trades.test, "day"]).dt.date,
                                                           trades.loc[(trades.hid == hid) & trades.test, "r_base"]), reg.n_trials)
        from bt.validate import verdict as _verdict
        rep.verdict, rep.reasons = _verdict(rep, app)
        reg.touch_test(hid, hyps[hid]["params"], cfg_hash, {"p_bootstrap": rep.p_bootstrap, "q_bh": rep.q_bh,
                                                            "verdict": rep.verdict, "expectancy_r": rep.expectancy_r,
                                                            "n_trades": rep.n_trades})
        md += [f"### {hid} — التدريب للاطلاع: {rtr.n_trades} صفقة، متوسط R "
               f"{(rtr.expectancy_r['base'] or 0):+.3f}، p = {rtr.p_bootstrap}", "",
               to_markdown(rep), "", f"وصف: {hyps[hid]['description']}", ""]
        res[hid] = {"train": rtr.to_dict(), "test": rep.to_dict()}
    summary["results"] = res
    summary["minutes_analyze"] = round((time.time() - t0) / 60, 1)
    summary["log"] = LOG[-150:]
    open(os.path.join(a.out, "REPORT_v3.md"), "w", encoding="utf-8").write("\n".join(md))
    json.dump(summary, open(os.path.join(a.out, "results_v3.json"), "w"), ensure_ascii=False, indent=1, default=str)
    log("\n".join(md))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
