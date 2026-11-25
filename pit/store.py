# -*- coding: utf-8 -*-
"""مخزن البيانات نقطي الزمن على ملفات Parquet، وواجهة PointInTimeView المقيدة بلحظة واحدة.

البنية على القرص:
  assets.parquet                     الأصول النشطة والمشطوبة (asset_id، الرمز، أول/آخر يوم تداول، CIK)
  daily/{year}.parquet               شموع يومية خام (raw) + factor = سعر معدل للتجميع ÷ الخام
  splits.parquet                     أحداث التجميع/التقسيم المستنتجة من factor
  sec/filings.parquet                الإفصاحات بوقت القبول
  sec/shares.parquet                 الأسهم القائمة من XBRL بوقت معرفتها
  intraday/{tf}/{symbol}/{day}.parquet   شموع دقيقة/5 دقائق تُجلب عند الحاجة وتُخزن
  manifest.json                      بصمات الملفات، التغطية، تاريخ البناء

قاعدة حارسة: دوال الميزات والمحاكاة تستقبل PointInTimeView فقط، لا المخزن نفسه.
"""
import json
import os
from datetime import date, datetime
from functools import lru_cache
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from pit.edgar import shares_as_of
from pit.models import ET, TF_DELTA, UTC, Asset, Filing, SharesFact, SplitEvent, daily_known_ts

DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "vwap", "trades", "factor"]
SPLIT_MIN_DEV = 0.02     # تغير factor أكثر من 2% في يوم = تجميع/تقسيم


class LookAheadError(RuntimeError):
    """محاولة قراءة بيانات بعد لحظة القرار."""


def _aware(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError("as_of يجب أن يكون timezone-aware")
    return t.astimezone(UTC)


# ====================================================================== اشتقاق
def derive_splits(daily: pd.DataFrame) -> pd.DataFrame:
    """factor = adj/raw. عند تجميع عكسي 1:10 يقفز الخام ×10 ويبقى المعدل متصلاً، فيصبح
    factor قبل الحدث = 10 × factor بعده. ratio (أسهم جديدة لكل قديم) = factor_بعد ÷ factor_قبل."""
    if daily.empty:
        return pd.DataFrame(columns=["symbol", "ex_date", "ratio"])
    d = daily[["symbol", "date", "factor"]].sort_values(["symbol", "date"])
    prev = d.groupby("symbol")["factor"].shift(1)
    ratio = d["factor"] / prev
    m = prev.notna() & (np.abs(ratio - 1) > SPLIT_MIN_DEV)
    out = pd.DataFrame({"symbol": d.loc[m, "symbol"].values, "ex_date": d.loc[m, "date"].values,
                        "ratio": ratio[m].astype("float64").values})
    return out.reset_index(drop=True)


# ====================================================================== المخزن
class ParquetStore:
    def __init__(self, root: str, intraday_fetcher: Optional[Callable] = None):
        """intraday_fetcher(symbol, day: date, tf) -> DataFrame بفهرس UTC (اختياري: جلب عند الحاجة)."""
        self.root, self.fetch_intraday = root, intraday_fetcher
        self._assets: Optional[List[Asset]] = None
        self._filings: Optional[Dict[int, List[Filing]]] = None
        self._shares: Optional[Dict[int, List[SharesFact]]] = None
        self._splits: Optional[Dict[str, List[SplitEvent]]] = None

    def p(self, *parts) -> str:
        return os.path.join(self.root, *parts)

    @property
    def manifest(self) -> dict:
        path = self.p("manifest.json")
        return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}

    # ---------------------------------------------------------------- الأصول
    def assets(self) -> List[Asset]:
        if self._assets is None:
            path = self.p("assets.parquet")
            self._assets = []
            if os.path.exists(path):
                for r in pd.read_parquet(path).itertuples(index=False):
                    self._assets.append(Asset(
                        asset_id=r.asset_id, symbol=r.symbol, name=r.name or "", exchange=r.exchange, status=r.status,
                        first_bar=_d(r.first_bar), last_bar=_d(r.last_bar),
                        cik=int(r.cik) if pd.notna(r.cik) else None, symbol_shared=bool(r.symbol_shared)))
        return self._assets

    def universe(self, as_of: datetime) -> List[Asset]:
        """كل أصل كان يتداول قبل يوم as_of ولم يُشطب قبله — بما فيها ما شُطب لاحقاً (بلا انحياز للناجين)."""
        d = _aware(as_of).astimezone(ET).date()
        return [a.masked(as_of) for a in self.assets()
                if a.first_bar is not None and a.first_bar < d and (a.last_bar is None or a.last_bar >= d)]

    # ---------------------------------------------------------------- يومي
    @lru_cache(maxsize=16)
    def _year(self, y: int) -> pd.DataFrame:
        path = self.p("daily", f"{y}.parquet")
        if not os.path.exists(path):
            return pd.DataFrame(columns=DAILY_COLS)
        df = pd.read_parquet(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        return df

    def daily(self, symbols: Optional[Iterable[str]], start: date, as_of: datetime, adjusted: bool = False) -> pd.DataFrame:
        """شموع يومية حتى آخر يوم أُغلق قبل as_of (16:00 نيويورك). adjusted=True للعوائد فقط؛
        فلتر السعر (تحت 5$) يُطبق دائماً على الخام."""
        as_of = _aware(as_of)
        last_day = as_of.astimezone(ET).date()
        parts = [self._year(y) for y in range(start.year, last_day.year + 1)]
        df = pd.concat([x for x in parts if len(x)], ignore_index=True) if any(len(x) for x in parts) \
            else pd.DataFrame(columns=DAILY_COLS)
        if symbols is not None:
            df = df[df["symbol"].isin(set(symbols))]
        df = df[(df["date"] >= start) & (df["date"] <= last_day)]
        if len(df) and df["date"].max() == last_day and as_of < daily_known_ts(last_day):
            df = df[df["date"] < last_day]                       # شمعة اليوم لم تُغلق بعد
        df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
        if adjusted:
            df = df.copy()
            for c in ("open", "high", "low", "close", "vwap"):
                df[c] = df[c] * df["factor"]
            df["volume"] = df["volume"] / df["factor"]
        return df

    # ---------------------------------------------------------------- داخل اليوم
    def intraday(self, symbol: str, day: date, tf: str, as_of: datetime) -> pd.DataFrame:
        if tf not in TF_DELTA:
            raise ValueError(f"tf غير مدعوم: {tf}")
        as_of = _aware(as_of)
        path = self.p("intraday", tf, symbol, f"{day.isoformat()}.parquet")
        if os.path.exists(path):
            df = pd.read_parquet(path)
        elif self.fetch_intraday is not None:
            df = self.fetch_intraday(symbol, day, tf)
            if df is not None:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                df.to_parquet(path)
            else:
                df = pd.DataFrame()
        else:
            df = pd.DataFrame()
        if df.empty:
            return df
        idx = pd.DatetimeIndex(df.index)
        df.index = idx.tz_convert(UTC) if idx.tz is not None else idx.tz_localize(UTC)
        return df[df.index + TF_DELTA[tf] <= as_of]          # الشمعة تُعرف عند إغلاقها

    # ---------------------------------------------------------------- SEC
    def _load_sec(self):
        if self._filings is not None:
            return
        self._filings, self._shares = {}, {}
        fp, sp = self.p("sec", "filings.parquet"), self.p("sec", "shares.parquet")
        if os.path.exists(fp):
            for r in pd.read_parquet(fp).itertuples(index=False):
                self._filings.setdefault(int(r.cik), []).append(Filing(
                    cik=int(r.cik), accession=r.accession, form=r.form,
                    items=tuple(x for x in (r.items or "").split(",") if x), filed=_d(r.filed),
                    accepted_raw=r.accepted_raw or "", accepted_at=_ts(r.accepted_at)))
        if os.path.exists(sp):
            for r in pd.read_parquet(sp).itertuples(index=False):
                self._shares.setdefault(int(r.cik), []).append(SharesFact(
                    cik=int(r.cik), value=float(r.value), period_end=_d(r.period_end), known_ts=_ts(r.known_ts),
                    split_adjusted=False, stale=False, accession=r.accession))
        for v in self._filings.values():
            v.sort(key=lambda f: f.accepted_at)

    def filings(self, cik: Optional[int], as_of: datetime, forms: Iterable[str] = (),
                items: Iterable[str] = (), since: Optional[datetime] = None) -> List[Filing]:
        if cik is None:
            return []
        self._load_sec()
        as_of, forms, items = _aware(as_of), set(forms), set(items)
        out = []
        for f in self._filings.get(int(cik), []):
            if f.known_ts > as_of:
                break
            if since is not None and f.known_ts < since:
                continue
            if (forms or items) and not (f.form in forms or set(f.items) & items):
                continue                                    # المطلوب: النموذج أو أحد البنود
            out.append(f)
        return out

    def splits(self, symbol: str) -> List[SplitEvent]:
        if self._splits is None:
            self._splits = {}
            path = self.p("splits.parquet")
            if os.path.exists(path):
                for r in pd.read_parquet(path).itertuples(index=False):
                    self._splits.setdefault(r.symbol, []).append(SplitEvent(r.symbol, _d(r.ex_date), float(r.ratio)))
        return self._splits.get(symbol, [])

    def shares_outstanding(self, cik: Optional[int], symbol: str, as_of: datetime) -> Optional[SharesFact]:
        if cik is None:
            return None
        self._load_sec()
        return shares_as_of(self._shares.get(int(cik), []), self.splits(symbol), _aware(as_of))

    def view(self, as_of: datetime) -> "PointInTimeView":
        return PointInTimeView(self, as_of)


class PointInTimeView:
    """نافذة مقيدة بلحظة واحدة. لا توجد طريقة لطلب بيانات بعد as_of من خلالها."""

    __slots__ = ("_s", "as_of")

    def __init__(self, store: ParquetStore, as_of: datetime):
        object.__setattr__(self, "_s", store)
        object.__setattr__(self, "as_of", _aware(as_of))

    def __setattr__(self, k, v):
        raise AttributeError("PointInTimeView غير قابل للتعديل")

    def universe(self) -> List[Asset]:
        return self._s.universe(self.as_of)

    def daily(self, symbols=None, start: date = date(2016, 1, 1), adjusted: bool = False) -> pd.DataFrame:
        return self._s.daily(symbols, start, self.as_of, adjusted)

    def intraday(self, symbol: str, day: date, tf: str = "5Min") -> pd.DataFrame:
        if day > self.as_of.astimezone(ET).date():
            raise LookAheadError(f"طلب شموع {day} من لحظة {self.as_of}")
        return self._s.intraday(symbol, day, tf, self.as_of)

    def filings(self, cik, forms=(), items=(), since=None) -> List[Filing]:
        return self._s.filings(cik, self.as_of, forms, items, since)

    def shares_outstanding(self, cik, symbol) -> Optional[SharesFact]:
        return self._s.shares_outstanding(cik, symbol, self.as_of)


def _d(x) -> Optional[date]:
    if x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NaT:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return pd.Timestamp(x).date()
    except Exception:
        return None


def _ts(x) -> datetime:
    t = pd.Timestamp(x)
    return (t.tz_localize(UTC) if t.tzinfo is None else t.tz_convert(UTC)).to_pydatetime()
