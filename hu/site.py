# -*- coding: utf-8 -*-
"""يولّد موقعاً ثابتاً (docs/index.html) من قاعدة البيانات — يُنشر مجاناً على GitHub Pages."""
import html
import json
from datetime import datetime
from typing import List

import intraday as I
from hu import rules as RL
from hu import trades as TR

E = lambda x: html.escape("" if x is None else str(x))
STATE_AR = {"OPEN": ("🟡 مفتوحة", "#fbbf24"), "ACTIVATED": ("✅ تفعّلت", "#4ade80"), "INVALIDATED": ("❌ أُلغيت", "#f87171"),
            "EXPIRED": ("⌛ انتهت بلا حسم", "#9da7b3")}
TSTAT = {"OPEN": "مفتوحة", "PENDING": "معلّقة", "STOP": "وقف", "TARGET": "هدف", "TIME": "نهاية المدة",
         "EXPIRED": "لم تُنفّذ", "CANCELLED": "أُلغيت قبل التنفيذ"}
KIND = {"A": "A عند التنبيه", "B": "B عند الاختراق", "BASE": "مقارنة"}


def t_et(iso):
    if not iso:
        return "—"
    d = datetime.fromisoformat(iso)
    return d.astimezone(I.ET).strftime("%m-%d %H:%M")


def money(v):
    return "—" if v is None else f"{'+' if v > 0 else ''}{v:,.2f}$"


def sum_card(title: str, s: dict) -> str:
    if not s.get("trades"):
        body = f"<div class='muted'>لا صفقات مغلقة بعد • مفتوحة {s.get('open', 0)}</div>"
    else:
        col = "#4ade80" if s["total_pnl"] > 0 else "#f87171"
        avg_r = "—" if s["avg_r"] is None else f"{s['avg_r']:+.2f}"
        body = (f"<div class='big' style='color:{col}'>{money(s['total_pnl'])}</div>"
                f"<div class='kv'><span>الصفقات</span><b>{s['trades']}</b></div>"
                f"<div class='kv'><span>نسبة الربح</span><b>{s['win_rate']:.0%}</b></div>"
                f"<div class='kv'><span>متوسط الربح / الخسارة</span><b>{money(s['avg_win'])} / {money(s['avg_loss'])}</b></div>"
                f"<div class='kv'><span>متوسط الصفقة</span><b>{s['avg_pct']:+.2f}%</b></div>"
                f"<div class='kv'><span>متوسط R</span><b>{avg_r}</b></div>"
                f"<div class='kv'><span>أكبر تراجع</span><b>{money(s['max_drawdown'])}</b></div>"
                f"<div class='kv'><span>لم تُنفّذ / مفتوحة</span><b>{s.get('not_filled', 0)} / {s['open']}</b></div>")
    return f"<div class='card'><div class='ct'>{E(title)}</div>{body}</div>"


def table(head: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "<div class='muted' style='padding:14px'>لا شيء بعد.</div>"
    th = "".join(f"<th>{h}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='tw'><table><thead><tr>{th}</tr></thead><tbody>{tr}</tbody></table></div>"


def build(store, out_path: str, cfg: dict) -> None:
    runs = store.q("SELECT * FROM runs ORDER BY id DESC LIMIT 60")
    last_scan = store.one("SELECT id, started_utc FROM runs WHERE id IN (SELECT DISTINCT run_id FROM candidates) ORDER BY id DESC LIMIT 1")
    cands = store.q("SELECT * FROM candidates WHERE run_id=? ORDER BY qualifies DESC, score DESC", (last_scan["id"],)) if last_scan else []
    alerts = store.q("SELECT * FROM alerts ORDER BY id DESC LIMIT 300")
    trades = store.q("SELECT * FROM trades ORDER BY id DESC")
    missed = store.q("SELECT * FROM missed ORDER BY day DESC, max_gain_pct DESC LIMIT 200")

    # ---- المرشحون
    crow = []
    for c in cands[:60]:
        d = json.loads(c["data"])
        ok = c["qualifies"]
        why = "<br>".join(E(x) for x in d.get("reasons", [])[:5])
        bad = "<br>".join(E(x) for x in (d.get("excluded", []) + d.get("gates_failed", []))[:4])
        risks = "<br>".join(E(x) for x in d.get("risks", [])[:3])
        stage = "🟡 مبكرة" if d.get("stage") == "EARLY" else "🔺 فاتت/اخترق"
        crow.append([f"<b>{E(c['symbol'])}</b><div class='muted'>{E((d.get('name') or '')[:28])}</div>",
                     f"<b style='color:{'#4ade80' if ok else '#9da7b3'}'>{c['score']}</b>" + (" ✔" if ok else ""),
                     stage, I.fmt_price(d.get("price")),
                     f"{d['rvol']:.1f}×" if d.get("rvol") else "—",
                     f"{I.fmt_price(d.get('trigger'))} / {I.fmt_price(d.get('invalidation'))}",
                     why or "—", (f"<span class='bad'>{bad}</span>" if bad else "") + (f"<div class='warn'>{risks}</div>" if risks else ""),
                     f"{RL.quality_label(d.get('data_quality', 0))} ({d.get('data_quality', 0)})"])
    cand_html = table(["السهم", "الدرجة", "المرحلة", "السعر", "RVOL", "الدخول / الإبطال", "سبب الإشارة", "ما يمنع / المخاطر", "جودة البيانات"], crow)

    # ---- التنبيهات
    arow = []
    for a in alerts:
        lab, col = STATE_AR.get(a["state"], (a["state"], "#9da7b3"))
        arow.append([E(a["day"]), t_et(a["alert_utc"]), f"<b>{E(a['symbol'])}</b>", str(a["score"]), I.fmt_price(a["price"]),
                     I.fmt_price(a["trigger"]), I.fmt_price(a["invalidation"]), f"<span style='color:{col}'>{lab}</span>",
                     "—" if a["lead_min"] is None else f"{a['lead_min']:.0f} د",
                     "—" if a["gain_from_open"] is None else f"{a['gain_from_open']:+.1f}%",
                     f"{a['data_quality']}", "✓" if a["sent"] else "✗"])
    alert_html = table(["اليوم", "الوقت (نيويورك)", "السهم", "الدرجة", "السعر", "شرط الدخول", "الإبطال", "الحالة",
                        "السبق قبل الاختراق", "التغير عند التنبيه", "جودة البيانات", "أُرسل"], arow)

    # ---- الصفقات
    sA, sB, sBase = (TR.summary([t for t in trades if t["kind"] == k]) for k in ("A", "B", "BASE"))
    summ = ("<div class='grid'>" + sum_card("A: الدخول عند التنبيه", sA) + sum_card("B: الدخول عند الاختراق", sB)
            + sum_card("المقارنة: أي سهم قفز حجمه", sBase) + "</div>")
    trow = []
    for t in trades[:400]:
        col = "#4ade80" if (t["pnl"] or 0) > 0 else "#f87171" if (t["pnl"] or 0) < 0 else "#c9d1d9"
        trow.append([E(t["day"]), E(KIND.get(t["kind"], t["kind"])), f"<b>{E(t['symbol'])}</b>", t_et(t["entry_utc"] or t["created_utc"]),
                     I.fmt_price(t["entry"]), I.fmt_price(t["stop"]), I.fmt_price(t["target"]), str(t["shares"]),
                     E(TSTAT.get(t["status"], t["status"])), I.fmt_price(t["exit"]), t_et(t["exit_utc"]),
                     f"<b style='color:{col}'>{money(t['pnl'])}</b>", "—" if t["r_mult"] is None else f"{t['r_mult']:+.2f}R",
                     f"<span class='muted'>{E(t['note'])}</span>"])
    trade_html = summ + table(["اليوم", "النوع", "السهم", "الدخول (نيويورك)", "سعر الدخول", "الوقف", "الهدف", "الكمية", "الحالة",
                               "الخروج", "وقت الخروج", "الربح", "R", "ملاحظة"], trow)

    # ---- فاتت
    mrow = [[E(m["day"]), f"<b>{E(m['symbol'])}</b>", I.fmt_price(m["prev_close"]), f"+{m['max_gain_pct']:.0f}%",
             f"{m['close_gain_pct']:+.0f}%", "نعم" if m["was_candidate"] else "لا", "—" if m["best_score"] is None else str(m["best_score"]),
             f"<span class='muted'>{E(m['reason'])}</span>"] for m in missed]
    missed_html = table(["اليوم", "السهم", "إغلاق أمس", "أعلى ارتفاع", "عند آخر فحص", "كان مرشحاً؟", "أعلى درجة", "لماذا لم يُنبَّه"], mrow)

    # ---- التشغيلات
    rrow = [[E(r["id"]), E(r["kind"]), t_et(r["started_utc"]), "—" if r["delay_min"] is None else f"{r['delay_min']:.0f} د",
             str(r["swept"] or 0), str(r["shortlist"] or 0), str(r["qualified"] or 0), str(r["alerts"] or 0),
             str(r["api_calls"] or 0), "—" if r["duration_sec"] is None else f"{r['duration_sec']:.0f} ث",
             f"<span class='bad'>{E((r['errors'] or '')[:200])}</span>"] for r in runs]
    runs_html = table(["#", "النوع", "البدء (نيويورك)", "تأخر عن الموعد", "أسهم ممسوحة", "قائمة قصيرة", "مؤهلة", "تنبيهات",
                       "طلبات Alpaca", "المدة", "أخطاء"], rrow)

    upd = runs[0]["started_utc"] if runs else None
    upd_txt = "—" if not upd else (datetime.fromisoformat(upd).astimezone(I.RIYADH).strftime("%Y-%m-%d %H:%M") + " الرياض • "
                                   + datetime.fromisoformat(upd).astimezone(I.ET).strftime("%H:%M") + " نيويورك")
    page = f"""<!DOCTYPE html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>تنبيه مبكر — أسهم تحت 5$</title>
<style>
:root{{--bg:#0b0f14;--card:#141a22;--line:#26303c;--txt:#e6edf3;--mut:#8b96a5}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--txt);font:15px/1.6 "IBM Plex Sans Arabic","Segoe UI",Tahoma,sans-serif}}
header{{padding:22px 18px 8px;max-width:1300px;margin:auto}}h1{{margin:0;font-size:22px}}
.muted{{color:var(--mut);font-size:13px}}.bad{{color:#fca5a5;font-size:13px}}.warn{{color:#fde68a;font-size:13px}}
.note{{background:#1a1406;border:1px solid #6b4e0a;color:#fde68a;border-radius:10px;padding:10px 14px;margin:12px 0;font-size:14px}}
nav{{display:flex;gap:6px;flex-wrap:wrap;max-width:1300px;margin:8px auto;padding:0 18px}}
nav a{{color:var(--txt);text-decoration:none;background:var(--card);border:1px solid var(--line);padding:8px 14px;border-radius:10px;font-size:14px}}
nav a:hover{{border-color:#3b82f6}}section{{max-width:1300px;margin:18px auto;padding:0 18px}}h2{{font-size:18px;margin:8px 0}}
.tw{{overflow-x:auto;border:1px solid var(--line);border-radius:12px}}table{{border-collapse:collapse;width:100%;min-width:900px}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top;font-size:13.5px}}
th{{background:#111821;color:var(--mut);font-weight:600;position:sticky;top:0}}tr:hover td{{background:#121922}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}}.ct{{color:var(--mut);font-size:13px}}
.big{{font-size:26px;font-weight:700;direction:ltr;text-align:right}}.kv{{display:flex;justify-content:space-between;font-size:13.5px;border-top:1px solid var(--line);padding:4px 0}}
.kv b{{direction:ltr}}
.bar{{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;margin-top:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px}}
.bar button{{background:#1f6feb;color:#fff;border:0;border-radius:9px;padding:8px 16px;font:inherit;font-weight:600;cursor:pointer}}
.bar button:hover{{background:#388bfd}}.bar button:disabled{{opacity:.6;cursor:wait}}
.tm{{display:flex;align-items:baseline;gap:8px}}.tm b{{font:700 22px/1 ui-monospace,Consolas,monospace;direction:ltr;color:#fbbf24}}
.new{{color:#4ade80;font-weight:600}}.new a{{color:#4ade80}}
</style></head><body>
<header><h1>🔔 التنبيه المبكر — أسهم أمريكية تحت 5$</h1>
<div class="muted">آخر تحديث: {E(upd_txt)} • فحص كل 15 دقيقة أثناء الجلسة</div>
<div class="bar" data-updated="{E(upd or '')}">
  <button id="refresh" type="button">🔄 تحديث</button>
  <div class="tm"><span class="lbl" id="nlbl">الفحص التالي بعد</span><b id="cd">—</b><span class="muted" id="nat"></span></div>
  <div class="muted" id="age"></div>
  <div class="new" id="newv" hidden>✨ وصل تحديث جديد — <a href="#" id="reload2">اعرضه</a></div>
</div>
<div class="note">البيانات من Alpaca المجانية: السوق الكامل متأخر 15 دقيقة، والسعر اللحظي من IEX فقط (جزء صغير من التداول).
Float غير متوفر مجاناً. التنبيهات ليست توصية شراء، والصفقات افتراضية فقط. النظام لا يكتشف كل انفجار — راجع قسم «فاتت».</div></header>
<nav><a href="#cands">المرشحون الآن</a><a href="#alerts">سجل التنبيهات</a><a href="#trades">الصفقات الافتراضية</a><a href="#missed">فاتت</a><a href="#runs">التشغيلات</a></nav>
<section id="cands"><h2>المرشحون في آخر فحص ({E(t_et(last_scan['started_utc']) if last_scan else '—')} نيويورك)</h2>
<div class="muted">مرتبون حسب الدرجة. ✔ = استوفى كل القواعد (حد التنبيه {cfg['min_score']}).</div>{cand_html}</section>
<section id="alerts"><h2>سجل التنبيهات</h2>{alert_html}</section>
<section id="trades"><h2>الصفقات الافتراضية ({cfg['shares_per_trade']} سهم لكل صفقة)</h2>{trade_html}</section>
<section id="missed"><h2>فاتت: ارتفعت 30%+ بلا تنبيه</h2><div class="muted">لا نخفي ما فات: كل سهم تحت 5$ ارتفع 30%+ اليوم بلا تنبيه، ولماذا.</div>{missed_html}</section>
<section id="runs"><h2>التشغيلات</h2><div class="muted">التأخر = الفرق بين موعد الجدولة ووقت البدء الفعلي على GitHub.</div>{runs_html}</section>
<script>
(function(){{
  // مواعيد الفحص: الدقائق 02 و17 و32 و47 من كل ساعة، من 9:32 إلى 15:47 بتوقيت نيويورك، الاثنين–الجمعة.
  var SLOTS=[2,17,32,47], FIRST=9*60+32, LAST=15*60+47, PUBLISH_MIN=3;
  var bar=document.querySelector('.bar'), updated=bar.getAttribute('data-updated');
  var nyF=new Intl.DateTimeFormat('en-US',{{timeZone:'America/New_York',hour12:false,weekday:'short',hour:'2-digit',minute:'2-digit'}});
  var ryF=new Intl.DateTimeFormat('ar-SA-u-nu-latn',{{timeZone:'Asia/Riyadh',weekday:'long',hour:'numeric',minute:'2-digit'}});
  function ny(d){{var o={{}};nyF.formatToParts(d).forEach(function(p){{o[p.type]=p.value}});return {{wd:o.weekday,m:(+o.hour%24)*60+(+o.minute),min:+o.minute}};}}
  function nextScan(now){{
    var t=new Date(now.getTime()); t.setSeconds(0,0); t=new Date(t.getTime()+60000);
    for(var i=0;i<6*24*60;i++){{
      var p=ny(t);
      if(p.wd!=='Sat'&&p.wd!=='Sun'&&p.m>=FIRST&&p.m<=LAST&&SLOTS.indexOf(p.min)>=0) return t;
      t=new Date(t.getTime()+60000);
    }}
    return null;
  }}
  function fmt(sec){{sec=Math.max(0,Math.round(sec));var d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60),s=sec%60;
    function z(x){{return (x<10?'0':'')+x}}var t=(h||d?z(h)+':'+z(m):m)+':'+z(s);return d?d+'ي '+t:t;}}
  var cd=document.getElementById('cd'),nat=document.getElementById('nat'),nlbl=document.getElementById('nlbl'),age=document.getElementById('age');
  function tick(){{
    var now=new Date(), n=nextScan(now);
    if(n){{
      var left=(n-now)/1000, open=ny(now), inSession=open.wd!=='Sat'&&open.wd!=='Sun'&&open.m>=9*60+30&&open.m<16*60;
      nlbl.textContent=inSession?'الفحص التالي بعد':'السوق مغلق — أول فحص بعد';
      cd.textContent=fmt(left);
      nat.textContent='('+ryF.format(n)+' بتوقيت الرياض، والموقع يتحدث بعده بدقيقة إلى ثلاث)';
    }}
    if(updated){{var a=(now-new Date(updated))/60000;
      age.textContent='آخر تحديث قبل '+(a<1?'أقل من دقيقة':a<60?Math.floor(a)+' دقيقة':a<1440?Math.floor(a/60)+' ساعة':Math.floor(a/1440)+' يوم');}}
  }}
  function reload(){{var b=document.getElementById('refresh');b.disabled=true;b.textContent='⏳ جارٍ التحديث…';
    location.replace(location.pathname+'?t='+Date.now()+location.hash);}}
  document.getElementById('refresh').onclick=reload;
  document.getElementById('reload2').onclick=function(e){{e.preventDefault();reload();}};
  // كل دقيقة: هل نُشرت نسخة أحدث من الصفحة المفتوحة؟
  function poll(){{
    if(location.protocol==='file:')return;
    fetch('status.json?t='+Date.now(),{{cache:'no-store'}}).then(function(r){{return r.ok?r.json():null}}).then(function(j){{
      if(j&&j.updated&&j.updated!==updated){{document.getElementById('newv').hidden=false;
        var done=null;try{{done=sessionStorage.getItem('hu_auto')}}catch(e){{}}
        if(document.visibilityState==='visible'&&done!==j.updated){{try{{sessionStorage.setItem('hu_auto',j.updated)}}catch(e){{}}setTimeout(reload,1500);}}}}
    }}).catch(function(){{}});
  }}
  tick(); setInterval(tick,1000); poll(); setInterval(poll,60000);
}})();
</script>
</body></html>"""
    import os
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    open(out_path, "w", encoding="utf-8").write(page)
    # ملف صغير تقرؤه الصفحة كل دقيقة لتعرف هل نُشر تحديث جديد
    json.dump({"updated": upd, "run": runs[0]["id"] if runs else None},
              open(os.path.join(os.path.dirname(out_path) or ".", "status.json"), "w", encoding="utf-8"))
