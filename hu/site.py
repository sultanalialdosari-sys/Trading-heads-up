# -*- coding: utf-8 -*-
"""يولّد موقعاً ثابتاً (docs/index.html) من قاعدة البيانات — يُنشر مجاناً على GitHub Pages.

التصميم: «مكتب المراقبة» بخلفية بيضاء — رأس بالرصيد الافتراضي، منحنى الربح، مدار الوكلاء (مراحل الفحص
بأرقام آخر تشغيل)، بطاقة الصفقة المميزة، وسجل الجلسة المباشر. كل الأرقام حقيقية من قاعدة البيانات.
"""
import html
import json
import os
from datetime import datetime, timedelta
from typing import List, Optional

import intraday as I
from hu import charts as CH
from hu import equity as EQ
from hu import halts as HL
from hu import rules as RL
from hu import timefmt as TF
from hu import trades as TR

E = lambda x: html.escape("" if x is None else str(x))
LTR = lambda x: "\u2066" + str(x) + "\u2069"   # عزل الأرقام داخل النص العربي حتى لا تنقلب (+1.62R)
GREEN, RED, BLUE, AMBER, GREY = "var(--g)", "var(--r)", "var(--b)", "var(--a)", "var(--mut)"
STATE_AR = {"OPEN": ("مفتوحة", AMBER), "ACTIVATED": ("تفعّلت", GREEN), "INVALIDATED": ("أُلغيت", RED),
            "EXPIRED": ("انتهت بلا حسم", GREY)}
TSTAT = {"OPEN": "مفتوحة", "PENDING": "معلّقة", "STOP": "وقف", "TARGET": "هدف", "TIME": "نهاية المدة",
         "EXPIRED": "لم تُنفّذ", "CANCELLED": "أُلغيت قبل التنفيذ"}
KIND = {"A": "A عند التنبيه", "B": "B عند الاختراق", "BASE": "مقارنة", "PRE": "قائمة قبل الافتتاح", "POST": "قائمة بعد الإغلاق"}
PHASE_LABEL = {"PRE": "ما قبل الافتتاح", "POST": "بعد الإغلاق", "REG": "الجلسة", None: "الجلسة"}
PARTS_MAX = [("الحجم", 30), ("التماسك", 35), ("الاتجاه", 20), ("الزخم", 5), ("الأخبار", 10)]


# ------------------------------------------------------------------ أدوات
def t_et(iso):
    if not iso:
        return "—"
    return TF.dh12(datetime.fromisoformat(iso).astimezone(I.RIYADH))   # بتوقيت الرياض، 12 ساعة


def t_sec(iso, precise):
    """وقت الصفقة: بالثانية إن كان التنفيذ محسوباً من صفقات وعروض SIP، وإلا بالدقيقة."""
    if not iso:
        return "—"
    d = datetime.fromisoformat(iso).astimezone(I.RIYADH)
    return TF.dh12(d, seconds=precise)


H5_FROM = "2026-11-27T17:29:41"   # وقت تسجيل الفرضية H5 — التقييم الرسمي على ما بعده فقط


def h5_html(trades) -> str:
    """متابعة الفرضية المسجلة H5: تنبيهات هادئة مقابل ساخنة مقابل الدخول العشوائي (للمراقبة — الحكم مرة واحدة)."""
    def grp(rows):
        r = [t["r_mult"] for t in rows if t["r_mult"] is not None]
        if not r:
            return "<td>0</td><td>—</td><td>—</td><td>—</td>"
        pnl = sum(t["pnl"] or 0 for t in rows if t["r_mult"] is not None)
        m = sum(r) / len(r)
        return (f"<td>{len(r)}</td><td class='mono' style='color:{col(m)}'>{m:+.2f}R</td>"
                f"<td>{sum(x > 0 for x in r) / len(r):.0%}</td><td class='mono' style='color:{col(pnl)}'>{money(pnl)}</td>")
    post = [t for t in trades if (t["created_utc"] or "") >= H5_FROM]
    pre = [t for t in trades if (t["created_utc"] or "") < H5_FROM]
    days = len({t["day"] for t in post if t["r_mult"] is not None})
    calm_n = sum(1 for t in post if t["kind"] in ("A", "B") and t.get("calm") == 1 and t["r_mult"] is not None)
    def rows(src):
        ab = [t for t in src if t["kind"] in ("A", "B")]
        return (f"<tr><td>هادئة (السهم لم ينفجر قبل التنبيه)</td>{grp([t for t in ab if t.get('calm') == 1])}</tr>"
                f"<tr><td>ساخنة</td>{grp([t for t in ab if t.get('calm') == 0])}</tr>"
                f"<tr><td>المقارنة العشوائية</td>{grp([t for t in src if t['kind'] == 'BASE'])}</tr>")
    head = "<tr><th>المجموعة</th><th>صفقات</th><th>متوسط R</th><th>نسبة الربح</th><th>المجموع</th></tr>"
    return (f"<div class='card'><h3>الفرضية H5 — هل التنبيهات «الهادئة» أفضل؟ (مسجلة مسبقاً)</h3>"
            "<p class='muted'>هادئة = حجم اليوم ≤ 1.61× المعتاد، وأكبر قفزة حجم ≤ 8.71×، والارتفاع من إغلاق الأمس ≤ 7.34%. "
            "الحدود مقفلة ولا تُعدّل. التنفيذ نفسه لم يتغير — هذا وسم للمقارنة فقط.</p>"
            f"<p><b>التقدم نحو الحكم:</b> {days}/40 يوم تداول • {calm_n}/100 صفقة هادئة. "
            "الأرقام هنا للمتابعة؛ الحكم مرة واحدة عند اكتمال الشرطين.</p>"
            f"<div class='tw'><table>{head}{rows(post)}</table></div>"
            f"<details><summary class='muted'>قبل التسجيل (استكشافي — منه جاءت الحدود، فلا يُعتبر دليلاً)</summary>"
            f"<div class='tw'><table>{head}{rows(pre)}</table></div></details></div>")


def fill_tag(t):
    src = t.get("fill_src")
    if src == "tick":
        return "<span class='tag t-green' title='محسوب من صفقات وعروض السوق الكامل بالثانية'>بالثانية</span>"
    if src == "bar":
        return "<span class='tag t-grey' title='تعذّرت بيانات الثانية: محسوب من شموع الدقيقة بافتراضات متحفظة'>بالدقيقة</span>"
    return "—"


def hm_et(iso):
    return "—" if not iso else TF.h12(datetime.fromisoformat(iso).astimezone(I.RIYADH))


def money(v):
    return "—" if v is None else f"{'+' if v > 0 else ''}{v:,.2f}$"


def col(v):
    return GREEN if (v or 0) > 0 else RED if (v or 0) < 0 else GREY


def pill(text, cls):
    return f"<span class='tag {cls}'>{E(text)}</span>"


AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


def calendar_html(trades: List[dict]) -> str:
    """تقويم يومي يتحدث مع كل صفقة: مجموع الربح ومجموع الخسارة وصافي الصفقات المغلقة، وسطر مستقل للصفقات
    المفتوحة بربحها/خسارتها غير المحققة (من آخر سعر تابعه النظام). المفتوحة لا تدخل في الصافي حتى تُغلق."""
    import calendar as _cal
    from collections import defaultdict
    days = defaultdict(lambda: {"win": 0.0, "loss": 0.0, "nw": 0, "nl": 0, "kinds": defaultdict(float),
                                "no": 0, "upnl": 0.0, "up_n": 0})
    for t in trades:
        if t.get("pnl") is None:
            if t.get("status") == "OPEN" and t.get("entry"):
                d = days[t["day"]]
                d["no"] += 1
                if t.get("last_px"):
                    d["upnl"] += (t["last_px"] - t["entry"]) * (t.get("shares") or 0)
                    d["up_n"] += 1
            continue
        d = days[t["day"]]
        if t["pnl"] > 0:
            d["win"] += t["pnl"]; d["nw"] += 1
        else:
            d["loss"] += t["pnl"]; d["nl"] += 1
        d["kinds"][t["kind"]] += t["pnl"]
    if not days:
        return "<div class='muted' style='padding:14px'>لا صفقات بعد.</div>"
    months = sorted({k[:7] for k in days}, reverse=True)
    wd = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة"]
    names = {"A": "A", "B": "B", "BASE": "مقارنة", "PRE": "قبل الافتتاح", "POST": "بعد الإغلاق"}
    out = []
    for mi, ym in enumerate(months):
        y, m = int(ym[:4]), int(ym[5:])
        tot = [v for k, v in days.items() if k.startswith(ym)]
        mw, ml = sum(v["win"] for v in tot), sum(v["loss"] for v in tot)
        cells = ""
        for week in _cal.Calendar(firstweekday=0).monthdayscalendar(y, m):
            if not any(week[:5]):
                continue                                # أسبوع بلا أيام تداول في هذا الشهر
            for dnum in week[:5]:                       # أيام التداول الأمريكية: الاثنين–الجمعة
                if not dnum:
                    cells += "<div class='cd empty'></div>"
                    continue
                key = f"{ym}-{dnum:02d}"
                v = days.get(key)
                if not v:
                    cells += f"<div class='cd'><span class='dn'>{dnum}</span></div>"
                    continue
                net = v["win"] + v["loss"]
                tip = " • ".join(f"{names.get(k, k)} {money(x)}" for k, x in v["kinds"].items())
                if v["no"]:
                    tip += (" • " if tip else "") + f"مفتوحة {v['no']}" + (f": غير محقق {money(v['upnl'])}" if v["up_n"] else "")
                closed = v["nw"] + v["nl"]
                opn = (f"<span class='co'><bdi dir='ltr' class='mono'>{'+' if v['upnl'] > 0 else ''}{v['upnl']:,.0f}$</bdi>"
                       f"<i> · {v['no']} مفتوحة</i></span>") if v["no"] else ""
                cells += (f"<div class='cd has {'pos' if net > 0 else 'neg' if net < 0 else ''}{' live' if v['no'] else ''}' title='{E(tip)}'>"
                          f"<span class='dn'>{dnum}</span>"
                          + (f"<span class='cw'><bdi dir='ltr' class='mono'>+{v['win']:,.0f}$</bdi><i> · {v['nw']} رابحة</i></span>"
                             f"<span class='cl'><bdi dir='ltr' class='mono'>{v['loss']:,.0f}$</bdi><i> · {v['nl']} خاسرة</i></span>" if closed else "")
                          + opn
                          + (f"<b class='cn'><bdi dir='ltr' class='mono' style='color:{col(net)}'>{'+' if net > 0 else ''}{net:,.2f}$</bdi></b>" if closed else "")
                          + "</div>")
        out.append(f"<div class='calm' data-i='{mi}'{'' if mi == 0 else ' hidden'}>"
                   f"<div class='calh'><button type='button' class='calp' {'disabled' if mi == len(months) - 1 else ''}>‹ السابق</button>"
                   f"<b>{AR_MONTHS[m - 1]} {y}</b>"
                   f"<button type='button' class='caln' {'disabled' if mi == 0 else ''}>التالي ›</button></div>"
                   f"<div class='calsum'>ربح الشهر <bdi dir='ltr' class='mono' style='color:{GREEN}'>+{mw:,.2f}$</bdi> • خسارة الشهر "
                   f"<bdi dir='ltr' class='mono' style='color:{RED}'>{ml:,.2f}$</bdi> • الصافي <bdi dir='ltr' class='mono' style='color:{col(mw + ml)}'>{'+' if mw + ml > 0 else ''}{mw + ml:,.2f}$</bdi>"
                   + (lambda no, up: f" • مفتوحة الآن {no} <bdi dir='ltr' class='mono' style='color:#d97706'>{'+' if up > 0 else ''}{up:,.2f}$</bdi> غير محقق" if no else "")(
                       sum(v["no"] for v in tot), sum(v["upnl"] for v in tot))
                   + "</div>"
                   f"<div class='calg'>{''.join(f'<div class=cwd>{w}</div>' for w in wd)}{cells}</div></div>")
    return "".join(out)


def table(head: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "<div class='empty'>لا شيء بعد.</div>"
    th = "".join(f"<th>{h}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='tw'><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>"


def sum_card(title: str, s: dict, accent: str) -> str:
    if not s.get("trades"):
        body = f"<div class='muted'>لا صفقات مغلقة بعد • مفتوحة {s.get('open', 0)}</div>"
    else:
        avg_r = "—" if s["avg_r"] is None else f"{s['avg_r']:+.2f}"
        body = (f"<div class='big mono' style='color:{col(s['total_pnl'])}'>{money(s['total_pnl'])}</div>"
                f"<div class='kv'><span>الصفقات</span><b>{s['trades']}</b></div>"
                f"<div class='kv'><span>نسبة الربح</span><b>{s['win_rate']:.0%}</b></div>"
                f"<div class='kv'><span>متوسط الربح / الخسارة</span><b>{money(s['avg_win'])} / {money(s['avg_loss'])}</b></div>"
                f"<div class='kv'><span>متوسط الصفقة</span><b>{s['avg_pct']:+.2f}%</b></div>"
                f"<div class='kv'><span>متوسط R</span><b>{avg_r}</b></div>"
                f"<div class='kv'><span>أكبر تراجع</span><b>{money(s['max_drawdown'])}</b></div>"
                f"<div class='kv'><span>لم تُنفّذ / مفتوحة</span><b>{s.get('not_filled', 0)} / {s['open']}</b></div>")
    return f"<div class='card sc' style='--ac:{accent}'><div class='ct'>{E(title)}</div>{body}</div>"


# ------------------------------------------------------------------ منحنى الربح
def equity_html(trades: list) -> str:
    closed = sorted([t for t in trades if t.get("pnl") is not None and t.get("exit_utc")], key=lambda t: t["exit_utc"])
    series = {"A": [], "B": [], "BASE": []}
    if not closed:
        return ("<div class='eq'><svg viewBox='0 0 1000 200' preserveAspectRatio='none'>"
                "<line x1='0' y1='150' x2='1000' y2='150' class='eqz'/></svg>"
                "<div class='eqempty'>يظهر المنحنى بعد أول صفقة افتراضية مغلقة</div></div>")
    run = {"A": 0.0, "B": 0.0, "BASE": 0.0}
    for t in closed:
        run[t["kind"]] = run.get(t["kind"], 0.0) + t["pnl"]
        for k in series:
            series[k].append(run[k])
    vals = [0.0] + [v for s in series.values() for v in s]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad
    n = len(closed)
    X = lambda i: 1000 * i / n
    Y = lambda v: 200 - 200 * (v - lo) / (hi - lo)

    def path(s):  # منحنى درجي: يبدأ من الصفر ويقفز عند كل إغلاق
        d, prev = [f"M0,{Y(0):.1f}"], 0.0
        for i, v in enumerate(s):
            d.append(f"L{X(i + 1) - 1000 / n * 0.35:.1f},{Y(prev):.1f} L{X(i + 1):.1f},{Y(v):.1f}")
            prev = v
        return " ".join(d)

    a = path(series["A"])
    area = a + f" L1000,{Y(series['A'][-1]):.1f} L1000,200 L0,200 Z"
    svg = (f"<svg viewBox='0 0 1000 200' preserveAspectRatio='none'>"
           f"<defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'><stop offset='0' style='stop-color:var(--g);stop-opacity:.22'/>"
           f"<stop offset='1' style='stop-color:var(--g);stop-opacity:0'/></linearGradient></defs>"
           f"<line x1='0' y1='{Y(0):.1f}' x2='1000' y2='{Y(0):.1f}' class='eqz'/>"
           f"<path d='{area}' fill='url(#g)'/>"
           f"<path d='{path(series['BASE'])}' class='eqbase'/>"
           f"<path d='{path(series['B'])}' class='eqb'/>"
           f"<path d='{a}' class='eqa'/></svg>")
    last = series["A"][-1]
    tag = (f"<span class='eqtag' style='top:{Y(last) / 2:.1f}%;background:{col(last) if last else GREEN}'>{money(last)}</span>")
    ticks = [closed[0], closed[len(closed) // 2], closed[-1]] if n > 2 else closed
    axis = "".join(f"<span>{E(t_et(t['exit_utc']))}</span>" for t in ticks)
    return f"<div class='eq'>{svg}{tag}</div><div class='eqx mono'>{axis}</div>"


# ------------------------------------------------------------------ مدار الوكلاء
ICONS = {
    "radar": "<circle cx='12' cy='12' r='8'/><path d='M12 12 18 6'/><circle cx='12' cy='12' r='2'/>",
    "filter": "<path d='M4 5h16l-6 7v6l-4 2v-8z'/>",
    "candle": "<path d='M8 4v16M16 4v16'/><rect x='6' y='8' width='4' height='7'/><rect x='14' y='6' width='4' height='9'/>",
    "shield": "<path d='M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6z'/><path d='m9 12 2 2 4-4'/>",
    "news": "<rect x='4' y='4' width='16' height='16' rx='2'/><path d='M8 9h8M8 13h8M8 17h5'/>",
    "gate": "<path d='M5 21V4M5 4h11l-2 4 2 4H5'/>",
    "bell": "<path d='M6 16V11a6 6 0 0 1 12 0v5l2 2H4z'/><path d='M10 20a2 2 0 0 0 4 0'/>",
    "wallet": "<path d='M4 17 9 11l4 3 7-8'/><path d='M15 6h5v5'/>",
    "eye": "<path d='M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/>",
    "search": "<circle cx='11' cy='11' r='6'/><path d='m20 20-4.5-4.5'/>",
}


def icon(name):
    return (f"<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' "
            f"stroke-linejoin='round'>{ICONS[name]}</svg>")


def orbit_html(agents: list, core_big: str, core_sub: str, core_note: str) -> str:
    import math
    pills, lines = [], []
    n = len(agents)
    for i, (name, sub, metric, color, ic) in enumerate(agents):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        x, y = 50 + 39 * math.cos(ang), 50 + 41 * math.sin(ang)
        lines.append(f"<line x1='{x:.2f}' y1='{y:.2f}' x2='50' y2='50' data-i='{i}' style='--c:{color}'/>")
        pills.append(f"<div class='ag' data-i='{i}' style='left:{x:.2f}%;top:{y:.2f}%;--c:{color}'>"
                     f"<i>{icon(ic)}</i><div><b>{E(name)}</b><small>{E(sub)}</small></div><em class='mono'>{E(metric)}</em></div>")
    return (f"<div class='orbit'><svg class='ol' viewBox='0 0 100 100' preserveAspectRatio='none'>{''.join(lines)}</svg>"
            f"<div class='ring r1'></div><div class='ring r2'></div>"
            f"<div class='core'><div class='cl'>{icon('radar')}</div><div class='cb mono'>{E(core_big)}</div>"
            f"<div class='cs'>{E(core_sub)}</div></div>"
            f"<div class='cnote' id='cnote'>{core_note}</div>"
            f"{''.join(pills)}</div>")


# ------------------------------------------------------------------ بطاقة الصفقة المميزة
def focus_html(alerts: list, trades: list, cands: list) -> str:
    tA = [t for t in trades if t["kind"] == "A"]
    open_a = [t for t in tA if t["status"] in ("OPEN", "PENDING")]
    t = open_a[0] if open_a else (tA[0] if tA else None)
    al = next((a for a in alerts if t and a["id"] == t["alert_id"]), None) if t else (alerts[0] if alerts else None)
    d = json.loads(al["data"]) if al and al.get("data") else None
    if not t and not al:
        if not cands:
            return "<div class='empty'>لا صفقة ولا تنبيه بعد. تظهر هنا أحدث صفقة افتراضية مع مستوياتها.</div>"
        c = cands[0]
        d = json.loads(c["data"])
        sym, head_tag = c["symbol"], pill("أعلى مرشح", "t-grey")
        levels = [("إبطال", d.get("invalidation"), RED), ("السعر", d.get("price"), "var(--txt)"), ("الاختراق", d.get("trigger"), BLUE)]
        sub = f"الدرجة {c['score']} • لم يستوفِ كل القواعد بعد" if not c["qualifies"] else f"الدرجة {c['score']} • مؤهل"
    else:
        sym = (t or al)["symbol"]
        if t:
            st_txt = TSTAT.get(t["status"], t["status"])
            cls = "t-green" if t["status"] == "TARGET" or (t["pnl"] or 0) > 0 else "t-red" if t["status"] == "STOP" or (t["pnl"] or 0) < 0 else "t-amber"
            head_tag = pill(f"A • {st_txt}", cls)
            levels = [("الوقف", t["stop"], RED), ("الدخول", t["entry"], "var(--txt)"), ("الاختراق", t["trigger"], BLUE),
                      ("الهدف", t["target"], GREEN)]
            if t["exit"] is not None:
                levels.append(("الخروج", t["exit"], col(t["pnl"])))
            sub = (f"دخول {hm_et(t['entry_utc'])} الرياض • {t['shares']} سهم"
                   + (f" • <b style='color:{col(t['pnl'])}'>{money(t['pnl'])}</b>" if t["pnl"] is not None else ""))
        else:
            lab, c_ = STATE_AR.get(al["state"], (al["state"], GREY))
            head_tag = pill(lab, "t-green" if c_ == GREEN else "t-red" if c_ == RED else "t-amber")
            levels = [("إبطال", al["invalidation"], RED), ("التنبيه", al["price"], "var(--txt)"), ("الاختراق", al["trigger"], BLUE)]
            sub = f"تنبيه {hm_et(al['alert_utc'])} الرياض"
        if al:
            sub += f" • الدرجة {al['score']}"
    levels = [(n, v, c) for n, v, c in levels if v is not None]
    ladder = ""
    if levels:
        vs = [v for _, v, _ in levels]
        lo, hi = min(vs), max(vs)
        span = (hi - lo) or max(hi * 0.02, 0.01)
        lo, hi = lo - span * 0.08, hi + span * 0.08
        P = lambda v: 100 * (v - lo) / (hi - lo)
        stop = next((v for n, v, _ in levels if n in ("الوقف", "إبطال")), None)
        tgt = next((v for n, v, _ in levels if n == "الهدف"), None)
        zones = ""
        ent = next((v for n, v, _ in levels if n in ("الدخول", "التنبيه", "السعر")), None)
        if stop is not None and ent is not None:
            zones += f"<span class='zn' style='left:{P(stop):.1f}%;width:{P(ent) - P(stop):.1f}%;background:color-mix(in srgb,var(--r) 16%,transparent)'></span>"
        if tgt is not None and ent is not None:
            zones += f"<span class='zn' style='left:{P(ent):.1f}%;width:{P(tgt) - P(ent):.1f}%;background:color-mix(in srgb,var(--g) 16%,transparent)'></span>"
        merged = []  # دمج المستويات المتساوية (مثل الهدف = الخروج)
        for n, v, c in sorted(levels, key=lambda x: x[1]):
            if merged and abs(v - merged[-1][1]) < 1e-9:
                merged[-1] = (merged[-1][0] + " • " + n, v, c)
            else:
                merged.append((n, v, c))
        marks, last_row = [], [-99.0, -99.0]
        for n, v, c in merged:
            x = P(v)
            row = 0 if x - last_row[0] >= 14 else 1 if x - last_row[1] >= 14 else 0
            last_row[row] = x
            marks.append(f"<span class='mk r{row}' style='left:{x:.1f}%;--c:{c}'><b class='mono'>{E(I.fmt_price(v))}</b>"
                         f"<small>{E(n)}</small><i></i></span>")
        marks = "".join(marks)
        ladder = f"<div class='ladder'><div class='track'>{zones}</div>{marks}</div>"
    parts = ""
    if d and d.get("parts"):
        p = d["parts"]
        parts = "<div class='parts'>" + "".join(
            f"<div class='pt'><span>{E(k)}</span><div class='pb'><i style='width:{100 * max(0, p.get(k, 0)) / m:.0f}%'></i></div>"
            f"<b class='mono'>{p.get(k, 0)}/{m}</b></div>" for k, m in PARTS_MAX) + "</div>"
        if p.get("خصم"):
            parts += f"<div class='warn'>خصم مخاطر: {p['خصم']}</div>"
    why = ""
    if d:
        rs = [f"<li>{E(x)}</li>" for x in d.get("reasons", [])[:3]] + [f"<li class='rk'>{E(x)}</li>" for x in d.get("risks", [])[:2]]
        why = f"<ul class='why'>{''.join(rs)}</ul>" if rs else ""
    name = E((d or {}).get("name", "")[:34])
    return (f"<div class='fh'><div><span class='sym mono'>{E(sym)}</span> {head_tag}<div class='muted'>{name}</div></div>"
            f"<div class='muted fs'>{sub}</div></div>{ladder}{parts}{why}")


# ------------------------------------------------------------------ سجل الجلسة
def log_html(alerts, trades, last_cands, last_scan, missed) -> str:
    ev = []
    for a in alerts:
        ev.append((a["alert_utc"], pill("تنبيه", "t-blue"), a["symbol"],
                   f"درجة {a['score']} • دخول فوق {I.fmt_price(a['trigger'])} • إبطال {I.fmt_price(a['invalidation'])}",
                   "" if a["gain_from_open"] is None else f"<span class='muted'>{a['gain_from_open']:+.1f}%</span>"))
        if a["activated_utc"]:
            lead = "" if a["lead_min"] is None else f" — سبق {a['lead_min']:.0f} د"
            ev.append((a["activated_utc"], pill("تفعّل", "t-green"), a["symbol"], f"اخترق {I.fmt_price(a['trigger'])}{lead}", ""))
        if a["invalidated_utc"]:
            ev.append((a["invalidated_utc"], pill("أُلغي", "t-red"), a["symbol"], f"كسر الإبطال {I.fmt_price(a['invalidation'])}", ""))
    for t in trades:
        k = {"A": "A", "B": "B", "BASE": "مقارنة"}.get(t["kind"], t["kind"])
        if t["entry_utc"] and t["status"] not in ("CANCELLED", "EXPIRED"):
            ev.append((t["entry_utc"], pill("شراء", "t-dark"), t["symbol"],
                       f"{k} • {t['shares']} سهم @ {I.fmt_price(t['entry'])} • وقف {I.fmt_price(t['stop'])}", ""))
        if t["exit_utc"] and t["pnl"] is not None:
            cls = {"TARGET": "t-green", "STOP": "t-red"}.get(t["status"], "t-grey")
            ev.append((t["exit_utc"], pill(TSTAT.get(t["status"], t["status"]), cls), t["symbol"],
                       f"{k} • خروج @ {I.fmt_price(t['exit'])}" + ("" if t["r_mult"] is None else " • " + LTR(f"{t['r_mult']:+.2f}R")),
                       f"<b class='mono' style='color:{col(t['pnl'])}'>{money(t['pnl'])}</b>"))
        elif t["status"] in ("CANCELLED", "EXPIRED"):
            ev.append((t["exit_utc"] or t["created_utc"], pill("لم تُنفّذ", "t-grey"), t["symbol"], f"{k} • {t['note'] or ''}", ""))
    if last_scan:
        for c in last_cands:
            d = json.loads(c["data"])
            if d.get("excluded"):
                ev.append((last_scan["started_utc"], pill("حظر", "t-amber"), c["symbol"], d["excluded"][0], "<span class='muted'>لم يُتداول</span>"))
    for m in missed:
        close = datetime.fromisoformat(m["day"]).replace(hour=16, tzinfo=I.ET).astimezone(I.UTC).isoformat()
        ev.append((close, pill("فات", "t-grey"), m["symbol"], LTR(f"+{m['max_gain_pct']:.0f}%") + f" بلا تنبيه — {(m['reason'] or '')[:90]}", ""))
    ev = [e for e in ev if e[0]]
    ev.sort(key=lambda e: e[0], reverse=True)
    if not ev:
        return "<div class='empty'>السجل فارغ — يمتلئ أثناء الجلسة (تنبيهات، تفعيل، صفقات، حظر، وما فات).</div>"
    rows = []
    last_day = None
    for at, tag, sym, why, val in ev[:60]:
        day = datetime.fromisoformat(at).astimezone(I.ET).strftime("%Y-%m-%d")
        if day != last_day:
            rows.append(f"<div class='lday mono'>{day}</div>")
            last_day = day
        rows.append(f"<div class='lr'><span class='lt mono'>{hm_et(at)}</span>{tag}<div class='lb'><b class='mono'>{E(sym)}</b>"
                    f"<small>{E(why)}</small></div><span class='lv'>{val}</span></div>")
    return "<div class='log'>" + "".join(rows) + "</div>"


# ------------------------------------------------------------------ البناء
def PX(cfg: dict) -> str:
    """حد السعر كما في config.json (live.max_price)، مثل «7$»."""
    return f"{cfg.get('max_price', 5):g}$"


def halts_html(store, cfg: dict, alerts: List[dict], trades: List[dict]) -> str:
    """توقفات اليوم الرسمية كما جاءت من Nasdaq (بلا تعديل)، مع تمييز الأسهم التي نتابعها."""
    try:
        last = store.one("SELECT MAX(day) d FROM halts")
    except Exception:
        return "<div class='empty'>لا بيانات توقفات بعد.</div>"
    day = last and last["d"]
    if not day:
        return "<div class='empty'>لا بيانات توقفات بعد — تظهر من أول فحص بعد هذا التحديث.</div>"
    rows = store.q("SELECT * FROM halts WHERE day=? ORDER BY halt_time DESC", (day,))
    mine = {a["symbol"] for a in alerts if a["day"] == day} | {t["symbol"] for t in trades if t["day"] == day and t["kind"] != "BASE"}
    cap = cfg.get("max_price", 5) * 1.5
    now = datetime.now(HL.NY)
    body, hidden = [], 0
    for r in rows:
        h = HL.Halt(r["symbol"], r["name"] or "", r["market"] or "", r["reason"] or "", r["halt_date"], r["halt_time"] or None,
                    r["pause_price"], r["resume_date"], r["resume_quote"], r["resume_trade"])
        if h.pause_price and h.pause_price > cap and h.symbol not in mine:
            hidden += 1
            continue
        ha, ra = h.halted_at(), h.resumes_at()
        act = h.active(now)
        star = "★ " if h.symbol in mine else ""
        body.append([
            f"<span class='mono'>{(TF.h12(ha.astimezone(I.RIYADH), True) if h.halt_date == day else TF.dh12(ha.astimezone(I.RIYADH), year=True)) if ha else '—'}</span>"
            + ("" if h.halt_date == day else "<div class='muted' style='font-size:12px'>توقف مستمر منذ أيام</div>"),
            f"<b class='mono'>{star}{E(h.symbol)}</b><div class='muted' style='font-size:12px'>{E(h.name[:40])}</div>",
            f"{E(h.reason_ar)} <span class='muted mono'>({E(h.reason)})</span>",
            "—" if not h.pause_price else f"<span class='mono'>{I.fmt_price(h.pause_price)}</span>",
            "—" if not ra else f"<span class='mono'>{TF.h12(ra.astimezone(I.RIYADH), True)}</span>",
            f"<b style='color:{RED if act else GREEN}'>{'موقوف الآن' if act else 'استؤنف'}</b>",
        ])
    note = (f"<div class='muted'>يوم {E(day)} • {len(body)} توقفاً" + (f" (أُخفي {hidden} لأسهم سعر إيقافها فوق {cap:g}$)" if hidden else "")
            + " • ★ = سهم عليه تنبيه أو صفقة اليوم • الأوقات بتوقيت الرياض • المصدر: "
              "<a href='https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts' target='_blank' rel='noopener'>Nasdaq Trader</a> "
              "(كما نُشر بلا تعديل)</div>")
    return note + table(["وقت التوقف", "السهم", "السبب", "سعر الإيقاف", "الاستئناف المعلن", "الحالة"], body)


def build(store, out_path: str, cfg: dict) -> None:
    runs = store.q("SELECT * FROM runs ORDER BY id DESC LIMIT 60")
    last_scan = store.one("SELECT * FROM runs WHERE id IN (SELECT DISTINCT run_id FROM candidates) ORDER BY id DESC LIMIT 1")
    last_scan_any = store.one("SELECT * FROM runs WHERE kind='scan' ORDER BY id DESC LIMIT 1")
    cands = store.q("SELECT * FROM candidates WHERE run_id=? ORDER BY qualifies DESC, score DESC", (last_scan["id"],)) if last_scan else []
    alerts = store.q("SELECT * FROM alerts ORDER BY id DESC LIMIT 300")
    trades = store.q("SELECT * FROM trades ORDER BY id DESC")
    missed = store.q("SELECT * FROM missed ORDER BY day DESC, max_gain_pct DESC LIMIT 200")
    day = store.one("SELECT MAX(day) d FROM (SELECT day FROM candidates UNION SELECT day FROM alerts UNION SELECT day FROM trades)")
    day = day["d"] if day else None

    # ---- الأرقام الرئيسية
    sA, sB, sBase = (TR.summary([t for t in trades if t["kind"] == k]) for k in ("A", "B", "BASE"))
    totA = sA.get("total_pnl", 0.0) if sA.get("trades") else 0.0
    closedA = [t for t in trades if t["kind"] == "A" and t["pnl"] is not None]
    winsA = sum(1 for t in closedA if t["pnl"] > 0)
    lead = [a["lead_min"] for a in alerts if a["lead_min"] is not None]
    best = max([t for t in trades if t["r_mult"] is not None and t["kind"] in ("A", "B")], key=lambda t: t["r_mult"], default=None)
    last_closed = max([t for t in trades if t["pnl"] is not None and t["exit_utc"]], key=lambda t: t["exit_utc"], default=None)
    alerts_today = sum(1 for a in alerts if a["day"] == day)

    # ---- الوكلاء: مراحل الفحص بأرقام آخر تشغيل
    ls = last_scan or last_scan_any or {}
    cd_ = [json.loads(c["data"]) for c in cands]
    n_excl = sum(1 for d in cd_ if d.get("excluded"))
    n_news = sum(1 for d in cd_ if d.get("news_headline"))
    n_broken = sum(1 for c, d in zip(cands, cd_) if c["stage"] == "BROKEN" or d.get("stage") == "BROKEN")
    n_open = sum(1 for t in trades if t["status"] in ("OPEN", "PENDING"))
    act = sum(1 for a in alerts if a["day"] == day and a["state"] == "ACTIVATED")
    inv = sum(1 for a in alerts if a["day"] == day and a["state"] == "INVALIDATED")
    n_missed = sum(1 for m in missed if m["day"] == day)
    agents = [
        ("ماسح السوق", "كل الأسهم المدرجة", f"{ls.get('swept') or 0:,}", "#2563eb", "radar"),
        ("الفلتر", f"تحت {PX(cfg)} وحجم كافٍ", f"{ls.get('shortlist') or 0}", "#0891b2", "filter"),
        ("محلل الشموع", "RVOL • تماسك • VWAP", f"{len(cands)}", "#7c3aed", "candle"),
        ("حارس SEC", "طرح • شطب • تجميع", f"{n_excl} محظور", "#dc2626", "shield"),
        ("قارئ الأخبار", "جوهري / ترويجي", f"{n_news}", "#db2777", "news"),
        ("بوابة التبكير", "قبل الاختراق فقط", f"{n_broken} فات", "#d97706", "gate"),
        ("المنبّه", "تيليجرام", f"{ls.get('alerts') or 0}", "#ea580c", "bell"),
        ("الصفقات", "A • B • مقارنة", f"{n_open} مفتوحة", "#16a34a", "wallet"),
        ("المتابع", "تفعّل / أُلغي", f"{act} / {inv}", "#059669", "eye"),
        ("مدقق «فاتت»", "30%+ بلا تنبيه", f"{n_missed}", "#475569", "search"),
    ]
    la = alerts[0] if alerts else None
    core_note = ("آخر إشارة: <b class='mono'>" + E(la["symbol"]) + "</b> " + STATE_AR.get(la["state"], (la["state"], ""))[0]
                 if la else "لا إشارات بعد")

    last_phase = cands[0].get("phase") if cands else None
    early_alerts = [a for a in alerts if (a.get("kind") or "EARLY") == "EARLY"]
    ext_alerts = [a for a in alerts if a.get("kind") in ("PRE", "POST")]
    tr_by_alert = {t["alert_id"]: t for t in trades if t["kind"] in ("PRE", "POST")}
    xrow = []
    for a in ext_alerts:
        lab, c_ = STATE_AR.get(a["state"], (a["state"], GREY))
        t = tr_by_alert.get(a["id"]) or {}
        xrow.append([E(a["day"]), pill("قبل الافتتاح" if a["kind"] == "PRE" else "بعد الإغلاق", "t-blue"), t_et(a["alert_utc"]),
                     f"<b class='mono'>{E(a['symbol'])}</b>", f"{a['gain_from_open']:+.1f}%" if a["gain_from_open"] is not None else "—",
                     str(a["score"]), I.fmt_price(a["price"]), I.fmt_price(a["trigger"]), I.fmt_price(a["invalidation"]),
                     f"<b style='color:{c_}'>{lab}</b>", E(TSTAT.get(t.get("status"), "—")),
                     f"<b class='mono' style='color:{col(t.get('pnl'))}'>{money(t.get('pnl'))}</b>"])
    ext_html = table(["الجلسة", "الفترة", "وقت التنبيه (الرياض)", "السهم", "التحرك", "الدرجة", "السعر", "التفعيل (القمة)",
                      "الإبطال", "الحالة في الجلسة", "الصفقة", "النتيجة"], xrow)

    # ---- الجداول
    crow = []
    for c in cands[:60]:
        d = json.loads(c["data"])
        ok = c["qualifies"]
        why = "<br>".join(E(x) for x in d.get("reasons", [])[:5])
        bad = "<br>".join(E(x) for x in (d.get("excluded", []) + d.get("gates_failed", []))[:4])
        risks = "<br>".join(E(x) for x in d.get("risks", [])[:3])
        stage = (pill("مبكرة", "t-amber") if d.get("stage") == "EARLY" else
                 pill("قبل الافتتاح", "t-blue") if d.get("phase") == "PRE" else
                 pill("بعد الإغلاق", "t-blue") if d.get("phase") == "POST" else pill("فاتت/اخترق", "t-grey"))
        crow.append([f"<b class='mono'>{E(c['symbol'])}</b><div class='muted'>{E((d.get('name') or '')[:28])}</div>",
                     f"<b class='mono' style='color:{GREEN if ok else GREY}'>{c['score']}</b>" + (" ✔" if ok else ""),
                     stage, f"<span class='mono'>{I.fmt_price(d.get('price'))}</span>",
                     (f"{d['rvol']:.1f}×" if d.get("rvol") else
                      f"{d['change_pct']:+.0f}%" if d.get("change_pct") is not None else "—"),
                     f"<span class='mono'>{I.fmt_price(d.get('trigger'))} / {I.fmt_price(d.get('invalidation'))}</span>",
                     why or "—", (f"<span class='bad'>{bad}</span>" if bad else "") + (f"<div class='warn'>{risks}</div>" if risks else ""),
                     f"{RL.quality_label(d.get('data_quality', 0))} ({d.get('data_quality', 0)})"])
    cand_html = table(["السهم", "الدرجة", "المرحلة", "السعر", "RVOL / التحرك", "الدخول / الإبطال", "سبب الإشارة", "ما يمنع / المخاطر", "جودة البيانات"], crow)

    arow = []
    for a in early_alerts:
        lab, c_ = STATE_AR.get(a["state"], (a["state"], GREY))
        arow.append([E(a["day"]), t_et(a["alert_utc"]), f"<b class='mono'>{E(a['symbol'])}</b>", str(a["score"]), I.fmt_price(a["price"]),
                     I.fmt_price(a["trigger"]), I.fmt_price(a["invalidation"]), f"<b style='color:{c_}'>{lab}</b>",
                     "—" if a["lead_min"] is None else f"{a['lead_min']:.0f} د",
                     "—" if a["gain_from_open"] is None else f"{a['gain_from_open']:+.1f}%",
                     f"{a['data_quality']}", "✓" if a["sent"] else "✗"])
    alert_html = table(["اليوم", "الوقت (الرياض)", "السهم", "الدرجة", "السعر", "شرط الدخول", "الإبطال", "الحالة",
                        "السبق قبل الاختراق", "التغير عند التنبيه", "جودة البيانات", "أُرسل"], arow)

    summ = h5_html(trades) + ("<div class='grid3'>" + sum_card("A: الدخول عند التنبيه", sA, GREEN) + sum_card("B: الدخول عند الاختراق", sB, BLUE)
            + sum_card("المقارنة: أي سهم قفز حجمه", sBase, GREY) + "</div>")
    trow = []
    for t in trades[:400]:
        trow.append([E(t["day"]), E(KIND.get(t["kind"], t["kind"])), f"<b class='mono'>{E(t['symbol'])}</b>", t_sec(t["entry_utc"] or t["created_utc"], t.get("fill_src") == "tick"),
                     I.fmt_price(t["entry"]), I.fmt_price(t["stop"]), I.fmt_price(t["target"]), str(t["shares"]),
                     E(TSTAT.get(t["status"], t["status"])), I.fmt_price(t["exit"]), t_sec(t["exit_utc"], t.get("fill_src") == "tick"),
                     f"<b class='mono' style='color:{col(t['pnl'])}'>{money(t['pnl'])}</b>", "—" if t["r_mult"] is None else f"{t['r_mult']:+.2f}R",
                     fill_tag(t), f"<span class='muted'>{E(t['note'])}</span>"])
    trade_html = summ + ("<p class='muted'>التنفيذ «بالثانية»: الشراء بسعر العرض بعد التنبيه بثانية، والوقف يتفعّل بأول صفقة "
                         "عند الوقف أو تحته ويُباع بسعر الطلب الفعلي بعدها، والهدف يُحسب فقط إذا تجاوزه السعر. "
                         "الربح صافٍ بعد رسوم البيع النظامية.</p>") + table(
        ["اليوم", "النوع", "السهم", "الدخول (الرياض)", "سعر الدخول", "الوقف", "الهدف", "الكمية", "الحالة",
         "الخروج", "وقت الخروج", "الربح", "R", "الدقة", "ملاحظة"], trow)

    mrow = [[E(m["day"]), f"<b class='mono'>{E(m['symbol'])}</b>", I.fmt_price(m["prev_close"]), f"+{m['max_gain_pct']:.0f}%",
             f"{m['close_gain_pct']:+.0f}%", "نعم" if m["was_candidate"] else "لا", "—" if m["best_score"] is None else str(m["best_score"]),
             f"<span class='muted'>{E(m['reason'])}</span>"] for m in missed]
    missed_html = table(["اليوم", "السهم", "إغلاق أمس", "أعلى ارتفاع", "عند آخر فحص", "كان مرشحاً؟", "أعلى درجة", "لماذا لم يُنبَّه"], mrow)

    rrow = [[E(r["id"]), E(r["kind"]), t_et(r["started_utc"]), "—" if r["delay_min"] is None else f"{r['delay_min']:.0f} د",
             str(r["swept"] or 0), str(r["shortlist"] or 0), str(r["qualified"] or 0), str(r["alerts"] or 0),
             str(r["api_calls"] or 0), "—" if r["duration_sec"] is None else f"{r['duration_sec']:.0f} ث",
             f"<span class='bad'>{E((r['errors'] or '')[:200])}</span>"] for r in runs]
    runs_html = table(["#", "النوع", "البدء (الرياض)", "تأخر عن الموعد", "أسهم ممسوحة", "قائمة قصيرة", "مؤهلة", "تنبيهات",
                       "طلبات Alpaca", "المدة", "أخطاء"], rrow)

    upd = runs[0]["started_utc"] if runs else None
    upd_txt = "—" if not upd else (TF.dh12(datetime.fromisoformat(upd).astimezone(I.RIYADH), year=True) + " بتوقيت الرياض")

    def tile(label, value, sub="", color="var(--txt)"):
        return f"<div class='tile'><span>{label}</span><b class='mono' style='color:{color}'>{value}</b><small>{sub}</small></div>"

    tiles = ("<div class='tiles'>"
             + tile("تنبيهات اليوم", str(alerts_today), f"الإجمالي {len(alerts)}")
             + tile("متوسط السبق قبل الاختراق", "—" if not lead else f"{sum(lead) / len(lead):.0f} د", f"من {len(lead)} تنبيه تفعّل")
             + tile("نسبة الربح (A)", "—" if not closedA else f"{winsA / len(closedA):.0%}", f"{winsA} ربح / {len(closedA) - winsA} خسارة")
             + tile("أفضل صفقة", "—" if not best else f"{best['r_mult']:+.2f}R", "" if not best else f"{E(best['symbol'])} • {best['kind']}",
                    GREEN if best and best["r_mult"] > 0 else "var(--txt)")
             + tile("آخر صفقة مغلقة", "—" if not last_closed else money(last_closed["pnl"]),
                    "" if not last_closed else f"{E(last_closed['symbol'])} • {E(TSTAT.get(last_closed['status'], ''))}",
                    col(last_closed["pnl"]) if last_closed else "var(--txt)")
             + "</div>")

    head_sub = (f"A: {len(closedA)} صفقة • {winsA} ربح / {len(closedA) - winsA} خسارة"
                f" • B {money(sB.get('total_pnl')) if sB.get('trades') else '—'} • المقارنة {money(sBase.get('total_pnl')) if sBase.get('trades') else '—'}")

    page = (HEAD.replace("__MAXPX__", PX(cfg))
            + f"""<header class="top">
<div class="brand"><div class="logo">{icon('bell')}</div><div>
<h1>التنبيه <span>المبكر</span></h1>
<div class="st"><span class="dot" id="dot"></span><span id="live">—</span> • فحص كل 5 دقائق في الجلسة • قبلها وبعدها كل 15 دقيقة • 10 وكلاء • أسهم أمريكية تحت {PX(cfg)} • Alpaca • SEC EDGAR</div>
<div class="thm" id="thm" role="group" aria-label="المظهر"><button data-t="light"><i style="background:#f5f6f8"></i>فاتح</button><button data-t="dark"><i style="background:#171a21"></i>داكن</button><button data-t="night"><i style="background:#0a1410"></i>ليلي</button><button data-t="blue"><i style="background:#101d40"></i>أزرق</button></div>
</div></div>
<div class="bal"><div class="bv mono" style="color:{col(totA) if totA else 'var(--txt)'}">{E(money(totA) if closedA else '0.00$')}</div>
<div class="muted mono-ish">{E(head_sub)}</div></div>
</header>
<div class="wrap">
<div class="bar" data-updated="{E(upd or '')}">
  <button id="refresh" type="button">تحديث</button>
  <div class="tm"><span class="lbl" id="nlbl">الفحص التالي بعد</span><b id="cd" class="mono">—</b><span class="muted" id="nat"></span></div>
  <div class="muted" id="age"></div>
  <div class="muted">آخر تحديث: {E(upd_txt)}</div>
  <div class="new" id="newv" hidden>وصل تحديث جديد — <a href="#" id="reload2">اعرضه</a></div>
</div>
<div id="tabs-top"></div><nav id="tabs" role="tablist"><a href="#overview">نظرة عامة</a><a href="#cands">المرشحون الآن</a><a href="#alerts">سجل التنبيهات</a><a href="#ext">قبل الافتتاح وبعد الإغلاق</a><a href="#trades">الصفقات الافتراضية</a><a href="#charts">الرسوم البيانية</a><a href="#cal">تقويم الربح والخسارة</a><a href="#halts">التوقفات</a><a href="#missed">فاتت</a><a href="#runs">التشغيلات</a></nav>
<section id="overview">
<div class="card"><div class="ch"><span class="lab">الربح الافتراضي — {cfg['shares_per_trade']} سهم لكل صفقة</span>
<span class="lg"><i class="la"></i>A عند التنبيه <i class="lb2"></i>B عند الاختراق <i class="lc"></i>المقارنة</span></div>
{EQ.section(trades)}</div>
{tiles}
<div class="card"><div class="ch"><span class="lab">مدار الوكلاء — كلهم متصلون بالمحرك</span>
<span class="muted">أرقام آخر فحص {E(t_et(ls.get('started_utc')) if ls else '—')} الرياض</span></div>
{orbit_html(agents, str(ls.get('qualified') or 0), f"مؤهل • حد {cfg['min_score']}", core_note)}</div>
<div class="two">
<div class="card"><div class="ch"><span class="lab">الصفقة المميزة</span><span class="muted">مستويات حقيقية من قاعدة البيانات</span></div>
{focus_html(alerts, trades, cands)}</div>
<div class="card"><div class="ch"><span class="lab">سجل الجلسة — مباشر</span><span class="muted">بتوقيت الرياض</span></div>
{log_html(alerts, trades, cands, last_scan, missed)}</div>
</div>
<div class="note">البيانات من Alpaca المجانية: السوق الكامل متأخر 15 دقيقة، والسعر اللحظي من IEX فقط (جزء صغير من التداول).
Float غير متوفر مجاناً. التنبيهات ليست توصية شراء، والصفقات افتراضية فقط. النظام لا يكتشف كل انفجار — راجع قسم «فاتت».</div>
<div class="card" id="study"><div class="ch"><span class="lab">ما تقوله الدراسة التاريخية (2016 → 2026، 3,730 سهماً تحت 5$)</span>
<a class="muted" href="https://github.com/sultanalialdosari-sys/Trading-heads-up/blob/main/research/REPORT_v3.md" target="_blank" rel="noopener">تقرير v3</a></div>
<ul class="study">
<li><b>المؤشرات تتنبأ بحجم الحركة لا باتجاهها:</b> التذبذب العالي وRVOL ≥ 5× يرفعان احتمال قفزة 20% غداً من 3% إلى 29%، لكن متوسط عائد الغد قريب من الصفر — تتحرك بعنف في الاتجاهين.</li>
<li><b>التنبيه المبكر الحالي لم يتفوق بشكل موثوق</b> على الدخول العشوائي تاريخياً (−0.22R مقابل −0.33R في سنوات الاختبار). الوقف الضيق تحت التماسك يُضرب كثيراً.</li>
<li><b>الدراسة v3 (نوفمبر 2026، فرضيات مسجلة مسبقاً، أيام مختارة بلا نظر للنتيجة، تكاليف وتنفيذ واقعي):</b> الزخم داخل اليوم (ارتفاع 5–25% بحجم 3× وفوق VWAP) على 13,318 إشارة منذ 2016 — <b>مرفوض في الفرضيات الثلاث</b>: قبل التكاليف حول الصفر (−0.02 إلى −0.09R)، وبعد تكلفة نحو 1.6% للصفقة −0.29R بوقف واسع و−0.62R بوقف ضيق في سنوات الاختبار، وكل سنة سالبة. الدخول العشوائي في نفس الأسهم والأيام لم يكن أسوأ.</li>
<li>لذلك: التنبيهات هنا <b>للمراقبة لا للتنبؤ</b>، والحكم الحقيقي من النتائج الحية في الأسفل.</li>
</ul></div>
</section>
<section id="cands"><h2>المرشحون في آخر فحص — {E(PHASE_LABEL.get(last_phase, ''))} ({E(t_et(last_scan['started_utc']) if last_scan else '—')} الرياض)</h2>
<div class="muted">مرتبون حسب الدرجة. ✔ = استوفى كل القواعد (حد التنبيه {cfg['min_score']}).</div>{cand_html}</section>
<section id="alerts"><h2>سجل التنبيهات المبكرة (داخل الجلسة)</h2>{alert_html}</section>
<section id="ext"><h2>قوائم ما قبل الافتتاح وبعد الإغلاق</h2><div class="muted">أسهم تحت {PX(cfg)} تحركت 8%+ خارج الجلسة بحجم حقيقي. التفعيل = اختراق قمة تلك الفترة خلال الجلسة التالية، والنتيجة تُقاس من الجلسة فقط.</div>{ext_html}</section>
<section id="trades"><h2>الصفقات الافتراضية ({cfg['shares_per_trade']} سهم لكل صفقة)</h2>{trade_html}</section>
""" + CH.section_html(CH.build_items(store, alerts, trades)) + f"""
<section id="cal"><h2>تقويم الربح والخسارة اليومي</h2><div class="muted">لكل يوم: مجموع ربح الصفقات الرابحة (وعددها)، ومجموع خسارة الخاسرة (وعددها)، والصافي — من كل الصفقات الافتراضية المغلقة بكل أنواعها. مرّر المؤشر على اليوم لترى التوزيع حسب النوع. افتراضي فقط.</div>
<div class="card">{calendar_html(trades)}</div></section>
<section id="halts"><h2>توقفات التداول اليوم</h2><div class="muted">إيقافات LULD وبانتظار الخبر وغيرها. أثناء التوقف لا يمكن البيع ولا الشراء، والنظام لا يرسل تنبيهاً لسهم موقوف.</div>{halts_html(store, cfg, alerts, trades)}</section>
<section id="missed"><h2>فاتت: ارتفعت 30%+ بلا تنبيه</h2><div class="muted">لا نخفي ما فات: كل سهم تحت {PX(cfg)} ارتفع 30%+ اليوم بلا تنبيه، ولماذا.</div>{missed_html}</section>
<section id="runs"><h2>التشغيلات</h2><div class="muted">التأخر = الفرق بين موعد الجدولة ووقت البدء الفعلي على GitHub.</div>{runs_html}</section>
<footer class="muted">صفقات افتراضية فقط — لا يُرسل أي أمر تداول.</footer>
</div>"""
            + SCRIPT + CH.SCRIPT + EQ.SCRIPT + TABS + "</body></html>")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    # ملف صغير تقرؤه الصفحة كل دقيقة لتعرف هل نُشر تحديث جديد
    with open(os.path.join(os.path.dirname(out_path) or ".", "status.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": upd, "run": runs[0]["id"] if runs else None}, f)


TABS = '''<script>
(function(){
 var nav=document.getElementById('tabs'); if(!nav) return;
 var links=[].slice.call(nav.querySelectorAll('a')), ids=links.map(function(a){return a.getAttribute('href').slice(1)});
 document.body.classList.add('tabs');
 function show(){
  var h=(location.hash||'').slice(1), id=ids.indexOf(h)>=0?h:'overview';
  ids.forEach(function(x){var el=document.getElementById(x); if(el) el.classList.toggle('on',x===id)});
  links.forEach(function(a){var on=a.getAttribute('href')==='#'+id; a.classList.toggle('on',on); a.setAttribute('aria-selected',on)});
  try{sessionStorage.setItem('hu_tab',id)}catch(e){}
  if(id==='charts'&&window.huRedrawChart) window.huRedrawChart();
  if(id==='overview'&&window.huRedrawEquity) window.huRedrawEquity();
  var act=nav.querySelector('a.on'); if(act&&act.scrollIntoView) act.scrollIntoView({block:'nearest',inline:'center'});
 }
 links.forEach(function(a){a.addEventListener('click',function(e){e.preventDefault();
  history.replaceState(null,'',a.getAttribute('href')); show();
  var top=document.getElementById('tabs-top'); if(top&&window.scrollY>top.offsetTop) window.scrollTo({top:top.offsetTop})})});
 window.addEventListener('hashchange',show);
 if(!location.hash){try{var t=sessionStorage.getItem('hu_tab'); if(t&&ids.indexOf(t)>=0) history.replaceState(null,'','#'+t)}catch(e){}}
 show();
})();
</script>'''

HEAD = """<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<link rel="icon" href="favicon.svg" type="image/svg+xml"><link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="icon" href="favicon.ico" sizes="any"><link rel="apple-touch-icon" href="apple-touch-icon.png">
<link rel="manifest" href="site.webmanifest"><meta name="theme-color" content="#0f1419">
<meta name="apple-mobile-web-app-title" content="التنبيه المبكر">
<title>التنبيه المبكر — أسهم تحت __MAXPX__</title>
<script>try{var t=localStorage.getItem("hu_theme");if(t)document.documentElement.setAttribute("data-theme",t)}catch(e){}</script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Arabic:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root,[data-theme=light]{--bg:#f5f6f8;--card:#fff;--line:#e7e9ee;--l2:#f1f3f6;--soft:#fafbfc;--txt:#0f1419;--mut:#6b7380;
 --ink:#0f1419;--inkfg:#fff;--glow:#e7f6ec;--g:#16a34a;--r:#dc2626;--b:#2563eb;--a:#d97706;--nbg:#fffbeb;--nln:#fde68a;--ntx:#92400e;--sh:rgba(16,24,40,.05)}
[data-theme=dark]{--bg:#0f1115;--card:#171a21;--line:#262b35;--l2:#1f232b;--soft:#1b1f27;--txt:#e6e8ec;--mut:#8b93a1;
 --ink:#e6e8ec;--inkfg:#0f1115;--glow:#1c2a22;--g:#22c55e;--r:#f87171;--b:#60a5fa;--a:#fbbf24;--nbg:#221c0c;--nln:#4a3a10;--ntx:#fcd34d;--sh:rgba(0,0,0,.3)}
[data-theme=night]{--bg:#040907;--card:#0a1410;--line:#163024;--l2:#10221a;--soft:#0d1a14;--txt:#d4f5e2;--mut:#6f9581;
 --ink:#22c55e;--inkfg:#03100a;--glow:#0f2e1c;--g:#4ade80;--r:#fb7185;--b:#38bdf8;--a:#facc15;--nbg:#141a07;--nln:#3a4210;--ntx:#d9f99d;--sh:rgba(34,197,94,.06)}
[data-theme=blue]{--bg:#0a1430;--card:#101d40;--line:#21325f;--l2:#18284f;--soft:#14244a;--txt:#e7eeff;--mut:#91a3cc;
 --ink:#60a5fa;--inkfg:#08122b;--glow:#15306a;--g:#34d399;--r:#fb7185;--b:#93c5fd;--a:#fbbf24;--nbg:#13214a;--nln:#2c4385;--ntx:#bfdbfe;--sh:rgba(0,0,0,.3)}
body,.card,.tile,.bar,.ag,.tw,nav a,nav{transition:background-color .25s,border-color .25s,color .25s}
.thm{display:flex;gap:2px;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:3px;margin-top:8px;width:max-content}
.thm button{border:0;background:none;color:var(--mut);font:600 12px "IBM Plex Sans Arabic",sans-serif;padding:4px 11px;border-radius:999px;cursor:pointer;display:flex;align-items:center;gap:5px}
.thm button i{width:9px;height:9px;border-radius:50%;border:1px solid var(--line)}
.thm button.on{background:var(--ink);color:var(--inkfg)}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 "IBM Plex Sans Arabic","Segoe UI",Tahoma,sans-serif}
.mono{font-family:"JetBrains Mono",ui-monospace,Consolas,monospace;direction:ltr;unicode-bidi:isolate}
.muted{color:var(--mut);font-size:13px}.bad{color:var(--r);font-size:13px}.warn{color:var(--a);font-size:13px}
.wrap{max-width:1280px;margin:auto;padding:0 18px 40px}
.top{max-width:1280px;margin:auto;padding:22px 18px 14px;display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}
.brand{display:flex;gap:12px;align-items:center}
.logo{width:46px;height:46px;border-radius:50%;background:var(--ink);color:var(--inkfg);display:grid;place-items:center;box-shadow:0 0 0 4px var(--glow)}
.logo svg{width:24px;height:24px}
h1{margin:0;font-size:24px;font-weight:700;letter-spacing:-.2px}h1 span{color:var(--g)}
.st{font-size:12.5px;color:var(--mut)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#9ca3af;margin-inline-end:6px;vertical-align:middle}
.dot.on{background:var(--g);box-shadow:0 0 0 0 rgba(22,163,74,.6);animation:pulse 1.8s infinite}
@keyframes pulse{70%{box-shadow:0 0 0 8px rgba(22,163,74,0)}100%{box-shadow:0 0 0 0 rgba(22,163,74,0)}}
.bal{text-align:left}.bv{font-size:38px;font-weight:700;line-height:1.1}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;margin-top:14px;box-shadow:0 1px 2px var(--sh)}
.ch{display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.lab{font:700 12px/1 "JetBrains Mono","IBM Plex Sans Arabic",monospace;letter-spacing:.4px;color:var(--txt);opacity:.8}
.lg{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:6px}
.lg i{display:inline-block;width:18px;height:3px;border-radius:2px;margin-inline-start:8px}
.la{background:var(--g)}.lb2{background:repeating-linear-gradient(90deg,var(--b) 0 5px,transparent 5px 8px)}.lc{background:repeating-linear-gradient(90deg,#9ca3af 0 2px,transparent 2px 5px)}
.bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 18px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 14px}
.bar button{background:var(--ink);color:var(--inkfg);border:0;border-radius:10px;padding:8px 18px;font:inherit;font-weight:600;cursor:pointer}
.bar button:hover{opacity:.85}.bar button:disabled{opacity:.6;cursor:wait}
.tm{display:flex;align-items:baseline;gap:8px}.tm b{font-size:22px;color:var(--g)}
.new{color:var(--g);font-weight:600}.new a{color:var(--g)}
.eq{position:relative;height:190px}.eq svg{width:100%;height:100%;display:block;overflow:visible}
.eqz{stroke:var(--line);stroke-dasharray:4 4;vector-effect:non-scaling-stroke}
.eqa{fill:none;stroke:var(--g);stroke-width:2.5;vector-effect:non-scaling-stroke;stroke-linejoin:round}
.eqb{fill:none;stroke:var(--b);stroke-width:1.6;stroke-dasharray:6 4;vector-effect:non-scaling-stroke}
.eqbase{fill:none;stroke:#9ca3af;stroke-width:1.4;stroke-dasharray:2 4;vector-effect:non-scaling-stroke}
.eqtag{position:absolute;right:-6px;transform:translate(0,-50%);color:#fff;font:700 12px "JetBrains Mono",monospace;padding:2px 8px;border-radius:6px;direction:ltr}
.eqempty{position:absolute;inset:0;display:grid;place-items:center;color:var(--mut);font-size:14px}
.eqx{display:flex;justify-content:space-between;font-size:11px;color:#9ca3af;margin-top:6px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:14px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 14px;display:flex;flex-direction:column}
.tile span{font-size:12px;color:var(--mut)}.tile b{font-size:24px;line-height:1.3;text-align:right}.tile small{font-size:12px;color:var(--mut)}
/* مدار الوكلاء */
.orbit{position:relative;height:470px}
.ol{position:absolute;inset:0;width:100%;height:100%}
.ol line{stroke:var(--line);stroke-width:1;stroke-dasharray:2 5;vector-effect:non-scaling-stroke;transition:stroke .3s}
.ol line.on{stroke:var(--c);stroke-width:2;stroke-dasharray:none}
.ring{position:absolute;left:50%;top:50%;border-radius:50%;transform:translate(-50%,-50%);border:1px dashed var(--glow)}
.r1{width:170px;height:170px}.r2{width:250px;height:250px;border-color:var(--l2)}
.core{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:122px;height:122px;border-radius:50%;background:var(--card);
 border:3px solid var(--ink);display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:0 0 0 8px var(--glow),0 8px 24px var(--sh)}
.cl{width:30px;height:30px;color:var(--ink)}.cl svg{width:100%;height:100%}
.cb{font-size:26px;font-weight:700;line-height:1}.cs{font-size:11px;color:var(--mut)}
.cnote{position:absolute;left:50%;top:calc(50% + 86px);transform:translateX(-50%);font-size:12px;color:var(--mut);white-space:nowrap}
.ag{position:absolute;transform:translate(-50%,-50%);display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--line);
 border-radius:12px;padding:7px 10px;min-width:190px;transition:border-color .3s,box-shadow .3s,background .3s}
.ag i{width:28px;height:28px;border-radius:8px;background:var(--c);color:#fff;display:grid;place-items:center;flex:none}.ag i svg{width:16px;height:16px}
.ag b{display:block;font-size:12.5px;line-height:1.3}.ag small{display:block;font-size:11px;color:var(--mut);line-height:1.3}
.ag em{font-style:normal;font-size:12px;font-weight:700;margin-inline-start:auto;color:var(--c);padding-inline-start:6px}
.ag.on{border-color:var(--c);box-shadow:0 0 0 3px color-mix(in srgb,var(--c) 18%,transparent);background:color-mix(in srgb,var(--c) 8%,var(--card))}
@media (max-width:900px){.orbit{height:auto;display:grid;grid-template-columns:1fr 1fr;gap:8px;padding-top:150px}
 .ol,.r2,.cnote{display:none}.r1{top:66px}.core{top:66px;width:110px;height:110px}.ag{position:static;transform:none;min-width:0}}
@media (max-width:520px){.orbit{grid-template-columns:1fr}.bv{font-size:30px}.bal{text-align:right}}
/* الصفقة المميزة */
.two{display:grid;grid-template-columns:1fr 1.25fr;gap:14px}.two .card{margin-top:14px}
@media (max-width:900px){.two{grid-template-columns:1fr}}
.fh{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start}
.sym{font-size:26px;font-weight:700}.fs{text-align:left}
.ladder{position:relative;direction:ltr;height:104px;margin:14px 10px 4px}
.track{position:absolute;left:0;right:0;top:46px;height:8px;border-radius:4px;background:var(--l2);overflow:hidden}
.zn{position:absolute;top:0;bottom:0}
.mk{position:absolute;transform:translateX(-50%);display:flex;flex-direction:column;align-items:center;font-size:11px;color:var(--mut);white-space:nowrap}
.mk i{position:absolute;left:50%;width:3px;height:22px;margin-left:-1.5px;background:var(--c);border-radius:2px}
.mk b{font-size:12.5px;color:var(--c);line-height:1.2}.mk small{line-height:1.3}
.mk.r0{top:0}.mk.r0 i{top:39px}
.mk.r1{top:66px}.mk.r1 i{top:-27px}
.parts{margin-top:12px;display:grid;gap:6px}
.pt{display:grid;grid-template-columns:70px 1fr 52px;align-items:center;gap:8px;font-size:12.5px}
.pb{height:7px;background:var(--l2);border-radius:4px;overflow:hidden}.pb i{display:block;height:100%;background:var(--ink);border-radius:4px}
.pt b{font-size:12px;text-align:left}
.why{margin:12px 0 0;padding-inline-start:18px;font-size:13px}.why li{margin:2px 0}.why .rk{color:var(--a)}
/* السجل */
.log{max-height:430px;overflow:auto;margin:0 -6px}
.lday{font-size:11px;color:var(--mut);padding:6px;border-bottom:1px solid var(--line)}
.lr{display:grid;grid-template-columns:44px 74px 1fr auto;gap:8px;align-items:center;padding:7px 6px;border-bottom:1px solid var(--l2);font-size:13px}
.lr:hover{background:var(--soft)}.lt{font-size:11.5px;color:var(--mut)}
.lb b{font-size:12.5px}.lb small{display:block;font-size:11.5px;color:var(--mut);line-height:1.4}.lv{font-size:12.5px;text-align:left}
.tag{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:6px;text-align:center;white-space:nowrap}
.t-blue{background:color-mix(in srgb,var(--b) 15%,transparent);color:var(--b)}.t-green{background:color-mix(in srgb,var(--g) 15%,transparent);color:var(--g)}.t-red{background:color-mix(in srgb,var(--r) 15%,transparent);color:var(--r)}
.t-amber{background:color-mix(in srgb,var(--a) 16%,transparent);color:var(--a)}.t-grey{background:var(--l2);color:var(--mut)}.t-dark{background:var(--ink);color:var(--inkfg)}
/* الجداول */
.note{background:var(--nbg);border:1px solid var(--nln);color:var(--ntx);border-radius:12px;padding:10px 14px;margin:16px 0 0;font-size:13.5px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin:18px 0 0;position:sticky;top:0;background:var(--bg);padding:8px 0;z-index:5}
nav a{color:var(--txt);text-decoration:none;background:var(--card);border:1px solid var(--line);padding:7px 14px;border-radius:999px;font-size:13.5px}
nav a:hover{border-color:var(--ink)}
nav a.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
nav{overflow-x:auto;flex-wrap:nowrap;scrollbar-width:none}nav a{white-space:nowrap;flex:0 0 auto}
body.tabs .wrap>section{display:none}body.tabs .wrap>section.on{display:block}
#overview>.card:first-child{margin-top:12px}
section{margin:22px 0 0}h2{font-size:17px;margin:6px 0}
.calh{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}.calh button{background:var(--l2);color:var(--txt);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer}.calh button:disabled{opacity:.35;cursor:default}
.calsum{font-size:13.5px;color:var(--mut);margin-bottom:10px}
.calg{display:grid;grid-template-columns:repeat(5,1fr);gap:6px}.cwd{font-size:12px;color:var(--mut);text-align:center;padding:2px}
bdi.mono{direction:ltr;unicode-bidi:isolate}.cd .cw,.cd .cl{white-space:nowrap}
.cd{min-height:96px;border:1px solid var(--line);border-radius:10px;padding:6px 8px;display:flex;flex-direction:column;gap:2px;background:var(--soft)}
.cd.empty{border:0;background:none}.cd .dn{font-size:12px;color:var(--mut)}.cd i{font-style:normal;color:var(--mut);font-size:11px}
.cd .cw{color:var(--g);font-size:12.5px}.cd .cl{color:var(--r);font-size:12.5px}.cd bdi{direction:ltr;unicode-bidi:isolate}
.cd .cn{font-size:14px;margin-top:auto}.cd.pos{border-color:var(--g)}.cd.neg{border-color:var(--r)}
.cd .co{color:#d97706;font-size:12.5px;white-space:nowrap}.cd.live{box-shadow:inset 0 0 0 1px #d97706}
.cd.live .dn::after{content:'';display:inline-block;width:6px;height:6px;border-radius:50%;background:#d97706;margin-inline-start:5px;vertical-align:middle;animation:pulse 1.6s infinite}
@keyframes pulse{50%{opacity:.25}}
@media(max-width:640px){.cd{min-height:64px;padding:4px}.cd .cw,.cd .cl,.cd .co{font-size:10.5px}.cd i{display:none}.cd .cn{font-size:11.5px}}
.tw{overflow-x:auto;border:1px solid var(--line);border-radius:14px;background:var(--card);margin-top:8px}
table{border-collapse:collapse;width:100%;min-width:900px}
th,td{padding:9px 11px;border-bottom:1px solid var(--l2);text-align:right;vertical-align:top;font-size:13px}
th{background:var(--soft);color:var(--mut);font-weight:600;position:sticky;top:0}tr:hover td{background:var(--soft)}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:8px 0 12px}
.sc{margin:0;border-top:3px solid var(--ac)}.ct{color:var(--mut);font-size:13px}
.big{font-size:26px;font-weight:700;text-align:right}
.kv{display:flex;justify-content:space-between;font-size:13px;border-top:1px solid var(--l2);padding:4px 0}.kv b{direction:ltr}
.empty{color:var(--mut);font-size:13.5px;padding:16px;text-align:center;background:var(--soft);border-radius:12px}
footer{margin-top:28px;text-align:center}
.study{margin:6px 0 0;padding-inline-start:20px;line-height:1.9;font-size:14px}.study li{margin:2px 0}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
/*CHARTS_CSS*/</style></head><body>
"""
HEAD = HEAD.replace("/*CHARTS_CSS*/", CH.CSS + EQ.CSS)

SCRIPT = """<script>
(function(){
  // مواعيد الفحص (بتوقيت نيويورك، الاثنين–الجمعة): قبل الافتتاح 7:02–9:17 كل 15 دقيقة،
  // الجلسة 9:32–15:57 كل 5 دقائق، بعد الإغلاق 16:02–19:47 كل 15 دقيقة. العرض بتوقيت الرياض.
  function slotOk(m,min){ if(m>=7*60+2&&m<9*60+30) return min%15===2;
    if(m>=9*60+30&&m<16*60) return min%5===2; if(m>=16*60&&m<20*60) return min%15===2; return false; }
  var bar=document.querySelector('.bar'), updated=bar.getAttribute('data-updated');
  var nyF=new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',hour12:false,weekday:'short',hour:'2-digit',minute:'2-digit'});
  var ryF=new Intl.DateTimeFormat('ar-SA-u-nu-latn',{timeZone:'Asia/Riyadh',weekday:'long',hour:'numeric',minute:'2-digit',hour12:true});
  function ny(d){var o={};nyF.formatToParts(d).forEach(function(p){o[p.type]=p.value});return {wd:o.weekday,m:(+o.hour%24)*60+(+o.minute),min:+o.minute};}
  function nextScan(now){
    var t=new Date(now.getTime()); t.setSeconds(0,0); t=new Date(t.getTime()+60000);
    for(var i=0;i<6*24*60;i++){
      var p=ny(t);
      if(p.wd!=='Sat'&&p.wd!=='Sun'&&slotOk(p.m,p.min)) return t;
      t=new Date(t.getTime()+60000);
    }
    return null;
  }
  function fmt(sec){sec=Math.max(0,Math.round(sec));var d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60),s=sec%60;
    function z(x){return (x<10?'0':'')+x}var t=(h||d?z(h)+':'+z(m):m)+':'+z(s);return d?d+'ي '+t:t;}
  var cd=document.getElementById('cd'),nat=document.getElementById('nat'),nlbl=document.getElementById('nlbl'),age=document.getElementById('age');
  var live=document.getElementById('live'),dot=document.getElementById('dot');
  function tick(){
    var now=new Date(), n=nextScan(now), open=ny(now);
    var wk=open.wd!=='Sat'&&open.wd!=='Sun', m=open.m;
    var ph=!wk?'':m>=7*60&&m<9*60+30?'pre':m>=9*60+30&&m<16*60?'reg':m>=16*60&&m<20*60?'post':'';
    live.textContent=ph==='reg'?'يعمل الآن — الجلسة مفتوحة':ph==='pre'?'يعمل الآن — ما قبل الافتتاح':ph==='post'?'يعمل الآن — بعد الإغلاق':'السوق مغلق';
    dot.className='dot'+(ph?' on':'');
    if(n){
      nlbl.textContent=ph?'الفحص التالي بعد':'أول فحص بعد';
      cd.textContent=fmt((n-now)/1000);
      nat.textContent='('+ryF.format(n)+' بتوقيت الرياض — الموقع يتحدث كل 10 دقائق تقريباً، وفوراً عند أي تنبيه)';
    }
    if(updated){var a=(now-new Date(updated))/60000;
      age.textContent='آخر تحديث قبل '+(a<1?'أقل من دقيقة':a<60?Math.floor(a)+' دقيقة':a<1440?Math.floor(a/60)+' ساعة':Math.floor(a/1440)+' يوم');}
  }
  function reload(){var b=document.getElementById('refresh');b.disabled=true;b.textContent='جارٍ التحديث…';
    location.replace(location.pathname+'?t='+Date.now()+location.hash);}
  document.getElementById('refresh').onclick=reload;
  document.getElementById('reload2').onclick=function(e){e.preventDefault();reload();};
  // كل دقيقة: هل نُشرت نسخة أحدث من الصفحة المفتوحة؟
  function poll(){
    if(location.protocol==='file:')return;
    fetch('status.json?t='+Date.now(),{cache:'no-store'}).then(function(r){return r.ok?r.json():null}).then(function(j){
      if(j&&j.updated&&j.updated!==updated){document.getElementById('newv').hidden=false;
        var done=null;try{done=sessionStorage.getItem('hu_auto')}catch(e){}
        if(document.visibilityState==='visible'&&done!==j.updated){try{sessionStorage.setItem('hu_auto',j.updated)}catch(e){}setTimeout(reload,1500);}}
    }).catch(function(){});
  }
  // مدار الوكلاء: يمر الضوء على المراحل بترتيب الفحص (ماسح ← فلتر ← … ← فاتت)
  var ags=[].slice.call(document.querySelectorAll('.ag')), lns=[].slice.call(document.querySelectorAll('.ol line')), k=0;
  function step(){ags.forEach(function(a,i){a.classList.toggle('on',i===k)});lns.forEach(function(l,i){l.classList.toggle('on',i===k)});k=(k+1)%(ags.length||1);}
  if(ags.length){step();setInterval(step,1500);}
  // المظهر: فاتح / داكن / ليلي / أزرق — يُحفظ في هذا المتصفح
  var root=document.documentElement, tb=[].slice.call(document.querySelectorAll('#thm button'));
  function setT(t){root.setAttribute('data-theme',t);tb.forEach(function(b){b.classList.toggle('on',b.getAttribute('data-t')===t)});
    try{localStorage.setItem('hu_theme',t)}catch(e){}}
  tb.forEach(function(b){b.onclick=function(){setT(b.getAttribute('data-t'))}});
  setT(root.getAttribute('data-theme')||'light');
  tick(); setInterval(tick,1000); poll(); setInterval(poll,60000);
})();
</script>
<script>
document.querySelectorAll('.calm').forEach(function(m){
  function go(d){var i=+m.getAttribute('data-i')+d, t=document.querySelector('.calm[data-i="'+i+'"]');
    if(t){m.hidden=true;t.hidden=false;}}
  var p=m.querySelector('.calp'), n=m.querySelector('.caln');
  if(p)p.onclick=function(){go(1)}; if(n)n.onclick=function(){go(-1)};
});
</script>
"""
