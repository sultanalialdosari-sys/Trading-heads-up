# -*- coding: utf-8 -*-
"""رسوم TradingView (Lightweight Charts) لكل تنبيه وصفقة.

البيانات: شموع 5 دقائق لكل سهم في يوم التنبيه/الصفقة، تُحفظ في جدول chart_bars بلا طلبات إضافية تقريباً:
  - عند التنبيه: من شموع الفحص (SIP متأخر + IEX للحظة).
  - مع كل متابعة: من شموع الدقيقة التي تُجلب أصلاً لمحاكاة الصفقات.
  - بعد الإغلاق: طلب واحد مجمّع لكل اليوم من SIP (04:00–16:04 نيويورك) يستبدل ما سبق.
المكتبة: docs/vendor/lightweight-charts (Apache-2.0، © TradingView) — نسخة مثبتة داخل المستودع.
"""
import html
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

import intraday as I

RIYADH_OFFSET = 3 * 3600        # الرياض UTC+3 بلا توقيت صيفي: نزيح الوقت ليعرض المحور توقيت الرياض
MAX_CHARTS = 40
KEEP_DAYS = 45
LIB = "vendor/lightweight-charts.standalone.production.js"


# ====================================================================== البيانات
def to_rows(df: Optional[pd.DataFrame]) -> List[list]:
    """DataFrame (فهرس زمني، open/high/low/close/volume) ← صفوف 5 دقائق [epoch_utc, o, h, l, c, v]."""
    if df is None or len(df) == 0:
        return []
    d = df[["open", "high", "low", "close", "volume"]].copy()
    idx = pd.DatetimeIndex(d.index)
    d.index = idx.tz_convert("UTC") if idx.tz is not None else idx.tz_localize("UTC")
    d = d.resample("5min", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open"])
    return [[int(t.timestamp()), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)]
            for t, r in d.iterrows()]


def merge_rows(old: List[list], new: List[list]) -> List[list]:
    """الجديد يحل محل القديم لنفس الشمعة (SIP بعد الإغلاق أدق من IEX اللحظي)."""
    m = {r[0]: r for r in old}
    m.update({r[0]: r for r in new})
    return [m[k] for k in sorted(m)]


def save(db, day: str, symbol: str, df: Optional[pd.DataFrame], source: str, replace: bool = False) -> int:
    rows = to_rows(df)
    if not rows:
        return 0
    prev = db.one("SELECT bars FROM chart_bars WHERE day=? AND symbol=?", (day, symbol))
    if prev and not replace:
        rows = merge_rows(json.loads(prev["bars"]), rows)
    db.db.execute("INSERT OR REPLACE INTO chart_bars(day, symbol, bars, source, updated_utc) VALUES (?,?,?,?,?)",
                  (day, symbol, json.dumps(rows, separators=(",", ":")), source, datetime.now(timezone.utc).isoformat()))
    return len(rows)


def chart_symbols(db, day: str) -> List[str]:
    """الأسهم التي تستحق رسماً: كل تنبيه، وكل صفقة عدا صفقات المقارنة العشوائية."""
    a = {r["symbol"] for r in db.q("SELECT symbol FROM alerts WHERE day=?", (day,))}
    t = {r["symbol"] for r in db.q("SELECT symbol FROM trades WHERE day=? AND kind!='BASE'", (day,))}
    return sorted(a | t)


def finalize_day(client, db, day: str, now: datetime) -> int:
    """بعد الإغلاق: اليوم كاملاً من SIP (المسموح مجاناً إذا أقدم من 15 دقيقة)."""
    syms = chart_symbols(db, day)
    if not syms:
        return 0
    d = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime(d.year, d.month, d.day, 4, 0, tzinfo=I.ET)
    end = min(datetime(d.year, d.month, d.day, 20, 0, tzinfo=I.ET), now - timedelta(minutes=16))
    n = 0
    for i in range(0, len(syms), 50):
        bars = client.bars(syms[i:i + 50], "5Min", start, end, feed="sip")
        for s, df in bars.items():
            n += 1 if save(db, day, s, df, "sip", replace=True) else 0
    cutoff = (d - timedelta(days=KEEP_DAYS)).isoformat()
    db.db.execute("DELETE FROM chart_bars WHERE day < ?", (cutoff,))
    return n


def backfill(client, db, now: datetime, days: int = 10) -> dict:
    """أيام سابقة فيها تنبيهات/صفقات بلا رسوم كاملة ← تُكمل من SIP (يُستدعى بعد كل إغلاق، ويدوياً)."""
    today = now.astimezone(I.ET).date()
    since = (today - timedelta(days=days)).isoformat()
    rows = db.q("SELECT day FROM alerts WHERE day>=? UNION SELECT day FROM trades WHERE day>=? AND kind!='BASE'", (since, since))
    done = {}
    for d in sorted({r["day"] for r in rows}):
        if d >= today.isoformat():
            continue                               # اليوم الجاري يُكمل عند الإغلاق
        have = {r["symbol"] for r in db.q("SELECT symbol FROM chart_bars WHERE day=? AND source='sip'", (d,))}
        if set(chart_symbols(db, d)) - have:
            done[d] = finalize_day(client, db, d, now)
    return done


# ====================================================================== الموقع
def _iso_ts(x: Optional[str]) -> Optional[int]:
    if not x:
        return None
    try:
        return int(datetime.fromisoformat(x).timestamp())
    except ValueError:
        return None


def _snap(ts: Optional[int], bars: List[list]) -> Optional[int]:
    """الشارة توضع على الشمعة التي تحتوي اللحظة (بداية شمعة 5 دقائق)."""
    if ts is None or not bars:
        return None
    t = ts - ts % 300
    return t if bars[0][0] <= t <= bars[-1][0] + 300 else None


def build_items(store, alerts: List[dict], trades: List[dict]) -> List[dict]:
    rows = store.q("SELECT day, symbol, bars, source FROM chart_bars ORDER BY day DESC")
    by_key = {(r["day"], r["symbol"]): r for r in rows}
    groups: Dict[tuple, dict] = {}
    for a in alerts:
        groups.setdefault((a["day"], a["symbol"]), {"alerts": [], "trades": []})["alerts"].append(a)
    for t in trades:
        if t["kind"] != "BASE":
            groups.setdefault((t["day"], t["symbol"]), {"alerts": [], "trades": []})["trades"].append(t)
    items = []
    for (day, sym), g in sorted(groups.items(), key=lambda kv: kv[0], reverse=True):
        r = by_key.get((day, sym))
        if not r:
            continue
        bars = json.loads(r["bars"])
        if len(bars) < 2:
            continue
        lines, marks = [], []
        for a in g["alerts"]:
            if a.get("trigger"):
                lines.append({"p": a["trigger"], "t": "التفعيل", "k": "b"})
            if a.get("invalidation"):
                lines.append({"p": a["invalidation"], "t": "الإبطال", "k": "r"})
            t = _snap(_iso_ts(a.get("alert_utc")), bars)
            if t:
                marks.append({"t": t, "k": "b", "s": "arrowDown", "p": "aboveBar", "x": "تنبيه"})
        for t in g["trades"]:
            k = t["kind"]
            if t.get("entry"):
                lines.append({"p": t["entry"], "t": f"دخول {k}", "k": "a"})
            if t.get("target"):
                lines.append({"p": t["target"], "t": f"هدف {k}", "k": "g"})
            if t.get("stop") and not any(abs(x["p"] - t["stop"]) < 1e-9 for x in lines):
                lines.append({"p": t["stop"], "t": f"وقف {k}", "k": "r"})
            te = _snap(_iso_ts(t.get("entry_utc")), bars)
            if te:
                marks.append({"t": te, "k": "a", "s": "arrowUp", "p": "belowBar", "x": k})
            tx = _snap(_iso_ts(t.get("exit_utc")), bars)
            if tx:
                win = (t.get("pnl") or 0) > 0
                marks.append({"t": tx, "k": "g" if win else "r", "s": "circle", "p": "aboveBar",
                              "x": f"{k} {t.get('r_mult'):+.1f}R" if t.get("r_mult") is not None else f"خروج {k}"})
        seen, uniq = set(), []
        for ln in lines:
            key = (round(ln["p"], 6), ln["t"])
            if key not in seen:
                seen.add(key)
                uniq.append(ln)
        pnl = sum(t["pnl"] for t in g["trades"] if t.get("pnl") is not None)
        closed = any(t.get("pnl") is not None for t in g["trades"])
        kinds = sorted({a.get("kind") or "EARLY" for a in g["alerts"]} | {t["kind"] for t in g["trades"]})
        items.append({
            "id": f"{day}_{sym}", "day": day, "sym": sym, "kinds": kinds, "src": r["source"],
            "pnl": round(pnl, 2) if closed else None,
            "bars": [[b[0] + RIYADH_OFFSET, *b[1:]] for b in bars],
            "lines": uniq, "marks": sorted(({**m, "t": m["t"] + RIYADH_OFFSET} for m in marks), key=lambda m: m["t"]),
        })
        if len(items) >= MAX_CHARTS:
            break
    return items


def section_html(items: List[dict]) -> str:
    if not items:
        return ("<section id='charts'><h2>الرسوم البيانية</h2><div class='muted'>لا رسوم بعد: تظهر هنا شموع كل سهم "
                "فيه تنبيه أو صفقة، بدءاً من أول تنبيه بعد هذا التحديث.</div></section>")
    opts = []
    for it in items:
        pnl = "" if it["pnl"] is None else f" • \u2066{it['pnl']:+.2f}$\u2069"     # عزل اتجاه الرقم داخل نص عربي
        opts.append(f"<option value='{html.escape(it['id'])}'>{html.escape(it['day'])} — {html.escape(it['sym'])}"
                    f" ({html.escape('/'.join(it['kinds']))}){html.escape(pnl)}</option>")
    data = json.dumps(items, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"""<section id="charts"><h2>الرسوم البيانية (TradingView)</h2>
<div class="muted">شموع 5 دقائق بتوقيت الرياض لكل سهم فيه تنبيه أو صفقة افتراضية: خطوط التفعيل والإبطال والدخول والهدف، وشارات التنبيه والدخول والخروج.
البيانات من السوق الكامل (SIP) متأخرة 15 دقيقة، وتكتمل بعد الإغلاق.</div>
<div class="card chartbox"><div class="ch"><select id="chsel" class="chsel">{''.join(opts)}</select>
<span id="chinfo" class="muted"></span></div>
<div id="tvchart" class="tvchart" dir="ltr"></div>
<div id="chlegend" class="chlegend"></div>
<div class="muted tvattr" dir="ltr">Charts: TradingView Lightweight Charts™ — Copyright (c) 2026 TradingView, Inc. —
<a href="https://www.tradingview.com/" target="_blank" rel="noopener">tradingview.com</a> — Apache-2.0</div></div>
<script type="application/json" id="chdata">{data}</script></section>"""


CSS = """
.chartbox{padding:12px}.chsel{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--soft);color:var(--txt);max-width:100%}
.tvchart{height:440px;width:100%;margin-top:10px}.chlegend{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:8px;font-size:13px}
.chlegend span{display:inline-flex;align-items:center;gap:6px}.chlegend i{display:inline-block;width:14px;height:3px;border-radius:2px}
.tvattr{margin-top:6px;font-size:12px}.tvattr a{color:var(--b)}@media(max-width:700px){.tvchart{height:340px}}
"""

SCRIPT = """<script src="%s"></script>
<script>
(function(){
 var el=document.getElementById('tvchart'), dataEl=document.getElementById('chdata');
 if(!el||!dataEl||!window.LightweightCharts) return;
 var items=JSON.parse(dataEl.textContent), byId={}; items.forEach(function(x){byId[x.id]=x});
 var LC=window.LightweightCharts, chart=null, cs, vs;
 function v(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()||'#888'}
 function col(k){return {g:v('--g'),r:v('--r'),b:v('--b'),a:v('--a')}[k]||v('--mut')}
 function fmt(p){return p<1?p.toFixed(4):p.toFixed(2)}
 function draw(id){
  var it=byId[id]; if(!it) return;
  if(chart){chart.remove();chart=null}
  chart=LC.createChart(el,{autoSize:true,
   layout:{background:{type:'solid',color:v('--card')},textColor:v('--txt'),attributionLogo:true},
   grid:{vertLines:{color:v('--l2')},horzLines:{color:v('--l2')}},
   rightPriceScale:{borderColor:v('--line')},
   timeScale:{borderColor:v('--line'),timeVisible:true,secondsVisible:false,minBarSpacing:0.5},
   localization:{priceFormatter:fmt}});
  cs=chart.addSeries(LC.CandlestickSeries,{upColor:v('--g'),downColor:v('--r'),borderVisible:false,
   wickUpColor:v('--g'),wickDownColor:v('--r'),priceFormat:{type:'price',precision:it.bars[0][4]<1?4:2,minMove:it.bars[0][4]<1?0.0001:0.01}});
  cs.setData(it.bars.map(function(b){return {time:b[0],open:b[1],high:b[2],low:b[3],close:b[4]}}));
  vs=chart.addSeries(LC.HistogramSeries,{priceFormat:{type:'volume'},priceScaleId:'vol',color:v('--mut'),lastValueVisible:false,priceLineVisible:false});
  chart.priceScale('vol').applyOptions({scaleMargins:{top:0.8,bottom:0}});
  vs.setData(it.bars.map(function(b){return {time:b[0],value:b[5],color:(b[4]>=b[1]?v('--g'):v('--r'))+'55'}}));
  var leg=[];
  it.lines.forEach(function(l){
   cs.createPriceLine({price:l.p,color:col(l.k),lineWidth:1,lineStyle:2,axisLabelVisible:true,title:l.t});
   leg.push('<span><i style="background:'+col(l.k)+'"></i>'+l.t+' '+fmt(l.p)+'</span>')});
  if(LC.createSeriesMarkers) LC.createSeriesMarkers(cs,it.marks.map(function(m){return {time:m.t,position:m.p,shape:m.s,color:col(m.k),text:m.x}}));
  chart.timeScale().fitContent();
  document.getElementById('chlegend').innerHTML=leg.join('');
  document.getElementById('chinfo').innerHTML=(it.src==='sip'?'يوم كامل (SIP)':'جزئي — يكتمل بعد الإغلاق')+(it.pnl===null?'':' • <bdi dir="ltr">'+(it.pnl>=0?'+':'')+it.pnl.toFixed(2)+'$</bdi>');
 }
 var sel=document.getElementById('chsel');
 sel.addEventListener('change',function(){draw(sel.value)});
 window.huRedrawChart=function(){draw(sel.value)};      // الرسم يُعاد عند فتح تبويبه (كان مخفياً بلا حجم)
 draw(sel.value);
 new MutationObserver(function(){draw(sel.value)}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
})();
</script>""" % LIB
