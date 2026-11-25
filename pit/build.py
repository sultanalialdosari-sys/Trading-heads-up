# -*- coding: utf-8 -*-
"""بناء مخزن البيانات نقطي الزمن (يعمل على GitHub Actions).

  python -m pit.build --out pit_data [--steps assets,daily,sec] [--limit 300]

1) الأصول: النشطة + المشطوبة من Alpaca (بورصات NASDAQ/NYSE/AMEX/ARCA/BATS، بلا OTC).
2) يومي: شموع خام لكل الرموز منذ 2016 ← نُبقي فقط ما كان سعره الخام ≤ max_price في أي يوم
   ← نجلب نسخة split-adjusted لها ← factor = المعدل ÷ الخام ← ملف لكل سنة.
3) أحداث التجميع/التقسيم مستنتجة من factor.
4) SEC: الإفصاحات والأسهم القائمة. CIK يُربط فقط للأصول النشطة غير المشتركة الرمز، لأن قائمة SEC
   للرموز حالية: ربط رمز مشطوب بها قد يعطي شركة أخرى أخذت الرمز لاحقاً. التغطية تُسجل في manifest.
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import intraday as I  # noqa: E402
from pit.edgar import EdgarClient, diagnose_hours  # noqa: E402
from pit.models import ET  # noqa: E402
from pit.store import DAILY_COLS, derive_splits  # noqa: E402

EXCH = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}
CHUNK = 100


def log(*a):
    print(" ".join(str(x) for x in a), flush=True)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ====================================================================== 1) الأصول
def assets_frame(raw_assets: list) -> pd.DataFrame:
    rows = []
    for a in raw_assets:
        s = a.get("symbol") or ""
        if a.get("exchange") in EXCH and s.isalpha() and len(s) <= 5:
            rows.append({"asset_id": a.get("id"), "symbol": s, "name": a.get("name") or "",
                         "exchange": a.get("exchange"), "status": a.get("status") or "inactive"})
    df = pd.DataFrame(rows, columns=["asset_id", "symbol", "name", "exchange", "status"]).drop_duplicates("asset_id")
    df["symbol_shared"] = df.duplicated("symbol", keep=False)
    return df.reset_index(drop=True)


def fetch_assets(c) -> pd.DataFrame:
    allr = []
    for status in ("active", "inactive"):
        r = c._get(I.PAPER_BASE, "/v2/assets", {"status": status, "asset_class": "us_equity"})
        log(f"أصول {status}: {len(r)}")
        allr += r
    return assets_frame(allr)


# ====================================================================== 2) يومي
def bars_frame(b: dict) -> pd.DataFrame:
    parts = []
    for sym, df in b.items():
        if df is None or df.empty:
            continue
        x = pd.DataFrame({
            "symbol": sym,
            "date": df.index.tz_convert(ET).date,
            "open": df["open"].values, "high": df["high"].values, "low": df["low"].values,
            "close": df["close"].values, "volume": df["volume"].values,
            "vwap": df["bar_vwap"].values if "bar_vwap" in df else df["close"].values,
            "trades": df["trades"].values if "trades" in df else 0,
        })
        parts.append(x)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=DAILY_COLS[:-1])


def merge_factor(raw: pd.DataFrame, split: pd.DataFrame) -> pd.DataFrame:
    m = raw.merge(split[["symbol", "date", "close"]].rename(columns={"close": "close_adj"}),
                  on=["symbol", "date"], how="left")
    m["factor"] = (m["close_adj"] / m["close"]).where(m["close"] > 0)
    m["factor"] = m.groupby("symbol")["factor"].transform(lambda s: s.ffill().bfill()).fillna(1.0)
    return m.drop(columns=["close_adj"])


def _bars(c, syms, start, end, adj):
    for attempt in range(4):
        try:
            return c.bars(syms, "1Day", start, end, feed="sip", adjustment=adj)
        except I.ApiError as e:
            log(f"  إعادة ({adj}) بعد خطأ: {e.message}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"فشل جلب الشموع اليومية ({adj}) لـ {syms[0]}… بعد 4 محاولات")


def build_daily(c, symbols, start, end, max_price, tmp, seen=None) -> list:
    """يكتب أجزاء مؤقتة ويرجع الرموز التي دخلت (سعرها الخام ≤ max_price في يوم ما).
    seen (اختياري): مجموعة تُملأ بكل رمز أعاد Alpaca له شموعاً — لقياس تغطية المشطوبة."""
    os.makedirs(tmp, exist_ok=True)
    kept, t0 = [], time.time()
    for i in range(0, len(symbols), CHUNK):
        part = symbols[i:i + CHUNK]
        raw = bars_frame(_bars(c, part, start, end, "raw"))
        if raw.empty:
            continue
        if seen is not None:
            seen.update(raw["symbol"].unique())
        low = raw.groupby("symbol")["close"].min()
        penny = sorted(low[low <= max_price].index)
        if penny:
            split = bars_frame(_bars(c, penny, start, end, "split"))
            m = merge_factor(raw[raw["symbol"].isin(penny)], split)
            for col in ("open", "high", "low", "close", "vwap", "factor"):
                m[col] = m[col].astype("float64")
            m["volume"] = m["volume"].astype("float64")
            m["trades"] = pd.to_numeric(m["trades"], errors="coerce").fillna(0).astype("int64")
            m[DAILY_COLS].to_parquet(os.path.join(tmp, f"part_{i:06d}.parquet"), index=False)
            kept += penny
        if (i // CHUNK) % 10 == 0:
            log(f"  يومي {i + len(part)}/{len(symbols)} • محتفظ {len(kept)} • طلبات {c.call_count} • {(time.time() - t0) / 60:.0f} د")
    return kept


def write_years(tmp: str, out: str) -> pd.DataFrame:
    """يجمع الأجزاء في ملف لكل سنة، ويرجع ملخص أول/آخر يوم لكل رمز + أحداث التجميع."""
    files = sorted(glob.glob(os.path.join(tmp, "part_*.parquet")))
    os.makedirs(os.path.join(out, "daily"), exist_ok=True)
    spans, splits, by_year = [], [], {}
    for f in files:
        df = pd.read_parquet(f)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        spans.append(df.groupby("symbol")["date"].agg(first_bar="min", last_bar="max"))
        splits.append(derive_splits(df))
        df["year"] = pd.to_datetime(df["date"]).dt.year
        for y, g in df.groupby("year"):
            by_year.setdefault(int(y), []).append(g.drop(columns=["year"]))
    for y, parts in by_year.items():
        d = pd.concat(parts, ignore_index=True).sort_values(["symbol", "date"])
        d["symbol"] = d["symbol"].astype("category")
        d.to_parquet(os.path.join(out, "daily", f"{y}.parquet"), index=False, compression="zstd")
    sp = pd.concat(splits, ignore_index=True) if splits else pd.DataFrame(columns=["symbol", "ex_date", "ratio"])
    sp.to_parquet(os.path.join(out, "splits.parquet"), index=False)
    return pd.concat(spans) if spans else pd.DataFrame(columns=["first_bar", "last_bar"])


# ====================================================================== 4) SEC
def build_sec(assets: pd.DataFrame, out: str, cache: str) -> dict:
    try:
        ed = EdgarClient(cache)
    except ValueError as e:
        log(f"تخطي SEC: {e}")
        return {"skipped": str(e)}
    tmap = ed.ticker_map()
    eligible = (assets["status"] == "active") & ~assets["symbol_shared"]
    assets["cik"] = [tmap.get(s, (None,))[0] if ok else None for s, ok in zip(assets["symbol"], eligible)]
    ciks = sorted({int(x) for x in assets["cik"].dropna()})
    log(f"SEC: {len(ciks)} شركة • قائمة الرموز {len(tmap)}")
    fil_rows, sh_rows, all_f, t0 = [], [], [], time.time()
    for k, cik in enumerate(ciks):
        try:
            filings, shares, _ = ed.company(cik)
        except Exception as e:                        # شركة واحدة لا توقف البناء
            log(f"  CIK {cik}: {type(e).__name__} {e}")
            continue
        all_f += filings
        fil_rows += [{"cik": f.cik, "accession": f.accession, "form": f.form, "items": ",".join(f.items),
                      "filed": f.filed, "accepted_raw": f.accepted_raw, "accepted_at": f.accepted_at} for f in filings]
        sh_rows += [{"cik": s.cik, "value": s.value, "period_end": s.period_end, "known_ts": s.known_ts,
                     "accession": s.accession} for s in shares]
        if k % 250 == 0:
            log(f"  SEC {k}/{len(ciks)} • طلبات {ed.calls} • {(time.time() - t0) / 60:.0f} د")
    os.makedirs(os.path.join(out, "sec"), exist_ok=True)
    pd.DataFrame(fil_rows, columns=["cik", "accession", "form", "items", "filed", "accepted_raw", "accepted_at"]) \
        .to_parquet(os.path.join(out, "sec", "filings.parquet"), index=False, compression="zstd")
    pd.DataFrame(sh_rows, columns=["cik", "value", "period_end", "known_ts", "accession"]) \
        .to_parquet(os.path.join(out, "sec", "shares.parquet"), index=False, compression="zstd")
    cov = assets.groupby("status")["cik"].apply(lambda s: round(float(s.notna().mean()), 3)).to_dict()
    diag = diagnose_hours(all_f)
    log(f"SEC: إفصاحات {len(fil_rows)} • حقائق أسهم {len(sh_rows)} • تغطية CIK {cov} • الساعات {diag}")
    return {"companies": len(ciks), "filings": len(fil_rows), "share_facts": len(sh_rows),
            "cik_coverage_by_status": cov, "accept_time_diagnosis": diag, "requests": ed.calls}


# ====================================================================== التشغيل
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(BASE, "pit_data"))
    ap.add_argument("--cache", default=os.path.join(BASE, "pit_cache"))
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--max-price", type=float, default=7.5, help="هامش فوق 5$ حتى لا نفقد أسهماً تعبر الحد")
    ap.add_argument("--steps", default="assets,daily,sec")
    ap.add_argument("--limit", type=int, default=0, help="للتجربة: أول N رمز فقط")
    a = ap.parse_args(argv)
    steps = set(a.steps.split(","))
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    man_path = os.path.join(a.out, "manifest.json")
    man = json.load(open(man_path, encoding="utf-8")) if os.path.exists(man_path) else {}

    assets_path = os.path.join(a.out, "assets.parquet")
    c = None
    if steps & {"assets", "daily"}:
        key, sec = os.environ.get("ALPACA_API_KEY", "").strip(), os.environ.get("ALPACA_API_SECRET", "").strip()
        c = I.AlpacaClient(key, sec, I.Config(max_calls_per_min=int(os.environ.get("RESEARCH_RPM", "100"))))
        c.clock()
    if "assets" in steps:
        assets = fetch_assets(c)
        if a.limit:
            assets = assets.sort_values("symbol").head(a.limit)
    else:
        assets = pd.read_parquet(assets_path)

    if "daily" in steps:
        start = datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc)
        end = c.now_utc() - timedelta(minutes=20)      # SIP التاريخي مسموح إذا أقدم من 15 دقيقة
        tmp = os.path.join(a.cache, "daily_parts")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(a.out, "daily"), ignore_errors=True)
        syms = sorted(assets["symbol"].unique())
        seen: set = set()
        kept = build_daily(c, syms, start, end, a.max_price, tmp, seen)
        st = assets.drop_duplicates("symbol").set_index("symbol")["status"]
        cover = {k: {"symbols": int((st == k).sum()), "with_bars": int(st[st == k].index.isin(seen).sum()),
                     "kept": int(st[st == k].index.isin(set(kept)).sum())} for k in ("active", "inactive")}
        log(f"تغطية حسب الحالة: {cover}")
        spans = write_years(tmp, a.out)
        assets = assets[assets["symbol"].isin(set(kept))].drop(columns=["first_bar", "last_bar"], errors="ignore")
        assets = assets.merge(spans, left_on="symbol", right_index=True, how="left")
        man["daily"] = {"start": a.start, "end": end.isoformat(), "max_price_raw": a.max_price,
                        "symbols_scanned": len(syms), "symbols_kept": len(set(kept)), "coverage_by_status": cover,
                        "requests": c.call_count, "built_utc": datetime.now(timezone.utc).isoformat()}
        log(f"يومي: {len(set(kept))} رمز من {len(syms)}")

    if "cik" not in assets:
        assets["cik"] = None
    if "sec" in steps:
        man["sec"] = build_sec(assets, a.out, os.path.join(a.cache, "edgar"))
        man["sec"]["built_utc"] = datetime.now(timezone.utc).isoformat()

    for col in ("first_bar", "last_bar"):
        if col not in assets:
            assets[col] = None
    assets["cik"] = pd.to_numeric(assets["cik"], errors="coerce").astype("Int64")
    assets.to_parquet(assets_path, index=False)
    man["assets"] = {"total": int(len(assets)), "by_status": assets["status"].value_counts().to_dict(),
                     "symbol_shared": int(assets["symbol_shared"].sum())}
    files = {}
    for p in sorted(glob.glob(os.path.join(a.out, "**", "*.parquet"), recursive=True)):
        rel = os.path.relpath(p, a.out)
        files[rel] = {"bytes": os.path.getsize(p), "sha256": sha256(p)}
    man.update({"version": 1, "files": files, "built_utc": datetime.now(timezone.utc).isoformat(),
                "minutes": round((time.time() - t0) / 60, 1)})
    json.dump(man, open(man_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    log(f"انتهى في {man['minutes']} دقيقة • {len(files)} ملف • "
        f"{sum(v['bytes'] for v in files.values()) / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
