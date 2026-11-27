# -*- coding: utf-8 -*-
"""قواعد التنبيه المبكر (قبل الاختراق). كل قاعدة واضحة وقابلة للضبط من config.json.

الفكرة: سهم حجمه يرتفع بينما سعره متماسك في نطاق ضيق تحت مقاومة قريبة، بقيعان أعلى، فوق VWAP،
ولم يتحرك كثيراً بعد. التنبيه يُرسل قبل اختراق المقاومة فقط. السهم الذي اخترقها بالفعل لا يُنبَّه عليه.

كل الحسابات على الشمعة المغلقة الأخيرة وما قبلها فقط (لا نظر للمستقبل).
"""
import math
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import numpy as np
import pandas as pd

import intraday as I

DEFAULTS = {
    "max_price": 5.0,
    "min_price": 0.20,           # تحت هذا السعر الأرقام غير موثوقة (أسهم بأجزاء السنت)
    "min_dollar_volume": 300_000,
    "max_day_change_pct": 25.0,  # ارتفع أكثر من هذا = لم يعد مبكراً
    "min_rvol": 2.0,
    "spike_mult": 3.0,           # شمعة حجمها ≥ 3× متوسط آخر 20 شمعة
    "window_bars": 12,           # نافذة التماسك: 12 شمعة × 5 دقائق = ساعة
    "max_range_atr": 3.0,        # مدى النافذة ≤ 3 × ATR(5m)
    "max_range_pct": 8.0,        # وأيضاً ≤ 8% من السعر
    "near_res_min_pct": 0.5,     # المسافة تحت المقاومة: من 0.5%
    "near_res_max_pct": 5.0,     #                       إلى 5%
    "max_spread_pct": 2.0,
    "min_score": 65,
    "shares_per_trade": 500,
    "target_r": 2.0,
    "slippage_pct": 0.5,         # انزلاق افتراضي عند الدخول إذا لم يتوفر سعر الطلب
}


@dataclass
class Setup:
    symbol: str
    name: str = ""
    price: Optional[float] = None
    price_time: Optional[str] = None
    price_age_sec: Optional[float] = None
    price_fresh: str = "DATA UNAVAILABLE"
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    day_change_pct: Optional[float] = None
    day_volume: Optional[float] = None
    dollar_volume: Optional[float] = None
    avg_daily_volume20: Optional[float] = None
    vol_vs_avg: Optional[float] = None       # حجم اليوم حتى الآن ÷ متوسط 20 يوماً
    rvol: Optional[float] = None
    rvol_1h_ago: Optional[float] = None
    spike: Optional[float] = None
    range_pct: Optional[float] = None
    range_atr: Optional[float] = None
    higher_lows: bool = False
    above_vwap: bool = False
    vwap: Optional[float] = None
    ema_ok: bool = False
    rsi: Optional[float] = None
    macd_rising: bool = False
    resistance: Optional[float] = None
    support: Optional[float] = None
    dist_res_pct: Optional[float] = None
    trigger: Optional[float] = None          # شرط الدخول: اختراق هذا المستوى
    invalidation: Optional[float] = None     # إبطال الفكرة: كسر هذا المستوى
    stage: str = "NONE"                      # EARLY / BROKEN (فات) / NONE
    score: int = 0
    parts: dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    gates_failed: List[str] = field(default_factory=list)
    news_kind: Optional[str] = None
    news_headline: Optional[str] = None
    shares_out: Optional[float] = None
    shares_out_date: Optional[str] = None
    data_quality: int = 0
    data_quality_notes: List[str] = field(default_factory=list)
    data_note: str = ""
    bar_time: Optional[str] = None

    @property
    def qualifies(self) -> bool:
        return self.stage == "EARLY" and not self.gates_failed and not self.excluded

    def to_dict(self) -> dict:
        return asdict(self)


def tick(p: float) -> float:
    """أصغر وحدة سعر في السوق الأمريكي: سنت فوق 1$، و0.0001 تحته."""
    return 0.0001 if p < 1 else 0.01


def tick_up(p: float) -> float:
    t = tick(p)
    return round(math.ceil(round(p / t, 6)) * t, 4)


def tick_down(p: float) -> float:
    t = tick(p)
    return round(math.floor(round(p / t, 6)) * t, 4)


def macd_hist(close: pd.Series) -> pd.Series:
    m = I.ema(close, 12) - I.ema(close, 26)
    return m - I.ema(m, 9)


def _f(x) -> Optional[float]:
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def evaluate_setup(sym: str, d: pd.DataFrame, cfg: dict, daily: Optional[pd.DataFrame] = None,
                   prev_close: Optional[float] = None) -> Setup:
    """d: شموع 5m للجلسة النظامية مع add_indicators. التقييم على آخر شمعة مغلقة."""
    st = Setup(sym)
    if d is None or len(d) < 40:
        st.missing.append("شموع 5 دقائق غير كافية لحساب المؤشرات")
        st.gates_failed.append("بيانات غير كافية")
        return st
    i = len(d) - 1
    row = d.iloc[i]
    today = d.index[i].date()
    td = d[d.index.date == today]
    st.bar_time = d.index[i].isoformat()
    close = float(row["close"])
    atr = _f(row["atr"])
    W = min(int(cfg["window_bars"]), len(td))   # بداية الجلسة: نافذة أقصر (لا تمتد لليوم السابق)
    if W < 6:
        st.gates_failed.append("بداية الجلسة: أقل من 30 دقيقة من بيانات اليوم")
        st.stage = "NONE"
        return st

    # ---------------- الحجم
    st.rvol = _f(row["rvol"])
    if len(td) > W:
        st.rvol_1h_ago = _f(td["rvol"].iloc[-W - 1])
    st.day_volume = float(td["volume"].sum())
    st.dollar_volume = float((td["volume"] * (td["high"] + td["low"] + td["close"]) / 3).sum())
    va = d["vol_avg20"].shift(1)   # متوسط سابق (لا يشمل الشمعة نفسها)
    recent = td.iloc[-W:]
    ratios = (recent["volume"] / va.reindex(recent.index)).replace([np.inf, -np.inf], np.nan)
    st.spike = _f(ratios.max())
    if daily is not None and len(daily) >= 5:
        past = daily[daily.index.date < today].tail(20)
        if len(past) >= 5:
            st.avg_daily_volume20 = float(past["volume"].mean())
            st.vol_vs_avg = st.day_volume / st.avg_daily_volume20 if st.avg_daily_volume20 else None
    if prev_close:
        st.day_change_pct = (close / prev_close - 1) * 100

    # ---------------- السعر: تماسك، قيعان أعلى، VWAP، المتوسطات، الزخم
    win = d.iloc[i - W + 1: i + 1]
    hh, ll = float(win["high"].max()), float(win["low"].min())
    st.range_pct = (hh - ll) / close * 100 if close else None
    st.range_atr = (hh - ll) / atr if atr else None
    k = max(W // 3, 2)
    lows = [float(win["low"].iloc[j * k:(j + 1) * k].min()) for j in range(3)]
    st.higher_lows = lows[0] < lows[1] < lows[2]
    st.vwap = _f(row["vwap"])
    st.above_vwap = bool(st.vwap is not None and close > st.vwap)
    e9, e20 = _f(row["ema9"]), _f(row["ema20"])
    e9_prev = _f(d["ema9"].iloc[i - 3])
    st.ema_ok = bool(e9 and e20 and (e9 > e20 or (e9_prev and e9 > e9_prev and (e20 - e9) / e20 < 0.005)))
    st.rsi = _f(row["rsi"])
    mh = macd_hist(d["close"])
    st.macd_rising = bool(len(mh) > 3 and mh.iloc[-1] > mh.iloc[-2] > mh.iloc[-3])

    # ---------------- المقاومة والدعم (مستويات معروفة حتى هذه الشمعة فقط)
    lv = I.levels_at(d, i)
    before = td.iloc[:-W]
    if len(before):
        lv.append(float(before["high"].max()))       # قمة اليوم قبل نافذة التماسك
    # المقاومة = أقرب مستوى معروف يبعد على الأقل near_res_min_pct (القمم الصغيرة الملاصقة للسعر داخل التماسك لا تُحسب)
    res = sorted(x for x in lv if x >= close * (1 + cfg["near_res_min_pct"] / 100))
    sup = sorted((x for x in lv if x < close), reverse=True)
    st.resistance = res[0] if res else None
    st.support = sup[0] if sup else None
    if st.resistance:
        st.dist_res_pct = (st.resistance / close - 1) * 100

    st.stage = "EARLY"   # يتحول إلى BROKEN أدناه إذا كان عند قمم جديدة أو تحرك كثيراً

    # ---------------- خطة: شرط الدخول ومستوى الإبطال
    if st.resistance:
        st.trigger = tick_up(st.resistance + tick(st.resistance))
    inval = lows[2] if st.higher_lows else ll
    if atr:
        inval = min(inval, close - 0.5 * atr)
    st.invalidation = tick_down(inval - tick(inval))

    # ---------------- البوابات الإلزامية
    g = st.gates_failed
    if not (cfg["min_price"] <= close <= cfg["max_price"]):
        g.append(f"السعر {close:.4g}$ خارج النطاق {cfg['min_price']}–{cfg['max_price']}$")
    if st.dollar_volume < cfg["min_dollar_volume"]:
        g.append(f"الحجم بالدولار ${st.dollar_volume:,.0f} أقل من ${cfg['min_dollar_volume']:,.0f}")
    if st.rvol is None:
        g.append("RVOL غير متوفر (تاريخ غير كافٍ)")
        st.missing.append("RVOL")
    elif st.rvol < cfg["min_rvol"]:
        g.append(f"RVOL {st.rvol:.1f} أقل من {cfg['min_rvol']}")
    if st.day_change_pct is not None and st.day_change_pct > cfg["max_day_change_pct"]:
        g.append(f"ارتفع {st.day_change_pct:.0f}% اليوم — لم يعد مبكراً")
        st.stage = "BROKEN"
    if st.dist_res_pct is None:
        g.append("لا مقاومة معروفة فوق السعر — السهم عند قمم جديدة (فات التبكير)")
        st.stage = "BROKEN"
    elif not (cfg["near_res_min_pct"] <= st.dist_res_pct <= cfg["near_res_max_pct"]):
        g.append(f"المسافة للمقاومة {st.dist_res_pct:.1f}% خارج {cfg['near_res_min_pct']}–{cfg['near_res_max_pct']}%")
    if not st.above_vwap:
        g.append("السعر تحت VWAP")

    # ---------------- الدرجة من 100
    p = {"الحجم": 0, "التماسك": 0, "الاتجاه": 0, "الزخم": 0, "الأخبار": 0}
    R = st.reasons
    if st.rvol is not None:
        p["الحجم"] += 15 if st.rvol >= 3 else 10 if st.rvol >= cfg["min_rvol"] else 0
        if st.rvol >= cfg["min_rvol"]:
            R.append(f"حجم نسبي {st.rvol:.1f}× مقارنة بنفس الوقت في الأيام السابقة")
    if st.rvol is not None and st.rvol_1h_ago is not None and st.rvol > st.rvol_1h_ago * 1.1:
        p["الحجم"] += 5; R.append(f"الحجم النسبي يتصاعد (كان {st.rvol_1h_ago:.1f}× قبل ساعة)")
    if st.spike is not None and st.spike >= cfg["spike_mult"]:
        p["الحجم"] += 5; R.append(f"قفزة حجم: شمعة بحجم {st.spike:.1f}× المتوسط خلال الساعة الأخيرة")
    if st.dollar_volume >= 2 * cfg["min_dollar_volume"]:
        p["الحجم"] += 5
    tight = (st.range_atr is not None and st.range_atr <= cfg["max_range_atr"]) and \
            (st.range_pct is not None and st.range_pct <= cfg["max_range_pct"])
    if tight:
        p["التماسك"] += 15; R.append(f"تماسك في نطاق ضيق آخر ساعة ({st.range_pct:.1f}% = {st.range_atr:.1f}× ATR)")
    if st.higher_lows:
        p["التماسك"] += 10; R.append("قيعان أعلى متتالية خلال آخر ساعة")
    if st.dist_res_pct is not None and cfg["near_res_min_pct"] <= st.dist_res_pct <= cfg["near_res_max_pct"]:
        p["التماسك"] += 10 if st.dist_res_pct <= 3 else 6
        R.append(f"تحت المقاومة {I.fmt_price(st.resistance)} بـ {st.dist_res_pct:.1f}% فقط — لم يخترق بعد")
    if st.above_vwap:
        p["الاتجاه"] += 8; R.append(f"فوق VWAP ({I.fmt_price(st.vwap)})")
    if st.ema_ok:
        p["الاتجاه"] += 7; R.append("EMA9 فوق EMA20 أو تقطعها للأعلى")
    if st.macd_rising:
        p["الاتجاه"] += 5; R.append("MACD: الهيستوجرام يرتفع 3 شموع متتالية")
    if st.rsi is not None and 45 <= st.rsi <= 70:
        p["الزخم"] += 5
    elif st.rsi is not None and st.rsi > 75:
        st.risks.append(f"RSI {st.rsi:.0f} — تشبع شراء، احتمال تراجع قبل الاختراق")
    st.parts = p
    return st


def apply_news(st: Setup, summary: dict) -> None:
    if not summary.get("available"):
        st.missing.append("الأخبار")
        return
    best = summary.get("best")
    st.news_kind = best
    first = next((x for x in summary["items"] if x["kind"] == best), None)
    st.news_headline = first["headline"] if first else None
    if best == "MATERIAL":
        st.parts["الأخبار"] = 10; st.reasons.append(f"خبر جوهري: {st.news_headline[:90]}")
    elif best == "OTHER":
        st.parts["الأخبار"] = 3
    elif best == "PROMO":
        st.parts["الأخبار"] = 0; st.risks.append(f"خبر ترويجي على الأغلب: {st.news_headline[:90]}")
    elif best == "RISK":
        st.parts["الأخبار"] = 0; st.risks.append(f"خبر مخاطرة: {st.news_headline[:90]}")
        st.parts["خصم"] = st.parts.get("خصم", 0) - 10
    elif best is None:
        st.parts["الأخبار"] = 3   # لا أخبار: محايد


def apply_sec(st: Setup, si) -> None:
    if not si.ok:
        st.missing.append("إفصاحات SEC")
        if si.note:
            st.data_quality_notes.append(si.note)
        return
    st.name = si.name or st.name
    st.shares_out, st.shares_out_date = si.shares_out, si.shares_out_date
    if si.shares_out is None:
        st.missing.append("عدد الأسهم القائمة")
    st.excluded.extend(si.exclude)
    for w, msg in si.penalties:
        st.risks.append(msg)
    if si.penalty:
        st.parts["خصم"] = st.parts.get("خصم", 0) - si.penalty
    for msg in si.infos:
        (st.risks if "تصويت" in msg else st.reasons).append(("إفصاح SEC: " + msg) if "تصويت" not in msg else msg)


def apply_price(st: Setup, pc, cfg: dict) -> None:
    st.price = pc.price
    st.price_time = pc.trade_time_utc.isoformat() if pc.trade_time_utc else None
    st.price_age_sec = pc.age_sec
    st.price_fresh = pc.freshness
    st.bid, st.ask, st.spread_pct = pc.bid, pc.ask, pc.spread_pct
    if pc.spread_pct is None:
        st.missing.append("السبريد (لا عرض/طلب على IEX)")
    elif pc.spread_pct > cfg["max_spread_pct"]:
        st.gates_failed.append(f"السبريد {pc.spread_pct:.1f}% أكبر من {cfg['max_spread_pct']}%")


def finalize(st: Setup, iex_share: Optional[float], sip_ok: bool) -> None:
    st.score = int(max(0, min(100, sum(st.parts.values()))))
    q, notes = 0, st.data_quality_notes
    if st.price_fresh == "FRESH":
        q += 25
    elif st.price_fresh == "DELAYED":
        q += 12; notes.append("آخر صفقة على IEX ليست لحظية")
    else:
        notes.append(f"السعر: {st.price_fresh}")
    if st.spread_pct is not None:
        q += 10
    if sip_ok:
        q += 20
    else:
        notes.append("بيانات السوق الكامل (SIP) غير متاحة — IEX فقط")
    if iex_share is not None:
        q += 15 if iex_share >= 0.05 else 7
        if iex_share < 0.05:
            notes.append(f"حصة IEX من الحجم {iex_share:.1%} فقط — السعر اللحظي أقل دقة")
    if "إفصاحات SEC" not in st.missing:
        q += 15
    if "الأخبار" not in st.missing:
        q += 10
    if st.shares_out is not None:
        q += 5
    st.data_quality = q


def quality_label(q: int) -> str:
    return "جيدة" if q >= 80 else "متوسطة" if q >= 55 else "ضعيفة"


# فرضية H5 (مسجلة مسبقاً في research/hypotheses.jsonl — لا تُعدّل؛ أي تعديل = فرضية جديدة باسم جديد):
# التنبيه «هادئ» إذا لم ينفجر السهم قبله: حجم اليوم ≤ 1.61× متوسط اليوم الكامل، وأكبر قفزة حجم ≤ 8.71×،
# والارتفاع من إغلاق الأمس ≤ 7.34%.
CALM_H5 = {"vol_vs_avg_max": 1.61, "spike_max": 8.71, "day_change_max": 7.34}


def calm_h5(d: dict):
    """1 = هادئ، 0 = ساخن، None = بيانات ناقصة. d = بيانات التنبيه (to_dict)."""
    try:
        v, sp, ch = d.get("vol_vs_avg"), d.get("spike"), d.get("day_change_pct")
        if v is None or sp is None or ch is None:
            return None
        return int(float(v) <= CALM_H5["vol_vs_avg_max"] and float(sp) <= CALM_H5["spike_max"]
                   and float(ch) <= CALM_H5["day_change_max"])
    except (TypeError, ValueError):
        return None

