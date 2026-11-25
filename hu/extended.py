# -*- coding: utf-8 -*-
"""مراقبة ما قبل الافتتاح (PRE) وما بعد الإغلاق (POST).

الفكرة مختلفة عن التنبيه المبكر داخل الجلسة: هنا نبحث عن أسهم تحت 5$ تتحرك خارج الجلسة بحجم حقيقي
(غالباً بسبب خبر)، ونضعها في «قائمة مراقبة» للجلسة التالية مع مستوى تفعيل (قمة ما قبل/بعد الجلسة)
ومستوى إبطال. النتيجة تُقاس من الجلسة النظامية التالية فقط.

البيانات: لقطة السوق الكامل متأخرة 15 دقيقة (delayed_sip) لكل الأسهم، ثم شموع دقيقة للقائمة القصيرة:
السوق الكامل حتى قبل 16 دقيقة + IEX لما بعدها (IEX يغطي 8:00–17:00 نيويورك فقط).
"""
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

import intraday as I
from hu import rules as RL
from hu import timefmt as TF

ET, UTC = I.ET, I.UTC

EXT_DEFAULTS = {
    "ext_min_change_pct": 8.0,          # تحرك 8%+ عن آخر إغلاق نظامي
    "pre_min_dollar_volume": 150_000,   # حجم دولاري قبل الافتتاح
    "post_min_dollar_volume": 75_000,   # بعد الإغلاق السيولة أقل
    "ext_max_from_high_pct": 15.0,      # يجب أن يبقى قريباً من قمته (لم ينهَر)
    "ext_min_score": 60,
    "ext_shortlist": 40,
}

PHASE_AR = {"PRE": "ما قبل الافتتاح", "POST": "بعد الإغلاق"}


@dataclass
class ExtSetup:
    symbol: str
    phase: str
    name: str = ""
    price: Optional[float] = None
    price_time: Optional[str] = None
    ref_close: Optional[float] = None
    change_pct: Optional[float] = None
    ext_volume: float = 0.0
    ext_dollar_volume: float = 0.0
    ext_high: Optional[float] = None
    ext_low: Optional[float] = None
    ext_vwap: Optional[float] = None
    from_high_pct: Optional[float] = None
    higher_lows: bool = False
    trigger: Optional[float] = None
    invalidation: Optional[float] = None
    score: int = 0
    parts: dict = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)
    risks: List[str] = field(default_factory=list)
    excluded: List[str] = field(default_factory=list)
    gates_failed: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    news_kind: Optional[str] = None
    news_headline: Optional[str] = None
    shares_out: Optional[float] = None
    shares_out_date: Optional[str] = None
    data_quality: int = 0
    data_quality_notes: List[str] = field(default_factory=list)
    data_note: str = ""
    stage: str = "EXT"

    @property
    def qualifies(self) -> bool:
        return not self.gates_failed and not self.excluded

    def to_dict(self) -> dict:
        d = asdict(self)
        d["qualifies"] = self.qualifies
        return d


def _ts(x) -> Optional[datetime]:
    try:
        return I.parse_ts(x) if x else None
    except Exception:
        return None


def ext_rows(snaps: Dict[str, dict], names: Dict[str, str], phase: str, now: datetime, cfg: dict) -> pd.DataFrame:
    """من لقطات السوق: السهم الذي تحرك خارج الجلسة اليوم مقارنة بآخر إغلاق نظامي."""
    today = now.astimezone(ET).date()
    start_hm = (4, 0) if phase == "PRE" else (16, 0)
    rows = []
    for s, sn in snaps.items():
        db, pb, lt = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}, sn.get("latestTrade") or {}
        t, p = _ts(lt.get("t")), lt.get("p")
        if not t or not p:
            continue
        te = t.astimezone(ET)
        if te.date() != today or (te.hour, te.minute) < start_hm:
            continue                     # لا صفقة في هذه الفترة اليوم
        dbt = _ts(db.get("t"))
        if phase == "PRE":
            ref = db.get("c") if dbt and dbt.astimezone(ET).date() < today else pb.get("c")
        else:
            ref = db.get("c") if dbt and dbt.astimezone(ET).date() == today else None
        if not ref:
            continue
        chg = (float(p) / float(ref) - 1) * 100
        rows.append({"symbol": s, "name": names.get(s, ""), "price": float(p), "ref_close": float(ref),
                     "chg": chg, "trade_t": t.isoformat()})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    m = (df.price.between(cfg["min_price"], cfg["max_price"] * 1.1) & (df.chg >= cfg["ext_min_change_pct"]))
    return df[m].sort_values("chg", ascending=False).head(int(cfg["ext_shortlist"]))


def ext_bars(client: I.AlpacaClient, syms: List[str], phase: str, now: datetime) -> Dict[str, pd.DataFrame]:
    """شموع دقيقة من بداية الفترة: السوق الكامل حتى قبل 16 دقيقة، ثم IEX للباقي."""
    day = now.astimezone(ET)
    start = day.replace(hour=4 if phase == "PRE" else 16, minute=0, second=0, microsecond=0).astimezone(UTC)
    cut = (now - timedelta(minutes=16)).replace(second=0, microsecond=0)
    out: Dict[str, pd.DataFrame] = {}
    for i in range(0, len(syms), 50):
        part = syms[i:i + 50]
        sip, iex = {}, {}
        if cut > start:
            try:
                sip = client.bars(part, "1Min", start, cut, feed="sip")
            except I.ApiError:
                sip = {}
        try:
            iex = client.bars(part, "1Min", max(start, cut), None)
        except I.ApiError:
            iex = {}
        for s in part:
            frames = [f for f in (sip.get(s), iex.get(s)) if f is not None and len(f)]
            if frames:
                d = pd.concat(frames)
                out[s] = d[~d.index.duplicated(keep="first")].sort_index()
    return out


def evaluate_ext(row: dict, bars: Optional[pd.DataFrame], phase: str, cfg: dict) -> ExtSetup:
    st = ExtSetup(row["symbol"], phase, name=row.get("name", ""), price=row["price"], price_time=row.get("trade_t"),
                  ref_close=row["ref_close"], change_pct=row["chg"])
    g, R = st.gates_failed, st.reasons
    if bars is None or bars.empty:
        st.missing.append("شموع خارج الجلسة")
        g.append("لا شموع كافية لهذه الفترة")
        return st
    tp = (bars["high"] + bars["low"] + bars["close"]) / 3
    st.ext_volume = float(bars["volume"].sum())
    st.ext_dollar_volume = float((tp * bars["volume"]).sum())
    st.ext_high, st.ext_low = float(bars["high"].max()), float(bars["low"].min())
    st.ext_vwap = float((tp * bars["volume"]).sum() / bars["volume"].sum()) if st.ext_volume > 0 else None
    last = float(bars["close"].iloc[-1])
    st.price = st.price or last
    st.from_high_pct = (1 - st.price / st.ext_high) * 100 if st.ext_high else None
    tail = bars.iloc[-30:]
    if len(tail) >= 9:
        k = len(tail) // 3
        lows = [float(tail["low"].iloc[j * k:(j + 1) * k].min()) for j in range(3)]
        st.higher_lows = lows[0] < lows[1] < lows[2]

    min_dv = cfg["pre_min_dollar_volume"] if phase == "PRE" else cfg["post_min_dollar_volume"]
    if not (cfg["min_price"] <= st.price <= cfg["max_price"]):
        g.append(f"السعر {I.fmt_price(st.price)} خارج النطاق")
    if st.ext_dollar_volume < min_dv:
        g.append(f"الحجم بالدولار {PHASE_AR[phase]} ${st.ext_dollar_volume:,.0f} أقل من ${min_dv:,.0f}")
    if st.from_high_pct is not None and st.from_high_pct > cfg["ext_max_from_high_pct"]:
        g.append(f"تراجع {st.from_high_pct:.0f}% عن قمته — الحركة تنهار")

    p = {"التحرك": 0, "السيولة": 0, "الثبات": 0, "الأخبار": 0}
    c = st.change_pct
    p["التحرك"] = 20 if c >= 25 else 15 if c >= 15 else 10
    R.append(f"{'+' if c >= 0 else ''}{c:.1f}% {PHASE_AR[phase]} عن آخر إغلاق ({I.fmt_price(st.ref_close)})")
    dv = st.ext_dollar_volume
    p["السيولة"] = 25 if dv >= 1_000_000 else 20 if dv >= 500_000 else 10 if dv >= min_dv else 0
    R.append(f"حجم {PHASE_AR[phase]}: {st.ext_volume:,.0f} سهم (${dv:,.0f})")
    fh = st.from_high_pct
    if fh is not None:
        p["الثبات"] += 20 if fh <= 5 else 12 if fh <= 10 else 5 if fh <= 15 else 0
        R.append(f"على بعد {fh:.1f}% من قمة {PHASE_AR[phase]} ({I.fmt_price(st.ext_high)})")
    if st.ext_vwap and st.price > st.ext_vwap:
        p["الثبات"] += 10
        R.append(f"فوق VWAP الفترة ({I.fmt_price(st.ext_vwap)})")
    if st.higher_lows:
        p["الثبات"] += 5
        R.append("قيعان أعلى في آخر 30 دقيقة")
    st.parts = p

    # المستويات للجلسة التالية
    st.trigger = RL.tick_up(st.ext_high + RL.tick(st.ext_high))
    stop = (st.ext_vwap if st.ext_vwap and st.ext_vwap < st.trigger else st.ext_low)
    if st.trigger and stop and (st.trigger - stop) / st.trigger < 0.03:
        stop = st.trigger * 0.95          # مسافة صغيرة جداً — وقف 5% تحت المستوى
    st.invalidation = RL.tick_down(stop - RL.tick(stop))
    return st


def apply_news(st: ExtSetup, summary: dict) -> None:
    if not summary.get("available"):
        st.missing.append("الأخبار")
        return
    best = summary.get("best")
    st.news_kind = best
    first = next((x for x in summary["items"] if x["kind"] == best), None)
    st.news_headline = first["headline"] if first else None
    if best == "MATERIAL":
        st.parts["الأخبار"] = 20; st.reasons.append(f"خبر جوهري: {st.news_headline[:90]}")
    elif best == "OTHER":
        st.parts["الأخبار"] = 8; st.reasons.append(f"خبر: {st.news_headline[:90]}")
    elif best == "PROMO":
        st.risks.append(f"خبر ترويجي على الأغلب: {st.news_headline[:90]}")
    elif best == "RISK":
        st.risks.append(f"خبر مخاطرة: {st.news_headline[:90]}")
        st.parts["خصم"] = st.parts.get("خصم", 0) - 15
    else:
        st.risks.append("لا يوجد خبر يفسر الحركة — حذر")


def finalize(st: ExtSetup, sec_ok: bool) -> None:
    st.score = int(max(0, min(100, sum(st.parts.values()))))
    q = 20  # السوق الكامل متأخر 15 دقيقة متاح
    q += 20 if st.ext_volume > 0 else 0
    q += 20 if sec_ok else 0
    q += 15 if "الأخبار" not in st.missing else 0
    q += 10 if st.price_time else 0
    q += 5 if st.shares_out else 0
    st.data_quality = q


def alert_text(st: ExtSetup, eval_from: Optional[datetime]) -> str:
    icon = "🌅" if st.phase == "PRE" else "🌙"
    when = "قبل الافتتاح" if st.phase == "PRE" else "بعد الإغلاق"
    L = [f"{icon} قائمة مراقبة {when} — {st.symbol}" + (f" ({st.name[:40]})" if st.name else ""),
         f"💵 {I.fmt_price(st.price)} ({st.change_pct:+.1f}% عن إغلاق {I.fmt_price(st.ref_close)}) — آخر صفقة "
         f"{_ry(st.price_time)} الرياض",
         "⏱️ السوق الكامل متأخر 15 دقيقة، وIEX يغطي 4 م – 1 ص بتوقيت الرياض فقط",
         f"📊 حجم {when}: {st.ext_volume:,.0f} سهم (${st.ext_dollar_volume:,.0f})",
         "🔎 لماذا ظهر:"] + [f"• {r}" for r in st.reasons[:6]] + [
         f"▶️ التفعيل في الجلسة: اختراق قمة {when} {I.fmt_price(st.trigger)}",
         f"⛔ الإبطال: كسر {I.fmt_price(st.invalidation)}",
         f"🕘 يُقاس من افتتاح {_ry(eval_from.isoformat()) if eval_from else 'الجلسة التالية'} الرياض"]
    L.append("⚠️ المخاطر: " + (" | ".join(st.risks[:4]) if st.risks else "لا مخاطر ظاهرة"))
    so = f"{st.shares_out / 1e6:.1f}M" if st.shares_out else "غير متوفر"
    L.append(f"🏷️ الأسهم القائمة: {so} • Float: غير متوفر")
    L.append(f"⭐ الدرجة {st.score}/100 • جودة البيانات {st.data_quality}/100")
    L.append("حركة خارج الجلسة سيولتها ضعيفة وقد تنعكس عند الافتتاح. ليست توصية شراء.")
    return "\n".join(L)


def _ry(iso: Optional[str]) -> str:
    if not iso:
        return "غير متوفر"
    d = datetime.fromisoformat(iso).astimezone(I.RIYADH)
    return (d.strftime("%a ") + TF.h12(d)).replace(
        "Mon", "الاثنين").replace("Tue", "الثلاثاء").replace("Wed", "الأربعاء").replace("Thu", "الخميس").replace(
        "Fri", "الجمعة").replace("Sat", "السبت").replace("Sun", "الأحد")
