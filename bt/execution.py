# -*- coding: utf-8 -*-
"""محاكاة التنفيذ والتوقفات على شموع الدقيقة أو 5 دقائق. كل غموض يُحل ضد الصفقة.

القواعد (config.json ← exits / execution):
  الدخول     : الإشارة عند إغلاق شمعة t ← التنفيذ على افتتاح الشمعة التالية (+ نصف السبريد + الانزلاق، تقريب للأعلى).
               إذا كان سعر الدخول ≤ الوقف أو ≥ الهدف (فجوة) ← لا صفقة.
  داخل الشمعة: الافتتاح تحت الوقف ← خروج بالافتتاح (فجوة). الافتتاح فوق الهدف ← خروج بالافتتاح (أمر محدد).
               لمس الوقف والهدف معاً ← **الوقف أولاً**. الهدف يُحتسب فقط إذا تجاوزه الأعلى بتيك واحد.
  الوقت      : شمعة تبدأ عند 15:55 نيويورك أو بعدها ← خروج بافتتاحها.
  التوقفات   : فجوة زمنية ≥ halt_gap_min بلا شموع في سهم كان يتداول كل شمعة ← توقف (تقريب لـ LULD).
               لا خروج ممكن أثناءه؛ بعد الاستئناف تحت الوقف ← خروج بأول سعر مهما بعُد.
               لا دخول في أول no_entry_bars_after_halt شموع بعد الاستئناف.
  التكلفة    : خروج السوق (وقف/فجوة/وقت) يدفع نصف السبريد + الانزلاق، والهدف أمر محدد بلا تكلفة إضافية.
               إذا قلّت التكلفة الفعلية عن أرضية السيناريو ← يُخصم الفرق من سعر الخروج.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

import pandas as pd

from bt.costs import CostModel, CostQuote, round_buy, round_sell, tick
from hu.config import AppConfig, Execution
from pit.models import ET, UTC

TF_MIN = {"1Min": 1, "5Min": 5}
HALT_PRIOR_CONSEC = 6          # «كان يتداول كل شمعة»: آخر 6 شموع متتالية بلا فجوة


@dataclass(frozen=True)
class Order:
    symbol: str
    signal_ts: datetime        # لحظة القرار = إغلاق شمعة الإشارة (UTC)
    qty: int
    stop: float
    target: float
    time_exit: datetime        # 15:55 نيويورك من يوم الصفقة (UTC)
    tf: str = "5Min"


@dataclass(frozen=True)
class Fill:
    ts: datetime
    price: float               # بعد التكلفة والتقريب
    ref_price: float           # السعر المرجعي قبل التكلفة (افتتاح/وقف/هدف/إغلاق)
    side: str                  # buy | sell
    reason: str                # entry | stop | target | gap_stop | gap_target | halt_resume | time | data_end


@dataclass
class Trade:
    order: Order
    quote: CostQuote
    entry: Optional[Fill] = None
    exit: Optional[Fill] = None
    skipped: Optional[str] = None          # سبب عدم الدخول
    halted: bool = False
    fees: float = 0.0
    floor_adj: float = 0.0                 # ما خُصم من سعر الخروج لبلوغ أرضية السيناريو

    @property
    def filled(self) -> bool:
        return self.entry is not None and self.exit is not None

    @property
    def pnl(self) -> Optional[float]:
        if not self.filled:
            return None
        return round((self.exit.price - self.entry.price) * self.order.qty - self.fees, 2)

    @property
    def risk(self) -> Optional[float]:
        return None if self.entry is None else self.entry.price - self.order.stop

    @property
    def r_multiple(self) -> Optional[float]:
        if not self.filled or self.risk <= 0:
            return None
        return (self.exit.price - self.entry.price - self.fees / self.order.qty) / self.risk

    @property
    def r_gross(self) -> Optional[float]:
        """نفس الصفقة بلا تكاليف — لقياس كم تأكل التكاليف."""
        if not self.filled:
            return None
        risk = self.entry.ref_price - self.order.stop
        return (self.exit.ref_price - self.entry.ref_price) / risk if risk > 0 else None

    @property
    def cost_pct(self) -> Optional[float]:
        if not self.filled:
            return None
        e = self.entry
        return ((e.price - e.ref_price) + (self.exit.ref_price - self.exit.price)) / e.ref_price


def time_exit_for(day: date, cfg: AppConfig) -> datetime:
    hh, mm = map(int, cfg.exits.time_exit_et.split(":"))
    return datetime.combine(day, time(hh, mm), tzinfo=ET).astimezone(UTC)


def _utc_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index)
    d = df.copy()
    d.index = idx.tz_convert(UTC) if idx.tz is not None else idx.tz_localize(UTC)
    return d.sort_index()


def detect_halts(bars: pd.DataFrame, tf: str, halt_gap_min: int) -> List[Tuple[pd.Timestamp, pd.Timestamp]]:
    """(آخر شمعة قبل التوقف، أول شمعة بعده). يشترط أن السهم كان يتداول كل شمعة قبل الفجوة،
    حتى لا نسمي سهماً قليل التداول «موقوفاً»."""
    if len(bars) < HALT_PRIOR_CONSEC + 1:
        return []
    step = pd.Timedelta(minutes=TF_MIN[tf])
    ts = list(_utc_index(bars).index)
    out = []
    for i in range(HALT_PRIOR_CONSEC, len(ts)):
        missing = ts[i] - ts[i - 1] - step
        if missing >= pd.Timedelta(minutes=halt_gap_min):
            prior = ts[i - HALT_PRIOR_CONSEC:i]
            if all(prior[k] - prior[k - 1] == step for k in range(1, len(prior))):
                out.append((ts[i - 1], ts[i]))
    return out


def simulate(order: Order, bars_after: pd.DataFrame, quote: CostQuote, model: CostModel, ex: Execution,
             bars_before: Optional[pd.DataFrame] = None) -> Trade:
    """bars_after: شموع تبدأ عند signal_ts أو بعده (فهرس = بداية الشمعة). bars_before: لكشف توقف قبل الدخول."""
    tr = Trade(order=order, quote=quote)
    if bars_after is None or len(bars_after) == 0:
        tr.skipped = "no_bars"
        return tr
    after = _utc_index(bars_after)
    if after.index.min() < pd.Timestamp(order.signal_ts):
        raise ValueError("bars_after تحتوي شموعاً بدأت قبل لحظة الإشارة — التنفيذ عليها نظر للماضي")
    before = _utc_index(bars_before) if bars_before is not None and len(bars_before) else after.iloc[:0]
    allb = pd.concat([before, after])
    halts = detect_halts(allb, order.tf, ex.halt_gap_min)
    resumes = {r for _, r in halts}
    cfg = model.cfg
    s_half, slip_in, slip_out = quote.spread_pct / 2, quote.slip_entry_pct, quote.slip_exit_pct

    # ---------------------------------------------------------------- الدخول
    pos = {t: k for k, t in enumerate(allb.index)}
    j = 0
    while j < len(after) and ex.no_entry_bars_after_halt:
        prior = [r for r in resumes if r <= after.index[j]]
        if not prior or pos[after.index[j]] - pos[max(prior)] >= ex.no_entry_bars_after_halt:
            break                                # مرت N شموع على الأقل منذ آخر استئناف
        j += 1
    if j >= len(after) or after.index[j] >= pd.Timestamp(order.time_exit):
        tr.skipped = "no_bars_before_time_exit"
        return tr
    ref = float(after["open"].iloc[j])
    px = round_buy(ref * (1 + s_half + slip_in), cfg)
    if px <= order.stop:
        tr.skipped = "gap_below_stop"
        return tr
    if px >= order.target:
        tr.skipped = "gap_above_target"
        return tr
    tr.entry = Fill(after.index[j].to_pydatetime(), px, ref, "buy", "entry")

    # ---------------------------------------------------------------- الخروج
    def market(ts, ref_px, reason):
        return Fill(ts.to_pydatetime(), round_sell(ref_px * (1 - s_half - slip_out), cfg), ref_px, "sell", reason)

    t_up = tick(order.target, cfg)
    exit_fill = None
    for i in range(j, len(after)):
        ts = after.index[i]
        o, h, l = (float(after[c].iloc[i]) for c in ("open", "high", "low"))
        if ts in resumes and i > j:
            tr.halted = True
        if ts >= pd.Timestamp(order.time_exit):
            exit_fill = market(ts, o, "time")
            break
        if i > j and o <= order.stop:
            exit_fill = market(ts, o, "halt_resume" if ts in resumes else "gap_stop")
            break
        if i > j and o >= order.target:
            exit_fill = Fill(ts.to_pydatetime(), round_sell(o, cfg), o, "sell", "gap_target")
            break
        if l <= order.stop:                       # الوقف أولاً حتى لو لمس الهدف في نفس الشمعة
            exit_fill = market(ts, order.stop, "stop")
            break
        if h >= order.target + t_up:
            exit_fill = Fill(ts.to_pydatetime(), order.target, order.target, "sell", "target")
            break
    if exit_fill is None:
        last = after.index[-1]
        exit_fill = market(last, float(after["close"].iloc[-1]), "data_end")

    # ---------------------------------------------------------------- أرضية السيناريو
    e = tr.entry
    realized = ((e.price - e.ref_price) + (exit_fill.ref_price - exit_fill.price)) / e.ref_price
    if realized < quote.floor_pct - 1e-9:                 # هامش لأخطاء الفاصلة العائمة
        adj = (quote.floor_pct - realized) * e.ref_price
        new_px = round_sell(exit_fill.price - adj, cfg)
        tr.floor_adj = round(exit_fill.price - new_px, 6)
        exit_fill = Fill(exit_fill.ts, new_px, exit_fill.ref_price, "sell", exit_fill.reason)
    tr.exit = exit_fill
    tr.fees = model.sell_fees(exit_fill.price, order.qty)
    return tr


def simulate_from_store(store, symbol: str, signal_ts: datetime, stop: float, target: float, app: AppConfig,
                        scenario: str = "base", rvol: Optional[float] = None, dollar_vol: Optional[float] = None,
                        tf: str = "5Min", qty: Optional[int] = None) -> Trade:
    """يربط المحاكاة بمخزن البيانات: التسعير من النافذة المقيدة بلحظة الإشارة، والتنفيذ من الشموع بعدها."""
    day = signal_ts.astimezone(ET).date()
    before = store.view(signal_ts).intraday(symbol, day, tf)          # ما أُغلق قبل القرار فقط
    end = datetime.combine(day, time(20, 0), tzinfo=ET).astimezone(UTC)
    full = store.intraday(symbol, day, tf, end)
    after = full[full.index >= pd.Timestamp(signal_ts)] if len(full) else full
    model = CostModel(app.costs)
    price = float(before["close"].iloc[-1]) if len(before) else float(after["open"].iloc[0]) if len(after) else 0.0
    q = model.quote(price, before, rvol, dollar_vol, scenario)
    order = Order(symbol, signal_ts, qty or app.paper.shares_per_trade, stop, target, time_exit_for(day, app), tf)
    return simulate(order, after, q, model, app.execution, before)
