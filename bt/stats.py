# -*- coding: utf-8 -*-
"""الإحصاء: مقاييس الصفقات، Bootstrap بالكتل اليومية، الدخول العشوائي، Benjamini–Hochberg، Deflated Sharpe.

الوحدة الأساسية: R لكل صفقة (بعد التكاليف). الصفقات في نفس اليوم مترابطة (نفس مزاج السوق)،
لذلك كل اختبار إحصائي يعمل على مجموع R لكل يوم، لا على الصفقة المفردة.
"""
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Callable, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

N = NormalDist()
EULER = 0.5772156649015329


# ====================================================================== المقاييس
def r_by_day(days: Sequence, r: Sequence[float]) -> pd.Series:
    s = pd.Series(list(r), index=pd.Index(list(days), name="day"), dtype="float64").dropna()
    return s.groupby(level=0).sum().sort_index()


def metrics(days: Sequence, r: Sequence[float]) -> dict:
    x = pd.Series(list(r), dtype="float64").dropna()
    if len(x) == 0:
        return {"n_trades": 0, "n_days": 0, "expectancy_r": None, "win_rate": None, "profit_factor": None,
                "max_drawdown_r": None, "max_consec_losses": 0, "total_r": 0.0}
    wins, losses = x[x > 0], x[x < 0]
    daily = r_by_day(days, r)
    eq = daily.cumsum()
    dd = float((eq - eq.cummax().clip(lower=0)).min())
    streak = best = 0
    for v in x:
        streak = streak + 1 if v < 0 else 0
        best = max(best, streak)
    return {"n_trades": int(len(x)), "n_days": int(len(daily)), "expectancy_r": float(x.mean()),
            "win_rate": float((x > 0).mean()),
            "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
            "max_drawdown_r": dd, "max_consec_losses": int(best), "total_r": float(x.sum())}


def without_top(r: Sequence[float], k: int = 5) -> Optional[float]:
    """المتوسط بعد حذف أفضل k صفقات: إذا انقلب الربح خسارة فهو ضربة حظ لا نمط."""
    x = sorted((v for v in r if v is not None and not math.isnan(v)), reverse=True)
    rest = x[k:]
    return float(np.mean(rest)) if rest else None


def by_group(days: Sequence, r: Sequence[float], label: Callable) -> Dict[str, dict]:
    df = pd.DataFrame({"day": list(days), "r": list(r)}).dropna()
    df["g"] = [label(d) for d in df["day"]]
    return {str(g): {"n": int(len(x)), "expectancy_r": float(x["r"].mean())} for g, x in df.groupby("g")}


# ====================================================================== Bootstrap
def block_bootstrap_p(daily: pd.Series, n: int = 10_000, seed: int = 7) -> Optional[float]:
    """فرضية العدم: متوسط R اليومي ≤ 0. نُمركز الأيام على الصفر ثم نعيد السحب بالأيام كاملة.
    p = نسبة العوالم الصفرية التي يبلغ فيها المتوسط ما رأيناه أو أكثر (طرف واحد)."""
    v = np.asarray(daily, dtype="float64")
    v = v[~np.isnan(v)]
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    obs = v.mean()
    null = v - obs
    sims = rng.choice(null, size=(n, len(v)), replace=True).mean(axis=1)
    return float((np.sum(sims >= obs) + 1) / (n + 1))


def bootstrap_ci(daily: pd.Series, n: int = 10_000, seed: int = 7, level: float = 0.95) -> Optional[tuple]:
    v = np.asarray(daily, dtype="float64")
    v = v[~np.isnan(v)]
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    sims = rng.choice(v, size=(n, len(v)), replace=True).mean(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(sims, a)), float(np.quantile(sims, 1 - a))


# ====================================================================== الدخول العشوائي
def random_entry_p(signal_daily: pd.Series, random_daily_draws: Iterable[pd.Series]) -> Optional[float]:
    """كل سحبة = نفس الأسهم ونفس الأيام ونفس الوقف/الهدف لكن بدخول في دقيقة عشوائية.
    p = نسبة السحبات العشوائية التي يبلغ متوسطها اليومي متوسط الإشارة أو أكثر."""
    obs = float(np.nanmean(signal_daily)) if len(signal_daily) else None
    means = [float(np.nanmean(d)) for d in random_daily_draws if len(d)]
    if obs is None or not means:
        return None
    return float((sum(m >= obs for m in means) + 1) / (len(means) + 1))


# ====================================================================== تعدد الاختبارات
def benjamini_hochberg(pvals: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """q-value لكل فرضية. الفرضيات بلا p (لم تُختبر) لا تُحسب لكنها تبقى في السجل."""
    items = [(k, p) for k, p in pvals.items() if p is not None]
    m = len(items)
    out: Dict[str, Optional[float]] = {k: None for k in pvals}
    if not m:
        return out
    items.sort(key=lambda kv: kv[1])
    q, prev = [0.0] * m, 1.0
    for i in range(m - 1, -1, -1):
        prev = min(prev, items[i][1] * m / (i + 1))
        q[i] = prev
    for (k, _), v in zip(items, q):
        out[k] = min(1.0, v)
    return out


def bonferroni(pvals: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    m = sum(p is not None for p in pvals.values())
    return {k: (None if p is None else min(1.0, p * m)) for k, p in pvals.items()}


# ====================================================================== Deflated Sharpe
def sharpe(daily: pd.Series) -> Optional[float]:
    v = np.asarray(daily, dtype="float64")
    v = v[~np.isnan(v)]
    if len(v) < 3 or v.std(ddof=1) == 0:
        return None
    return float(v.mean() / v.std(ddof=1))


def deflated_sharpe(daily: pd.Series, n_trials: int, sr_var_trials: Optional[float] = None) -> Optional[float]:
    """احتمال أن Sharpe الحقيقي > 0 بعد احتساب عدد المحاولات (Bailey & López de Prado 2014).
    sr_var_trials = تباين Sharpe بين كل المحاولات؛ إن لم يُعرف نستخدم 1/T (تقريب متحفظ)."""
    v = np.asarray(daily, dtype="float64")
    v = v[~np.isnan(v)]
    sr = sharpe(v)
    T = len(v)
    if sr is None or T < 3:
        return None
    var = sr_var_trials if sr_var_trials is not None else 1.0 / T
    n = max(1, int(n_trials))
    if n == 1:
        sr0 = 0.0
    else:
        sr0 = math.sqrt(var) * ((1 - EULER) * N.inv_cdf(1 - 1 / n) + EULER * N.inv_cdf(1 - 1 / (n * math.e)))
    z = (v - v.mean()) / v.std(ddof=1)
    skew = float(np.mean(z ** 3))
    kurt = float(np.mean(z ** 4))
    den = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if den <= 0:
        return None
    return float(N.cdf((sr - sr0) * math.sqrt(T - 1) / math.sqrt(den)))


# ====================================================================== الحساسية
@dataclass(frozen=True)
class Sensitivity:
    param: str
    base: float
    low: Optional[float]           # النتيجة عند −20%
    high: Optional[float]          # النتيجة عند +20%
    flips: bool                    # تغيّرت إشارة النتيجة؟


def sensitivity(evaluate: Callable[[dict], Optional[float]], params: dict, frac: float = 0.2) -> List[Sensitivity]:
    """قمة حادة في الشبكة = ضجيج: تغيير أي معامل ±20% يجب ألا يقلب إشارة النتيجة."""
    base = evaluate(params)
    out = []
    for k, v in params.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        lo = evaluate({**params, k: v * (1 - frac)})
        hi = evaluate({**params, k: v * (1 + frac)})
        flips = base is not None and any(x is not None and (x > 0) != (base > 0) for x in (lo, hi))
        out.append(Sensitivity(k, v, lo, hi, flips))
    return out
