# -*- coding: utf-8 -*-
"""إشارات الدراسة v3 على شموع 5 دقائق ليوم واحد. كل قيمة عند الشمعة k تُحسب من الشموع 0..k فقط.

إشارة الزخم (مشتركة بين فرضيات H1–H4، والفرق في الوقف والهدف والفلاتر):
  عند إغلاق شمعة k (إغلاقها بين 09:45 و 15:00 نيويورك):
    - السعر فوق إغلاق الأمس بنسبة min_up إلى max_up
    - الحجم التراكمي منذ 09:30 ≥ rvol_min × متوسط الحجم اليومي لآخر 20 يوماً (يوماً كاملاً — شرط أصعب
      من RVOL المطابق للوقت، ولا يحتاج بيانات داخل اليوم لأيام سابقة)
    - الإغلاق فوق VWAP الجلسة
  أول شمعة تحقق الشروط فقط (إشارة واحدة لكل سهم في اليوم). الدخول على افتتاح الشمعة k+1.

المسافة للوقف تُحسب عند k: tight = atr_mult × ATR(14) ، wide = max(atr_mult × ATR، min_pct × السعر).
"""
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

OPEN_MIN, CLOSE_MIN = 9 * 60 + 30, 16 * 60


@dataclass
class Day:
    """شموع جلسة واحدة (09:30–16:00) بأسعار خام. starts = دقائق منذ منتصف الليل بتوقيت نيويورك."""
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    starts: np.ndarray

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "Day":
        idx = pd.DatetimeIndex(df.index).tz_convert("America/New_York")
        m = (idx.hour * 60 + idx.minute).to_numpy()
        keep = (m >= OPEN_MIN) & (m < CLOSE_MIN)
        d = df.loc[keep]
        return cls(*(d[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close", "volume")), m[keep])

    def __len__(self):
        return len(self.c)


def features(d: Day) -> dict:
    """مصفوفات تراكمية — القيمة عند k تعتمد على 0..k فقط."""
    n = len(d)
    cumv = np.cumsum(d.v)
    tp = (d.h + d.l + d.c) / 3
    cumpv = np.cumsum(tp * d.v)
    vwap = np.where(cumv > 0, cumpv / np.where(cumv > 0, cumv, 1), d.c)
    prev_c = np.concatenate([[d.o[0]], d.c[:-1]]) if n else d.c
    tr = np.maximum(d.h - d.l, np.maximum(np.abs(d.h - prev_c), np.abs(d.l - prev_c)))
    atr = np.empty(n)
    for k in range(n):                       # متوسط آخر 14 مدى حقيقي حتى k (أقل إذا لم يتوفر)
        atr[k] = tr[max(0, k - 13):k + 1].mean()
    return {"cumv": cumv, "vwap": vwap, "atr": atr}


@dataclass(frozen=True)
class TriggerParams:
    min_up: float = 0.05
    max_up: float = 0.25
    rvol_min: float = 3.0
    first_close_min: int = 9 * 60 + 45
    last_close_min: int = 15 * 60


def eligible(d: Day, step: int, p: TriggerParams) -> np.ndarray:
    close_t = d.starts + step
    return (close_t >= p.first_close_min) & (close_t <= p.last_close_min)


def first_trigger(d: Day, f: dict, prev_close: float, avg_vol20: float, p: TriggerParams, step: int = 5) -> Optional[int]:
    if len(d) == 0 or prev_close <= 0 or not avg_vol20 or avg_vol20 <= 0:
        return None
    up = d.c / prev_close - 1
    ok = (eligible(d, step, p) & (up >= p.min_up) & (up <= p.max_up)
          & (f["cumv"] >= p.rvol_min * avg_vol20) & (d.c > f["vwap"]))
    k = np.flatnonzero(ok)
    return int(k[0]) if len(k) else None


def stop_target(price: float, atr: float, kind: str, atr_mult: float, min_pct: float, target_r: float) -> Tuple[float, float]:
    dist = atr_mult * atr if kind == "tight" else max(atr_mult * atr, min_pct * price)
    dist = max(dist, 1e-6)
    return price - dist, price + target_r * dist


def random_bars(d: Day, step: int, p: TriggerParams, rng: np.random.Generator, n: int) -> List[int]:
    """للدخول العشوائي: شمعة عشوائية من نفس نافذة الوقت (09:45–15:00)."""
    idx = np.flatnonzero(eligible(d, step, p))
    if len(idx) == 0:
        return []
    return [int(x) for x in rng.choice(idx, size=n, replace=True)]


def superset_mask(prev_close_raw, prev_equiv, high_raw, vol_adj, avg20_vol_adj, avg20_dv, p: TriggerParams,
                  min_price: float, max_price: float, min_dv: float) -> np.ndarray:
    """شروط ضرورية (لا كافية) من الشمعة اليومية: كل يوم يمكن أن تنطلق فيه الإشارة داخله موجود هنا.
    تُستخدم لاختيار الأيام التي تُنزَّل شموعها فقط — الإشارة نفسها تُقيَّم بعد ذلك بالترتيب الزمني."""
    return ((prev_close_raw >= min_price) & (prev_close_raw <= max_price) & (avg20_dv >= min_dv)
            & (high_raw >= prev_equiv * (1 + p.min_up)) & (vol_adj >= p.rvol_min * avg20_vol_adj))
