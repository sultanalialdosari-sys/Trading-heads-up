# -*- coding: utf-8 -*-
"""محرك التكاليف والانزلاق.

التكلفة لكل صفقة تُحسب من السعر والسيولة لحظة الدخول، ومن الشموع **قبل** الدخول فقط:

  spread  = max(تقدير Corwin–Schultz من الشموع، أرضية حسب شريحة السعر) × مضاعف السيولة
  slip    = min(ربع متوسط مدى الشمعة قبل الدخول، حد أعلى حسب شريحة السعر)       — لكل طرف
  fees    = رسوم SEC و FINRA TAF على البيع فقط
  round-trip = spread + slip_دخول + slip_خروج  ← ثم أرضية السيناريو (0.3% / 0.5% / 1.0%): يؤخذ الأكبر

بيانات IEX المجانية لا تعطي سجل عرض/طلب تاريخي، لذلك يُقدَّر السبريد من أعلى وأدنى شمعتين متتاليتين
(Corwin & Schultz 2012). الأرضيات والحدود في config.json ← costs (أمثلة بدائية تُعايَر بلقطات النظام الحي).
"""
import math
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

from hu.config import Costs

Scenario = Literal["optimistic", "base", "pessimistic"]
CS_LOOKBACK = 20          # عدد الشموع السابقة لتقدير السبريد
SLIP_RANGE_FRAC = 0.25    # الانزلاق = ربع متوسط مدى الشمعة
DV_THIN = 250_000
RVOL_THIN = 2.0


def bucket(price: float) -> str:
    return "lt_1" if price < 1.0 else "1_to_5"


def tick(price: float, cfg: Costs) -> float:
    return cfg.tick.lt_1 if price < 1.0 else cfg.tick.ge_1


def round_buy(price: float, cfg: Costs) -> float:
    """الشراء يُقرَّب للأعلى (ضدنا)."""
    t = tick(price, cfg)
    return round(math.ceil(price / t - 1e-9) * t, 6)


def round_sell(price: float, cfg: Costs) -> float:
    """البيع يُقرَّب للأسفل (ضدنا)."""
    t = tick(price, cfg)
    return round(math.floor(price / t + 1e-9) * t, 6)


def corwin_schultz(h1: float, l1: float, h2: float, l2: float) -> float:
    """تقدير السبريد النسبي من شمعتين متتاليتين. السالب يُعامل صفراً (كما في الورقة الأصلية)."""
    if min(h1, l1, h2, l2) <= 0 or h1 < l1 or h2 < l2:
        return 0.0
    beta = math.log(h1 / l1) ** 2 + math.log(h2 / l2) ** 2
    gamma = math.log(max(h1, h2) / min(l1, l2)) ** 2
    k = 3 - 2 * math.sqrt(2)
    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
    return max(0.0, 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)))


def cs_estimate(bars_before: Optional[pd.DataFrame], n: int = CS_LOOKBACK) -> Optional[float]:
    if bars_before is None or len(bars_before) < 3:
        return None
    b = bars_before.tail(n + 1)
    h, l = b["high"].to_numpy(float), b["low"].to_numpy(float)
    vals = [corwin_schultz(h[i - 1], l[i - 1], h[i], l[i]) for i in range(1, len(b))]
    return float(np.mean(vals)) if vals else None


def avg_range_pct(bars_before: Optional[pd.DataFrame], n: int = CS_LOOKBACK) -> Optional[float]:
    if bars_before is None or len(bars_before) == 0:
        return None
    b = bars_before.tail(n)
    c = b["close"].replace(0, np.nan)
    r = ((b["high"] - b["low"]) / c).dropna()
    return float(r.mean()) if len(r) else None


@dataclass(frozen=True)
class CostQuote:
    price: float
    spread_pct: float          # كنسبة: 0.01 = 1%
    slip_entry_pct: float
    slip_exit_pct: float
    floor_pct: float           # أرضية السيناريو للتكلفة الكاملة ذهاباً وإياباً
    scenario: str
    spread_source: str         # "corwin_schultz" | "floor"
    liq_mult: float

    @property
    def model_round_trip_pct(self) -> float:
        return self.spread_pct + self.slip_entry_pct + self.slip_exit_pct

    @property
    def round_trip_pct(self) -> float:
        return max(self.model_round_trip_pct, self.floor_pct)


class CostModel:
    def __init__(self, cfg: Costs):
        self.cfg = cfg

    def quote(self, price: float, bars_before: Optional[pd.DataFrame], rvol: Optional[float],
              dollar_vol: Optional[float], scenario: Scenario = "base") -> CostQuote:
        """bars_before = شموع قبل لحظة الإشارة فقط. لا تُمرَّر شمعة التنفيذ أبداً."""
        if scenario not in self.cfg.scenarios:
            raise ValueError(f"سيناريو غير معروف: {scenario}")
        bk = bucket(price)
        floor = getattr(self.cfg.spread_floor, "lt_1" if bk == "lt_1" else "to_5")
        cap = getattr(self.cfg.slip_cap_per_side, "lt_1" if bk == "lt_1" else "to_5")
        cs = cs_estimate(bars_before)
        spread, src = (cs, "corwin_schultz") if cs is not None and cs > floor else (floor, "floor")
        m = 1.0
        if dollar_vol is None or dollar_vol < DV_THIN:
            m *= self.cfg.liquidity_mult.dv_lt_250k      # غير معروف = نعامله كضعيف (تحفظ)
        if rvol is None or rvol < RVOL_THIN:
            m *= self.cfg.liquidity_mult.rvol_lt_2
        rng = avg_range_pct(bars_before)
        slip = cap if rng is None else min(cap, SLIP_RANGE_FRAC * rng)
        return CostQuote(price=price, spread_pct=spread * m, slip_entry_pct=slip, slip_exit_pct=slip,
                         floor_pct=self.cfg.scenarios[scenario], scenario=scenario, spread_source=src, liq_mult=m)

    def sell_fees(self, price: float, qty: int) -> float:
        f = self.cfg.fees
        sec = math.ceil(price * qty * f.sec_per_million / 1e6 * 100) / 100 if f.sec_per_million else 0.0
        taf = min(f.taf_max, math.ceil(qty * f.taf_per_share * 100) / 100) if f.taf_per_share else 0.0
        return round(sec + taf, 2)
