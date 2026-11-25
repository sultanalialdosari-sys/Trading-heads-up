# -*- coding: utf-8 -*-
"""تنفيذ افتراضي دقيق بالثانية من صفقات وعروض السوق الكامل (SIP) — لا يُرسل أي أمر حقيقي.

الخطة المجانية في Alpaca تسمح بصفقات وعروض SIP التاريخية الأقدم من 15 دقيقة (تحققنا منها فعلياً).
بدل افتراض «سعر التنبيه» أو «افتتاح الدقيقة»، نعيد بناء ما كان سيحدث لأمر حقيقي:

  الشراء (A/BASE): أمر سوق بعد لحظة التنبيه بثانية ← يُنفَّذ بسعر العرض (ask) في أول عرض صالح بعدها.
  الشراء (B/PRE/POST): أمر وقف-شراء عند مستوى التفعيل ← يتحول لأمر سوق عند أول صفقة ≥ المستوى،
                       ويُنفَّذ بسعر العرض (ask) في أول عرض بعد تلك الصفقة.
  وقف الخسارة: أمر وقف-بيع ← يتحول لأمر سوق عند أول صفقة ≤ الوقف، ويُنفَّذ بسعر الطلب (bid) بعدها
               (فيظهر الانزلاق الحقيقي تحت الوقف بدل افتراض التنفيذ عند الوقف تماماً).
  الهدف: أمر بيع محدد عند الهدف ← يُنفَّذ فقط إذا طُبعت صفقة أعلى من الهدف أو صار الطلب أعلى منه
         (ملامسة الهدف وحدها لا تكفي: قد يكون أمرك في آخر الطابور).
  الخروج الزمني 15:55 نيويورك: أمر سوق ← سعر الطلب (bid) في أول عرض بعد 15:55:00.

تُستبعد صفقات «الكسور» (أقل من 100 سهم) والصفقات المتأخرة أو المصححة لأنها لا تحرّك أوامر الوقف.
لا نجلب كل الصفقات: نستخدم شموع الدقيقة لمعرفة الدقائق التي لامس فيها السعر مستوى مهماً،
ثم نجلب صفقات وعروض تلك الدقائق فقط. إن تعذّر الجلب نرجع لمحاكاة الشموع (hu/trades.py) ونذكر ذلك.
"""
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

import intraday as I
from hu import trades as TR
from hu.rules import tick_up

LATENCY = timedelta(seconds=1)       # من قرار التنبيه إلى وصول الأمر
QUOTE_WAIT = timedelta(seconds=90)   # أقصى انتظار لعرض صالح
# شروط صفقات لا تُفعّل أوامر الوقف ولا تمثل السعر الجاري: كسور، متأخرة، خارج التسلسل، متوسط سعر، مصححة…
SKIP_COND = {"I", "W", "Z", "T", "U", "4", "7", "9", "B", "C", "G", "H", "M", "N", "P", "Q", "R", "V"}

_CACHE: "OrderedDict[tuple, list]" = OrderedDict()
_CACHE_MAX = 400


class TickError(Exception):
    pass


def _ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s).tz_convert("UTC") if pd.Timestamp(s).tzinfo else pd.Timestamp(s).tz_localize("UTC")


def fetch(client, symbol: str, kind: str, a: datetime, b: datetime, cache_ok: bool = True) -> list:
    """صفقات أو عروض SIP بين a و b (مرتبة زمنياً). kind: trades | quotes."""
    key = (symbol, kind, a.isoformat(), b.isoformat())
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    try:
        rows = client._paged(I.DATA_BASE, f"/v2/stocks/{symbol}/{kind}",
                             {"start": I.iso(a), "end": I.iso(b), "feed": "sip", "limit": 10000}, kind)
    except Exception as e:                       # noqa: BLE001 — أي فشل يعيدنا لمحاكاة الشموع
        raise TickError(f"{kind}: {e}")
    out = []
    for r in rows:
        try:
            t = _ts(r["t"])
        except Exception:                        # noqa: BLE001
            continue
        if kind == "trades":
            c = set(r.get("c") or [])
            if c & SKIP_COND or not r.get("p"):
                continue
            out.append(("T", t, float(r["p"]), int(r.get("s") or 0)))
        else:
            bp, ap = float(r.get("bp") or 0), float(r.get("ap") or 0)
            if (bp <= 0 and ap <= 0) or (bp > 0 and ap > 0 and ap < bp):   # فارغ أو متقاطع
                continue
            out.append(("Q", t, bp, ap))
    out.sort(key=lambda x: (x[1], 0 if x[0] == "T" else 1))
    if cache_ok:
        _CACHE[key] = out
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return out


def _first_quote(qs: list, after: pd.Timestamp, side: str) -> Optional[tuple]:
    """أول عرض بعد لحظة معينة فيه الجانب المطلوب: ask للشراء، bid للبيع."""
    for q in qs:
        if q[1] >= after and (q[3] > 0 if side == "ask" else q[2] > 0):
            return q
    return None


def _py(x: pd.Timestamp) -> datetime:
    return x.floor("us").to_pydatetime()


def _quotes_after(client, sym, at: pd.Timestamp, cut: pd.Timestamp, pool: list, side: str) -> Optional[tuple]:
    q = _first_quote(pool, at, side)
    if q is not None:
        return q
    for wait in (QUOTE_WAIT, timedelta(minutes=10)):      # أسهم ضعيفة السيولة: قد يتأخر العرض الصالح
        b = min(at + wait, cut)
        if b <= at:
            return None
        q = _first_quote(fetch(client, sym, "quotes", _py(at), _py(b), cache_ok=b < cut), at, side)
        if q is not None:
            return q
    return None


def sell_fees(price: float, shares: int, fees: dict) -> float:
    """رسوم البيع النظامية فقط (Alpaca بلا عمولة): رسم SEC على قيمة البيع + رسم FINRA TAF لكل سهم."""
    if not price or not shares:
        return 0.0
    sec = price * shares * fees.get("sec_per_million", 0) / 1e6
    taf = min(shares * fees.get("taf_per_share", 0), fees.get("taf_max", 1e9))
    return round(sec + taf, 2)


def simulate(t: dict, bars: Optional[pd.DataFrame], cut_utc: datetime, client, slippage_pct: float,
             target_r: float) -> Optional[dict]:
    """نفس مخرجات TR.simulate لكن بدقة الثانية. يعيد None إن تعذّر (فيُستخدم محاكي الشموع)."""
    try:
        return _simulate(t, bars, cut_utc, client, slippage_pct, target_r)
    except TickError:
        return None


def _simulate(t, bars, cut_utc, client, slip, target_r):
    sym, kind, stop = t["symbol"], t["kind"], float(t["stop"])
    sh = int(t["shares"])
    created = pd.Timestamp(datetime.fromisoformat(t["created_utc"])).tz_convert("UTC")
    ex_t = pd.Timestamp(TR.exit_time(t["day"]).astimezone(I.UTC))
    cut = pd.Timestamp(cut_utc).tz_convert("UTC")
    end = min(cut, ex_t)
    status = "PENDING" if kind in ("B", "PRE", "POST") else "OPEN"
    entry = target = exit_px = None
    entry_at = exit_at = None
    note = ""
    detail: Dict[str, object] = {}

    def arm(px_ask: float, at: pd.Timestamp, why: str):
        nonlocal entry, entry_at, target, status, note
        entry, entry_at = round(px_ask, 4), at
        if entry <= stop:
            status, note = "CANCELLED", "سعر التنفيذ الفعلي كان عند الوقف أو تحته — لا صفقة"
            return
        target = tick_up(entry + target_r * (entry - stop))
        status = "OPEN"
        detail["entry_how"] = why

    # 1) دخول A/BASE: أمر سوق بعد التنبيه بثانية
    if status == "OPEN":
        at = created + LATENCY
        if at >= cut:
            return None                              # لم يمر 15 دقيقة بعد — ننتظر
        q = _quotes_after(client, sym, at, cut, [], "ask")
        if q is None:
            return None
        if q[2] > 0:
            detail["entry_spread_pct"] = round((q[3] - q[2]) / q[3] * 100, 2)
        arm(q[3], q[1], "أمر سوق بسعر العرض")
        if status == "CANCELLED":
            return _out(t, status, None, q[1], None, None, None, note, sh, stop, detail)
        pos = q[1]
    else:
        pos = created

    if bars is None or bars.empty:
        b = pd.DataFrame(columns=["open", "high", "low", "close"])
    else:
        b = bars[(bars.index >= pos.floor("min")) & (bars.index < end)]

    last_close = None
    for ts, r in b.iterrows():
        h, l = float(r["high"]), float(r["low"])
        last_close = float(r["close"])
        if status == "PENDING":
            touch = h >= t["trigger"] or l <= stop
        else:
            touch = l <= stop or h >= target
        if not touch:
            continue
        a = max(ts, pos)
        z = min(ts + pd.Timedelta(minutes=1), end)
        if z <= a:
            continue
        full = z == ts + pd.Timedelta(minutes=1) and z <= cut
        ev = fetch(client, sym, "trades", _py(a), _py(z), full) + fetch(client, sym, "quotes", _py(a), _py(z), full)
        ev.sort(key=lambda x: (x[1], 0 if x[0] == "T" else 1))
        qs = [e for e in ev if e[0] == "Q"]
        i = 0
        while i < len(ev):
            e = ev[i]
            i += 1
            if e[1] < pos:
                continue
            if status == "PENDING":
                if e[0] != "T":
                    continue
                if e[2] <= stop:
                    status, note = "CANCELLED", "أُبطلت الفكرة قبل الاختراق"
                    exit_at = e[1]
                    break
                if e[2] >= t["trigger"]:
                    q = _quotes_after(client, sym, e[1], cut, qs, "ask")
                    px = q[3] if q is not None else tick_up(max(e[2], t["trigger"]) * (1 + slip / 100))
                    at = q[1] if q is not None else e[1]
                    detail["trigger_print"] = e[2]
                    if q is not None and q[2] > 0:
                        detail["entry_spread_pct"] = round((q[3] - q[2]) / q[3] * 100, 2)
                    arm(px, at, "وقف-شراء تفعّل بصفقة ثم تنفيذ بسعر العرض")
                    if status == "CANCELLED":
                        exit_at = at
                        break
                    pos = at
                    continue
            else:  # OPEN
                if e[0] == "T" and e[2] <= stop:
                    q = _quotes_after(client, sym, e[1], cut, qs, "bid")
                    exit_px = q[2] if q is not None else round(e[2] * (1 - slip / 100), 4)
                    exit_at = q[1] if q is not None else e[1]
                    detail["stop_print"] = e[2]
                    status = "STOP"
                    note = (f"وقف تفعّل بصفقة {I.fmt_price(e[2])} وتنفّذ بسعر الطلب {I.fmt_price(exit_px)}"
                            if exit_px < stop else "وقف تفعّل وتنفّذ عند الوقف أو أفضل")
                    break
                if (e[0] == "T" and e[2] > target) or (e[0] == "Q" and e[2] > target):   # e[2] للعرض = الطلب (bid)
                    exit_px, exit_at, status = target, e[1], "TARGET"
                    note = "بيع محدد عند الهدف — تنفّذ لأن السعر تجاوزه"
                    break
        if status not in ("PENDING", "OPEN"):
            break

    if status in ("OPEN", "PENDING") and cut >= ex_t:
        if status == "OPEN":
            q = _quotes_after(client, sym, ex_t, cut, [], "bid")
            if q is not None:
                exit_px, exit_at = q[2], q[1]
            else:
                exit_px, exit_at = (last_close if last_close is not None else entry), ex_t
            status, note = "TIME", "خروج بنهاية المدة (قبل الإغلاق بـ5 دقائق) — أمر سوق بسعر الطلب"
        else:
            status, note, exit_at = "EXPIRED", "لم يخترق المستوى حتى نهاية اليوم", None
    return _out(t, status, entry, entry_at, target, exit_px, exit_at, note, sh, stop, detail)


def _iso(x: Optional[pd.Timestamp]) -> Optional[str]:
    return None if x is None else _py(x).isoformat()


def _out(t, status, entry, entry_at, target, exit_px, exit_at, note, sh, stop, detail) -> dict:
    out = {"status": status, "entry": entry, "entry_utc": _iso(entry_at) if entry is not None else t.get("entry_utc"),
           "target": target, "note": note, "exit": exit_px, "exit_utc": _iso(exit_at), "fill_src": "tick",
           "fill_detail": detail}
    if exit_px is not None and entry:
        out["pnl"] = round((exit_px - entry) * sh, 2)
        out["r_mult"] = round((exit_px - entry) / (entry - stop), 3) if entry > stop else None
        out["pct"] = round((exit_px / entry - 1) * 100, 3)
    return out
