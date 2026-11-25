# -*- coding: utf-8 -*-
"""نسخة سريعة من bt.execution.simulate تعمل على مصفوفات يوم واحد (للبحث على مئات آلاف الصفقات).

نفس القواعد حرفياً — والاختبارات تقارنها بالنسخة المرجعية على حالات عشوائية:
دخول على افتتاح الشمعة j، الوقف أولاً، الفجوات بالافتتاح، الهدف يحتاج تيكاً، خروج الوقت، التوقفات،
لا دخول في أول N شموع بعد استئناف، أرضية السيناريو، الرسوم، التقريب ضدنا.
"""
import math
from typing import List, Optional, Sequence

Result = tuple   # (r_net, r_gross, reason, cost_pct, halted, entry_px, exit_px)   أو (None, ..., skip_reason, ...)


def _tick(p, t_lt1, t_ge1):
    return t_lt1 if p < 1.0 else t_ge1


def _buy(p, t_lt1, t_ge1):
    t = _tick(p, t_lt1, t_ge1)
    return round(math.ceil(p / t - 1e-9) * t, 6)


def _sell(p, t_lt1, t_ge1):
    t = _tick(p, t_lt1, t_ge1)
    return round(math.floor(p / t + 1e-9) * t, 6)


def halt_resumes(starts_min: Sequence[int], step: int, halt_gap_min: int, prior: int = 6) -> List[bool]:
    """starts_min = بداية كل شمعة بالدقائق. True للشمعة الأولى بعد توقف (فجوة ≥ halt_gap_min بعد 6 شموع متتالية)."""
    n = len(starts_min)
    out = [False] * n
    for i in range(prior, n):
        if starts_min[i] - starts_min[i - 1] - step >= halt_gap_min:
            if all(starts_min[k] - starts_min[k - 1] == step for k in range(i - prior + 1, i)):
                out[i] = True
    return out


def sim(o, h, l, c, starts_min, j: int, stop: float, target: float, time_exit_min: int, resume: Sequence[bool],
        spread_half: float, slip_in: float, slip_out: float, floor: float, qty: int,
        t_lt1: float, t_ge1: float, sec_per_million: float, taf_per_share: float, taf_max: float,
        no_entry_after_halt: int = 2) -> Result:
    n = len(o)
    # لا دخول في أول N شموع بعد آخر استئناف
    while j < n and no_entry_after_halt:
        last = max((k for k in range(0, j + 1) if resume[k]), default=None)
        if last is None or j - last >= no_entry_after_halt:
            break
        j += 1
    if j >= n or starts_min[j] >= time_exit_min:
        return (None, None, "no_bars_before_time_exit", None, False, None, None)
    ref_in = o[j]
    e = _buy(ref_in * (1 + spread_half + slip_in), t_lt1, t_ge1)
    if e <= stop:
        return (None, None, "gap_below_stop", None, False, None, None)
    if e >= target:
        return (None, None, "gap_above_target", None, False, None, None)
    up = _tick(target, t_lt1, t_ge1)
    halted, reason, ref_out, x = False, None, None, None

    def mkt(p):
        return _sell(p * (1 - spread_half - slip_out), t_lt1, t_ge1)

    for i in range(j, n):
        if resume[i] and i > j:
            halted = True
        if starts_min[i] >= time_exit_min:
            reason, ref_out = "time", o[i]; x = mkt(ref_out); break
        if i > j and o[i] <= stop:
            reason, ref_out = ("halt_resume" if resume[i] else "gap_stop"), o[i]; x = mkt(ref_out); break
        if i > j and o[i] >= target:
            reason, ref_out = "gap_target", o[i]; x = _sell(o[i], t_lt1, t_ge1); break
        if l[i] <= stop:
            reason, ref_out = "stop", stop; x = mkt(stop); break
        if h[i] >= target + up:
            reason, ref_out, x = "target", target, target; break
    if reason is None:
        reason, ref_out = "data_end", c[n - 1]; x = mkt(ref_out)
    realized = ((e - ref_in) + (ref_out - x)) / ref_in
    if realized < floor - 1e-9:
        x = _sell(x - (floor - realized) * ref_in, t_lt1, t_ge1)
    sec = math.ceil(x * qty * sec_per_million / 1e6 * 100) / 100 if sec_per_million else 0.0
    taf = min(taf_max, math.ceil(qty * taf_per_share * 100) / 100) if taf_per_share else 0.0
    fees = round(sec + taf, 2)
    risk = e - stop
    r_net = (x - e - fees / qty) / risk
    risk_g = ref_in - stop
    r_gross = (ref_out - ref_in) / risk_g if risk_g > 0 else None
    cost = ((e - ref_in) + (ref_out - x)) / ref_in
    return (r_net, r_gross, reason, cost, halted, e, x)
