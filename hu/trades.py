# -*- coding: utf-8 -*-
"""الصفقات الافتراضية — لا تُرسل أي أمر حقيقي أبداً.

لكل تنبيه صفقتان افتراضيتان:
  A: دخول عند التنبيه مباشرة (هل التبكير يربح؟)
  B: أمر شراء معلّق فوق مستوى التفعيل — يُنفَّذ فقط إذا اخترق السعر المستوى (هل الانتظار أفضل؟)
وطريقة مقارنة BASE: أي سهم تحت 5$ قفز حجمه في نفس الوقت، بنفس قواعد الخروج.

قواعد الخروج: وقف عند مستوى الإبطال، هدف = target_r × المخاطرة، خروج إجباري 15:55 بتوقيت نيويورك.
المحاكاة على شموع دقيقة للسوق الكامل (SIP) جاءت بعد لحظة الدخول فقط:
  - فجوة تحت الوقف: الخروج بسعر الافتتاح (أسوأ من الوقف) — انزلاق حقيقي.
  - الوقف والهدف في نفس الشمعة: يُحسب الوقف (تحفظ).
  - شمعة التنفيذ في B: إذا لمست الوقف أيضاً نحسبها خسارة (تحفظ).
"""
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

import intraday as I

EXIT_HHMM = (15, 55)


def _tick_up(p: float) -> float:
    from hu.rules import tick_up
    return tick_up(p)


def exit_time(day: str) -> datetime:
    d = datetime.fromisoformat(day)
    return d.replace(hour=EXIT_HHMM[0], minute=EXIT_HHMM[1], tzinfo=I.ET)


def simulate(t: dict, bars: pd.DataFrame, cut_utc: datetime, slippage_pct: float, target_r: float) -> dict:
    """يعيد حالة الصفقة كاملة من البداية (حتمي). bars: شموع 1m (UTC index)."""
    out = {"status": t["status"] if t["status"] in ("PENDING", "OPEN") else t["status"]}
    created = datetime.fromisoformat(t["created_utc"])
    ex_t = exit_time(t["day"]).astimezone(I.UTC)
    end = min(cut_utc, ex_t)
    if bars is None or bars.empty:
        b = bars
    else:
        b = bars[(bars.index >= pd.Timestamp(created).ceil("min")) & (bars.index < pd.Timestamp(end))]
    kind = t["kind"]
    status = "PENDING" if kind in ("B", "PRE", "POST") else "OPEN"
    entry, stop, target = t.get("entry"), t["stop"], t.get("target")
    entry_utc = t.get("entry_utc")
    exit_px = exit_utc = None
    note = ""
    if b is not None and not b.empty:
        for ts, r in b.iterrows():
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            if status == "PENDING":
                if h >= t["trigger"]:
                    fill = max(o, t["trigger"]) * (1 + slippage_pct / 100)
                    entry, entry_utc = _tick_up(fill), ts.isoformat()
                    risk = entry - stop
                    if risk <= 0:
                        status, note = "CANCELLED", "التنفيذ فوق مستوى لا يترك مسافة للوقف"
                        break
                    target = _tick_up(entry + target_r * risk)
                    status = "OPEN"
                    if l <= stop:     # لا نعرف الترتيب داخل الشمعة: نفترض الأسوأ
                        exit_px, exit_utc, status, note = stop, ts.isoformat(), "STOP", "تنفيذ ثم وقف في نفس الدقيقة (افتراض متحفظ)"
                        break
                    continue
                if l <= stop:
                    status, note, exit_utc = "CANCELLED", "أُبطلت الفكرة قبل الاختراق", ts.isoformat()
                    break
                continue
            # OPEN
            if o <= stop:
                exit_px, exit_utc, status, note = o, ts.isoformat(), "STOP", "فجوة تحت الوقف — خروج بسعر الافتتاح"
                break
            if l <= stop:
                exit_px, exit_utc, status = stop, ts.isoformat(), "STOP"
                if h >= target:
                    note = "الوقف والهدف في نفس الدقيقة — حُسب الوقف"
                break
            if h >= target:
                exit_px, exit_utc, status = max(o, target), ts.isoformat(), "TARGET"
                break
    if status in ("OPEN", "PENDING") and cut_utc >= ex_t:
        if status == "OPEN":
            last = b.iloc[-1] if b is not None and not b.empty else None
            exit_px = float(last["close"]) if last is not None else entry
            exit_utc, status, note = ex_t.isoformat(), "TIME", "خروج بنهاية المدة (قبل الإغلاق بـ5 دقائق)"
        else:
            status, note = "EXPIRED", "لم يخترق المستوى حتى نهاية اليوم"
    out.update(status=status, entry=entry, entry_utc=entry_utc, target=target, note=note,
               exit=exit_px, exit_utc=exit_utc)
    if exit_px is not None and entry:
        sh = int(t["shares"])
        out["pnl"] = round((exit_px - entry) * sh, 2)
        out["r_mult"] = round((exit_px - entry) / (entry - stop), 3) if entry > stop else None
        out["pct"] = round((exit_px / entry - 1) * 100, 3)
    return out


def alert_events(alert: dict, bars: pd.DataFrame, cut_utc: datetime) -> dict:
    """متى تفعّلت الفكرة (اختراق المستوى) أو أُبطلت — من شموع جاءت بعد التنبيه فقط."""
    created = datetime.fromisoformat(alert["alert_utc"])
    if alert.get("eval_from"):                       # قوائم ما قبل/بعد الجلسة: تُقاس من الجلسة النظامية فقط
        created = max(created, datetime.fromisoformat(alert["eval_from"]))
    end = min(cut_utc, exit_time(alert["day"]).astimezone(I.UTC))
    if bars is None or bars.empty:
        return {}
    b = bars[(bars.index >= pd.Timestamp(created).ceil("min")) & (bars.index < pd.Timestamp(end))]
    for ts, r in b.iterrows():
        hit_t, hit_i = float(r["high"]) >= alert["trigger"], float(r["low"]) <= alert["invalidation"]
        if hit_i:   # في نفس الدقيقة نعتبر الإبطال (تحفظ)
            return {"state": "INVALIDATED", "at": ts.isoformat()}
        if hit_t:
            return {"state": "ACTIVATED", "at": ts.isoformat(),
                    "lead_min": round((ts.to_pydatetime() - created).total_seconds() / 60, 1)}
    if cut_utc >= exit_time(alert["day"]).astimezone(I.UTC):
        return {"state": "EXPIRED", "at": end.isoformat()}
    return {}


def summary(trades: List[dict]) -> dict:
    closed = [t for t in trades if t.get("pnl") is not None]
    n = len(closed)
    if not n:
        return {"trades": 0, "open": sum(1 for t in trades if t["status"] in ("OPEN", "PENDING"))}
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(closed, key=lambda x: x.get("exit_utc") or ""):
        eq += t["pnl"]; peak = max(peak, eq); dd = min(dd, eq - peak)
    rs = [t["r_mult"] for t in closed if t.get("r_mult") is not None]
    return {
        "trades": n,
        "open": sum(1 for t in trades if t["status"] in ("OPEN", "PENDING")),
        "not_filled": sum(1 for t in trades if t["status"] in ("EXPIRED", "CANCELLED")),
        "win_rate": round(len(wins) / n, 3),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
        "avg_pct": round(sum(t["pct"] for t in closed) / n, 2),
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "total_pnl": round(sum(t["pnl"] for t in closed), 2),
        "max_drawdown": round(dd, 2),
    }
