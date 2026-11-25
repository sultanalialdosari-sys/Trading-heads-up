# -*- coding: utf-8 -*-
"""محرك التحقق: تقسيم الزمن، النوافذ المتدحرجة، تقرير الفرضية، والحكم.

الحكم (على فترة الاختبار 2023+ فقط، بالسيناريو الأساسي):
  candidate     كل الشروط: عينة كافية، متوسط R > 0، المتشائم ≥ 0، q (BH) < alpha، الدخول العشوائي p < alpha،
                يبقى موجباً بعد حذف أفضل 5 صفقات، ونصف السنوات على الأقل موجبة.
  reject        متوسط R ≤ 0، أو p > 0.5 (لا شيء يُرى).
  inconclusive  غير ذلك — غالباً عينة صغيرة أو شرط واحد ناقص. لا تُعامل كنجاح.
candidate لا يعني تداولاً حقيقياً: يعني مراقبة ورقية حية 3 أشهر على الأقل.
"""
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from bt import stats as S
from bt.registry import Registry
from hu.config import AppConfig

SCEN = ("optimistic", "base", "pessimistic")


# ====================================================================== الزمن
def split(df: pd.DataFrame, app: AppConfig, trading_days: Optional[List[date]] = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """تدريب (بعد حذف أيام العزل من نهايته) واختبار. df يحتوي عمود day (date)."""
    v = app.validation
    days = pd.to_datetime(df["day"]).dt.date
    if trading_days is None:
        trading_days = sorted(set(days))
    cutoff = v.train_cutoff(trading_days)
    return df[(days >= v.train[0]) & (days <= cutoff)], df[days >= v.test_from]


def walk_forward_windows(start: date, end: date, train_months: int, test_months: int) -> List[Tuple[date, date, date, date]]:
    """نوافذ متدحرجة داخل فترة التدريب: (بداية تدريب، نهايته، بداية اختبار، نهايته)."""
    out, s = [], pd.Timestamp(start)
    while True:
        tr_end = s + pd.DateOffset(months=train_months) - pd.Timedelta(days=1)
        te_end = tr_end + pd.DateOffset(months=test_months)
        if te_end.date() > end:
            break
        out.append((s.date(), tr_end.date(), (tr_end + pd.Timedelta(days=1)).date(), te_end.date()))
        s = s + pd.DateOffset(months=test_months)
    return out


def param_stability(best_per_window: List[dict]) -> dict:
    """هل أفضل معاملات النافذة تتغير كثيراً؟ تغير كبير = الميزة غالباً وهم."""
    if len(best_per_window) < 2:
        return {"windows": len(best_per_window), "changes": 0, "share_changed": None}
    keys = sorted({k for p in best_per_window for k in p})
    changes = sum(any(a.get(k) != b.get(k) for k in keys) for a, b in zip(best_per_window, best_per_window[1:]))
    return {"windows": len(best_per_window), "changes": changes, "share_changed": changes / (len(best_per_window) - 1)}


# ====================================================================== التقرير
@dataclass
class ValidationReport:
    hid: str
    period: Tuple[str, str]
    n_trades: int
    n_days: int
    expectancy_r: Dict[str, Optional[float]]
    win_rate: Optional[float]
    profit_factor: Optional[float]
    max_drawdown_r: Optional[float]
    max_consec_losses: int
    ci95_daily_r: Optional[Tuple[float, float]]
    p_bootstrap: Optional[float]
    p_random_entry: Optional[float]
    q_bh: Optional[float]
    deflated_sharpe: Optional[float]
    n_trials: int
    without_top5_r: Optional[float]
    by_year: Dict[str, dict]
    by_regime: Dict[str, dict]
    verdict: str = "inconclusive"
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def verdict(rep: ValidationReport, app: AppConfig) -> Tuple[str, List[str]]:
    v, base, pess = app.validation, rep.expectancy_r.get("base"), rep.expectancy_r.get("pessimistic")
    fails, why = [], []
    if base is None or rep.n_trades == 0:
        return "reject", ["لا صفقات"]
    if base <= 0:
        return "reject", [f"متوسط R بعد التكاليف {base:+.3f} ≤ 0"]
    if rep.p_bootstrap is not None and rep.p_bootstrap > 0.5:
        return "reject", [f"p = {rep.p_bootstrap:.2f}: لا يختلف عن الصفر"]
    checks = [
        (rep.n_trades >= v.min_trades, f"عدد الصفقات {rep.n_trades} < {v.min_trades}"),
        (rep.n_days >= v.min_days, f"أيام التداول {rep.n_days} < {v.min_days}"),
        (pess is not None and pess >= 0, f"السيناريو المتشائم {pess:+.3f} < 0" if pess is not None else "لا نتيجة متشائمة"),
        (rep.q_bh is not None and rep.q_bh < v.alpha, f"q بعد تصحيح تعدد المحاولات = {rep.q_bh} ≥ {v.alpha}"),
        (rep.p_random_entry is not None and rep.p_random_entry < v.alpha,
         f"الدخول العشوائي p = {rep.p_random_entry} ≥ {v.alpha} (أو لم يُحسب)"),
        (rep.without_top5_r is not None and rep.without_top5_r > 0, f"بعد حذف أفضل 5 صفقات: {rep.without_top5_r}"),
    ]
    yrs = [y["expectancy_r"] for y in rep.by_year.values()]
    checks.append((bool(yrs) and sum(x > 0 for x in yrs) >= len(yrs) / 2, "أقل من نصف السنوات موجبة"))
    for ok, msg in checks:
        if not ok:
            fails.append(msg)
    if fails:
        return "inconclusive", fails
    return "candidate", ["كل الشروط تحققت — المرحلة التالية: مراقبة ورقية حية 3 أشهر"]


def build_report(hid: str, trades: pd.DataFrame, app: AppConfig, registry: Optional[Registry] = None,
                 random_draws: Optional[Iterable[pd.Series]] = None,
                 regime: Optional[Callable[[date], str]] = None, sr_var_trials: Optional[float] = None) -> ValidationReport:
    """trades: أعمدة day و r_optimistic و r_base و r_pessimistic (صفقة لكل صف، R بعد التكاليف)."""
    t = trades.dropna(subset=["r_base"]).copy()
    t["day"] = pd.to_datetime(t["day"]).dt.date
    m = S.metrics(t["day"], t["r_base"])
    daily = S.r_by_day(t["day"], t["r_base"])
    v = app.validation
    p = S.block_bootstrap_p(daily, v.bootstrap.n, v.bootstrap.seed)
    pr = S.random_entry_p(daily, random_draws) if random_draws is not None else None
    n_trials = registry.n_trials if registry else 1
    pv = registry.test_pvalues() if registry else {}
    pv[hid] = p
    q = (S.benjamini_hochberg if v.multiple_testing == "benjamini_hochberg" else S.bonferroni)(pv).get(hid)
    rep = ValidationReport(
        hid=hid,
        period=(str(min(t["day"])) if len(t) else "", str(max(t["day"])) if len(t) else ""),
        n_trades=m["n_trades"], n_days=m["n_days"],
        expectancy_r={s: (float(t[f"r_{s}"].mean()) if f"r_{s}" in t and t[f"r_{s}"].notna().any() else None) for s in SCEN},
        win_rate=m["win_rate"], profit_factor=m["profit_factor"], max_drawdown_r=m["max_drawdown_r"],
        max_consec_losses=m["max_consec_losses"], ci95_daily_r=S.bootstrap_ci(daily, v.bootstrap.n, v.bootstrap.seed),
        p_bootstrap=p, p_random_entry=pr, q_bh=q,
        deflated_sharpe=S.deflated_sharpe(daily, max(1, n_trials), sr_var_trials), n_trials=n_trials,
        without_top5_r=S.without_top(t["r_base"], 5),
        by_year=S.by_group(t["day"], t["r_base"], lambda d: d.year),
        by_regime=S.by_group(t["day"], t["r_base"], regime) if regime else {},
    )
    rep.verdict, rep.reasons = verdict(rep, app)
    return rep


VERDICT_AR = {"candidate": "مرشحة للمراقبة الحية", "inconclusive": "غير محسومة", "reject": "مرفوضة"}


def to_markdown(rep: ValidationReport) -> str:
    f = lambda x, d=3: "—" if x is None else (f"{x:+.{d}f}" if isinstance(x, float) else str(x))  # noqa: E731
    u = lambda x, d=4: "—" if x is None else f"{x:.{d}f}"                                         # noqa: E731  بلا إشارة
    e = rep.expectancy_r
    lines = [
        f"## {rep.hid} — {VERDICT_AR[rep.verdict]}",
        "",
        f"الفترة {rep.period[0]} → {rep.period[1]} • {rep.n_trades} صفقة في {rep.n_days} يوم",
        "",
        "| المقياس | القيمة |", "| --- | --- |",
        f"| متوسط R (متفائل / أساسي / متشائم) | {f(e.get('optimistic'))} / {f(e.get('base'))} / {f(e.get('pessimistic'))} |",
        f"| نسبة الربح | {'—' if rep.win_rate is None else f'{rep.win_rate:.0%}'} |",
        f"| Profit factor | {u(rep.profit_factor, 2)} |",
        f"| أقصى تراجع (R) | {f(rep.max_drawdown_r, 1)} |",
        f"| أطول سلسلة خسائر | {rep.max_consec_losses} |",
        f"| مجال ثقة 95% لـ R اليومي | {'—' if not rep.ci95_daily_r else f'{rep.ci95_daily_r[0]:+.3f} إلى {rep.ci95_daily_r[1]:+.3f}'} |",
        f"| p (Bootstrap بالأيام) | {u(rep.p_bootstrap, 4)} |",
        f"| p (مقابل الدخول العشوائي) | {u(rep.p_random_entry, 4)} |",
        f"| q (بعد تصحيح {rep.n_trials} فرضية) | {u(rep.q_bh, 4)} |",
        f"| Deflated Sharpe | {u(rep.deflated_sharpe, 3)} |",
        f"| بعد حذف أفضل 5 صفقات | {f(rep.without_top5_r)} |",
        "",
        "حسب السنة: " + " • ".join(f"{y}: {f(v['expectancy_r'])} ({v['n']})" for y, v in sorted(rep.by_year.items())),
    ]
    if rep.by_regime:
        lines.append("حسب حالة السوق: " + " • ".join(f"{k}: {f(v['expectancy_r'])} ({v['n']})" for k, v in rep.by_regime.items()))
    lines += ["", "**الأسباب:**"] + [f"- {r}" for r in rep.reasons]
    return "\n".join(lines)
