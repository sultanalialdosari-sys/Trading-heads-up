# -*- coding: utf-8 -*-
"""تحليل نتائج research.py: يقرأ results.json وصفوف الدراسة داخل اليوم، ويكتب تقريراً عربياً.

القاعدة: نختار الإعدادات على سنوات التدريب فقط (≤2022)، ثم نحكم عليها مرة واحدة على سنوات الاختبار (≥2023).
لا نوصي بتغيير إلا إذا تحسن الاختبار بعينة كافية.
"""
import itertools
import json
import math
import os
import sys

import numpy as np
import pandas as pd

TRAIN_END = 2022


def stats(g: pd.DataFrame) -> dict:
    t = g.dropna(subset=["trade_R"])
    n = len(t)
    if n == 0:
        return {"n": 0}
    r = t.trade_R.clip(-3, 10)
    return {"n": n, "avg_R": round(float(r.mean()), 3), "win": round(float((r > 0).mean()), 3),
            "hit10": round(float(g.hit10_before5.mean()), 3), "pf": round(float(r[r > 0].sum() / -r[r < 0].sum()), 2) if (r < 0).any() else None,
            "se": round(float(r.std() / math.sqrt(n)), 3) if n > 1 else None}


def qualifies(x: pd.DataFrame, p: dict) -> pd.Series:
    m = (x.stage == "EARLY") & (x.dollar_volume >= p["min_dv"]) & (x.rvol >= p["min_rvol"]) & (x.above_vwap == 1)
    m &= x.dist_res_pct.between(0.5, p["near_res_max"])
    m &= x.day_change_pct.fillna(0) <= p["max_chg"]
    m &= x.tod >= p["tod_min"]
    if p["max_range_atr"] < 99:
        m &= x.range_atr <= p["max_range_atr"]
    if p["need_hl"]:
        m &= x.higher_lows == 1
    m &= x.score >= p["min_score"]
    return m


CURRENT = {"min_dv": 3e5, "min_rvol": 2.0, "near_res_max": 5.0, "max_chg": 25.0, "tod_min": 600,
           "max_range_atr": 99, "need_hl": False, "min_score": 65}
GRID = {"min_dv": [3e5, 1e6], "min_rvol": [1.5, 2.0, 3.0], "near_res_max": [3.0, 5.0, 8.0], "max_chg": [15.0, 25.0, 50.0],
        "tod_min": [600, 630, 690], "max_range_atr": [3.0, 99], "need_hl": [False, True], "min_score": [55, 65, 75]}


def buckets(x: pd.DataFrame, col: str, edges) -> list:
    out = []
    tr, te = x[x.year <= TRAIN_END], x[x.year > TRAIN_END]
    for lo, hi in zip(edges[:-1], edges[1:]):
        a, b = tr[(tr[col] >= lo) & (tr[col] < hi)], te[(te[col] >= lo) & (te[col] < hi)]
        fmt = lambda v: "∞" if v >= 1e8 else (f"{v / 1e6:g}M" if v >= 1e5 else f"{v:g}")
        out.append({"col": col, "range": f"{fmt(lo)}–{fmt(hi)}", "train": stats(a), "test": stats(b)})
    return out


def main(res_path: str, rows_path: str, out_md: str):
    res = json.load(open(res_path, encoding="utf-8"))
    L = ["# نتائج الدراسة التاريخية — أسهم أمريكية تحت 5$", "",
         f"الفترة: من {res.get('start')} حتى {res.get('generated_utc', '')[:10]} • تدريب ≤ {TRAIN_END} • اختبار ≥ {TRAIN_END + 1}",
         f"طلبات Alpaca: {res.get('api_calls')} • المدة {res.get('minutes')} دقيقة", ""]
    d = res.get("daily") or {}
    findings = {}
    if d:
        L += ["## 1) الدراسة اليومية: ما الذي يسبق قفزة اليوم التالي؟",
              f"صفوف (سهم × يوم): {int(d['rows']):,} من {int(d['symbols']):,} سهماً (مدرجة ومشطوبة).",
              f"- نسبة الأساس لأن يرتفع السهم 20%+ في اليوم التالي (من الإغلاق إلى قمة الغد): تدريب {d['base']['y_up20_next']['train']:.1%} • اختبار {d['base']['y_up20_next']['test']:.1%}",
              f"- نسبة الأساس لـ 30%+ خلال 5 أيام: تدريب {d['base']['y_up30_5d']['train']:.1%} • اختبار {d['base']['y_up30_5d']['test']:.1%}",
              f"- متوسط عائد اليوم التالي (إغلاق لإغلاق): تدريب {d['r1_mean']['train']:+.2%} • اختبار {d['r1_mean']['test']:+.2%}", ""]
        lt = pd.DataFrame(d["lift_up20_next"])
        for c_ in ("n_train", "n_test", "rate_train", "rate_test", "lift_train", "lift_test", "r1_test_mean", "r1_test_median"):
            if c_ in lt:
                lt[c_] = pd.to_numeric(lt[c_], errors="coerce")
        if len(lt):
            lt["consistent"] = (lt.lift_train >= 1.3) & (lt.lift_test >= 1.3)
            lt["neg"] = (lt.lift_train <= 0.7) & (lt.lift_test <= 0.7)
            good = lt[lt.consistent].sort_values("lift_test", ascending=False).head(15)
            bad = lt[lt.neg].sort_values("lift_test").head(10)
            L += ["### مؤشرات رفعت احتمال القفزة (ثابتة في التدريب والاختبار، رفع ≥ 1.3×)", "",
                  "| المؤشر | النطاق | الرفع تدريب | الرفع اختبار | العدد اختبار | متوسط عائد الغد (اختبار) |", "|---|---|---|---|---|---|"]
            L += [f"| {r.feature} | {r.bucket} | {r.lift_train}× | {r.lift_test}× | {r.n_test:,} | {r.r1_test_mean:+.2%} |" for r in good.itertuples()]
            L += ["", "### مؤشرات خفّضت الاحتمال (≤ 0.7×)", "", "| المؤشر | النطاق | الرفع تدريب | الرفع اختبار |", "|---|---|---|---|"]
            L += [f"| {r.feature} | {r.bucket} | {r.lift_train}× | {r.lift_test}× |" for r in bad.itertuples()]
            L.append("")
            findings["daily_good"] = good.to_dict("records")
        for t in ("y_up20_next", "y_up30_5d"):
            m = d.get(f"model_{t}")
            if m:
                m = {k: (float(v) if isinstance(v, str) and k != "importance" else v) for k, v in m.items()}
                L += [f"### نموذج يجمع كل المؤشرات ({'قفزة 20% غداً' if t == 'y_up20_next' else '30% خلال 5 أيام'}) — على سنوات لم يرها",
                      f"- AUC = {m['auc_test']} (0.5 = صدفة)",
                      f"- أعلى 1% من التوقعات: {m['top_1%']:.1%} مقابل الأساس {m['base_test']:.1%} • أعلى 5%: {m['top_5%']:.1%}",
                      f"- متوسط عائد الغد لأعلى 1%: {m['top_1%_r1']:+.2%}، لأعلى 5%: {m['top_5%_r1']:+.2%}",
                      "- أهم المؤشرات: " + "، ".join(f"{f} ({v})" for f, v in m["importance"][:8]), ""]
    if os.path.exists(rows_path):
        x = pd.read_csv(rows_path)
        tr, te = x[x.year <= TRAIN_END], x[x.year > TRAIN_END]
        L += ["## 2) داخل اليوم: قواعد النظام الحالية على عينة عشوائية", "",
              f"صفوف التقييم (سهم × وقت كل 15 دقيقة): {len(x):,} من {x[['symbol', 'date']].drop_duplicates().shape[0]:,} يوم-سهم.",
              "الصفقة = صفقة A في النظام: دخول عند الإشارة (+0.5% انزلاق)، وقف عند الإبطال، هدف 2R، خروج 15:55.", "",
              "| المجموعة | صفقات تدريب | متوسط R تدريب | ربح تدريب | صفقات اختبار | متوسط R اختبار | ربح اختبار | وصل +10% قبل −5% (اختبار) |",
              "|---|---|---|---|---|---|---|---|"]
        def line(name, a, b):
            s1, s2 = stats(a), stats(b)
            return (f"| {name} | {s1['n']:,} | {s1.get('avg_R', '—')} | {s1.get('win', '—')} | {s2['n']:,} | {s2.get('avg_R', '—')} | "
                    f"{s2.get('win', '—')} | {s2.get('hit10', '—')} |")
        L.append(line("كل الأوقات (الأساس)", tr, te))
        L.append(line("مرحلة مبكرة فقط", tr[tr.stage == "EARLY"], te[te.stage == "EARLY"]))
        L.append(line("**يستوفي قواعد النظام الحالية**", tr[tr.qualifies == 1], te[te.qualifies == 1]))
        for lo, hi in ((0, 50), (50, 65), (65, 75), (75, 101)):
            L.append(line(f"الدرجة {lo}–{hi}", tr[(tr.score >= lo) & (tr.score < hi)], te[(te.score >= lo) & (te.score < hi)]))
        L.append("")
        e = x[x.stage == "EARLY"]
        bk = []
        for col, edges in (("rvol", [0, 1, 1.5, 2, 3, 5, 1e9]), ("range_atr", [0, 2, 3, 4, 6, 1e9]),
                           ("dist_res_pct", [0, 0.5, 2, 3, 5, 8, 1e9]), ("day_change_pct", [-100, 0, 5, 15, 25, 50, 1e9]),
                           ("rsi", [0, 45, 55, 70, 80, 101]), ("tod", [600, 660, 720, 780, 840, 900, 961]),
                           ("dollar_volume", [0, 3e5, 1e6, 5e6, 1e12]), ("price", [0, 0.5, 1, 2, 3, 5.5])):
            bk += buckets(e, col, edges)
        for col in ("higher_lows", "above_vwap", "ema_ok", "macd_rising", "rvol_rising"):
            bk += buckets(e, col, [0, 0.5, 1.5])
        L += ["### كل مؤشر وحده (بين الصفوف في المرحلة المبكرة)", "",
              "| المؤشر | النطاق | صفقات تدريب | R تدريب | صفقات اختبار | R اختبار |", "|---|---|---|---|---|---|"]
        for b in bk:
            if b["train"]["n"] >= 50 and b["test"]["n"] >= 20:
                L.append(f"| {b['col']} | {b['range']} | {b['train']['n']:,} | {b['train']['avg_R']} | {b['test']['n']:,} | {b['test']['avg_R']} |")
        L.append("")
        # بحث في الإعدادات على التدريب فقط
        keys = list(GRID)
        best = []
        for vals in itertools.product(*[GRID[k] for k in keys]):
            p = dict(zip(keys, vals))
            s = stats(tr[qualifies(tr, p)])
            if s["n"] >= 150:
                best.append((s["avg_R"] - (s["se"] or 0), s, p))     # نعاقب العينة الصغيرة بخطئها المعياري
        best.sort(key=lambda z: -z[0])
        cur_tr, cur_te = stats(tr[qualifies(tr, CURRENT)]), stats(te[qualifies(te, CURRENT)])
        L += ["### البحث عن إعدادات أفضل (اختيار على التدريب فقط، ثم حكم واحد على الاختبار)", "",
              f"- الإعدادات الحالية: تدريب {cur_tr} • اختبار {cur_te}"]
        top = best[:5]
        for k, (_, s, p) in enumerate(top, 1):
            st_te = stats(te[qualifies(te, p)])
            L.append(f"- مرشح {k}: {p} → تدريب R {s['avg_R']} (n={s['n']}) • **اختبار R {st_te.get('avg_R')} (n={st_te['n']})**")
        findings["grid_top"] = [{"params": p, "train": s, "test": stats(te[qualifies(te, p)])} for _, s, p in top]
        findings["current"] = {"train": cur_tr, "test": cur_te}
        L.append("")
    L += ["## حدود الدراسة", "- البيانات من 2016 فقط (بيانات Alpaca المجانية)، لا 20 سنة.",
          "- لا يوجد Float تاريخي ولا أخبار تاريخية في هذه النسخة، فأثر «الخبر» لم يُختبر هنا.",
          "- المحاكاة على شموع 5 دقائق: الوقف يُحسب أولاً إذا تعارض مع الهدف في نفس الشمعة (تحفظ).",
          "- الأسهم المشطوبة مشمولة بقدر ما تحتفظ Alpaca ببياناتها؛ قد يبقى بعض انحياز الناجين.",
          "- الأداء السابق لا يضمن المستقبل. هذه دراسة وليست توصية."]
    open(out_md, "w", encoding="utf-8").write("\n".join(L))
    json.dump(findings, open(out_md.replace(".md", "_findings.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("\n".join(L))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "research_report.md")
