# -*- coding: utf-8 -*-
"""منحنى الربح الافتراضي التراكمي (TradingView Lightweight Charts) مع تتبع بالماوس.

خط لكل نوع صفقات: A (عند التنبيه)، B (عند الاختراق)، قوائم قبل/بعد الجلسة، والمقارنة العشوائية.
كل خط درجي يقفز لحظة إغلاق الصفقة. مؤشر متقاطع + بطاقة عائمة: الوقت بتوقيت الرياض، رصيد كل خط
في تلك اللحظة، والصفقات التي أُغلقت عندها. أزرار المدى (اليوم / 5 أيام / الكل)، وإخفاء خط بالنقر على
مفتاحه، وتكبير وسحب بالماوس واللمس. المكتبة نفسها المستخدمة في قسم الرسوم (تُحمَّل مرة واحدة).
"""
import html
import json
from datetime import datetime
from typing import Dict, List

from hu.charts import RIYADH_OFFSET

SERIES = [  # المفتاح، الاسم، لون CSS، نمط
    ("A", "A عند التنبيه", "--g", "area"),
    ("B", "B عند الاختراق", "--b", "line"),
    ("EXT", "قبل/بعد الجلسة", "--a", "line"),
    ("BASE", "المقارنة العشوائية", "--mut", "dash"),
]
KIND_TO = {"A": "A", "B": "B", "PRE": "EXT", "POST": "EXT", "BASE": "BASE"}
NAME = {"A": "A", "B": "B", "PRE": "قبل الافتتاح", "POST": "بعد الإغلاق", "BASE": "مقارنة"}


def build(trades: List[dict]) -> dict:
    closed = [t for t in trades if t.get("pnl") is not None and t.get("exit_utc")]
    events: Dict[int, list] = {}
    for t in closed:
        try:
            ts = int(datetime.fromisoformat(t["exit_utc"]).timestamp())
        except ValueError:
            continue
        events.setdefault(ts, []).append(t)
    if not events:
        return {}
    times = sorted(events)
    run = {k: 0.0 for k, *_ in SERIES}
    pts = {k: [[times[0] - 60 + RIYADH_OFFSET, 0.0]] for k, *_ in SERIES}
    detail = {}
    for ts in times:
        for t in events[ts]:
            g = KIND_TO.get(t["kind"])
            if g:
                run[g] += t["pnl"]
        for k in run:
            pts[k].append([ts + RIYADH_OFFSET, round(run[k], 2)])
        detail[ts + RIYADH_OFFSET] = [
            {"s": t["symbol"], "k": NAME.get(t["kind"], t["kind"]), "p": round(t["pnl"], 2),
             "r": None if t.get("r_mult") is None else round(t["r_mult"], 2), "st": t.get("status")}
            for t in events[ts]]
    used = {KIND_TO.get(t["kind"]) for t in closed}
    return {"series": [{"k": k, "n": n, "c": c, "t": typ, "d": pts[k]} for k, n, c, typ in SERIES if k in used],
            "detail": detail, "n": len(closed)}


def section(trades: List[dict]) -> str:
    data = build(trades)
    if not data:
        return ("<div class='eqwrap'><div class='eqempty'>يظهر المنحنى بعد أول صفقة افتراضية مغلقة</div></div>")
    legend = "".join(f"<button type='button' class='eql on' data-k='{s['k']}'><i style='background:var({s['c']})'"
                     f"{' class=dash' if s['t'] == 'dash' else ''}></i>{html.escape(s['n'])}</button>" for s in data["series"])
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (f"<div class='eqwrap'><div class='eqtools'><div class='eqlegend'>{legend}</div>"
            f"<div class='eqrange'><button type='button' data-r='1'>اليوم</button><button type='button' data-r='5'>5 أيام</button>"
            f"<button type='button' data-r='0' class='on'>الكل</button></div></div>"
            f"<div id='eqchart' class='eqchart' dir='ltr'><div id='eqtip' class='eqtip' hidden></div></div>"
            f"<div class='muted eqhint'>مرّر الماوس (أو المس) لتتبع الرصيد • اسحب للتنقل • العجلة أو القرص للتكبير • "
            f"انقر على المفتاح لإخفاء خط • {data['n']} صفقة مغلقة</div>"
            f"<script type='application/json' id='eqdata'>{js}</script></div>")


CSS = """
.eqwrap{position:relative}.eqtools{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin:4px 0 8px}
.eqlegend,.eqrange{display:flex;gap:6px;flex-wrap:wrap}
.eqlegend button,.eqrange button{font:inherit;font-size:12.5px;padding:4px 10px;border-radius:999px;border:1px solid var(--line);background:var(--soft);color:var(--txt);cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.eqlegend button:not(.on){opacity:.45;text-decoration:line-through}.eqrange button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.eqlegend i{display:inline-block;width:14px;height:3px;border-radius:2px}.eqlegend i.dash{background:repeating-linear-gradient(90deg,var(--mut) 0 4px,transparent 4px 7px)!important}
.eqchart{position:relative;height:300px;width:100%}
.eqtip{position:absolute;z-index:6;pointer-events:none;min-width:190px;max-width:280px;background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:10px;padding:8px 10px;font-size:12.5px;box-shadow:0 6px 24px rgba(0,0,0,.12);direction:rtl;text-align:right}
.eqtip .tt{font-weight:600;margin-bottom:4px}.eqtip .rw{display:flex;justify-content:space-between;gap:12px}.eqtip .rw b{direction:ltr;unicode-bidi:isolate;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.eqtip .tr{border-top:1px solid var(--line);margin-top:6px;padding-top:5px;color:var(--mut)}.eqtip .tr b{direction:ltr;unicode-bidi:isolate}
.eqhint{font-size:12px;margin-top:4px}.eqempty{padding:40px 10px;text-align:center;color:var(--mut)}
@media(max-width:700px){.eqchart{height:240px}}
"""

SCRIPT = """<script>
(function(){
 var el=document.getElementById('eqchart'), dEl=document.getElementById('eqdata');
 if(!el||!dEl||!window.LightweightCharts) return;
 var D=JSON.parse(dEl.textContent), LC=window.LightweightCharts, chart=null, S={}, hidden={}, range='0';
 function v(n){return getComputedStyle(document.documentElement).getPropertyValue(n).trim()||'#888'}
 function money(x){return (x>0?'+':x<0?'−':'')+Math.abs(x).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})+'$'}
 function pad(n){return (n<10?'0':'')+n}
 var AR=['يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر'];
 function h12(t){var d=new Date(t*1000),h=d.getUTCHours();return (h%12||12)+':'+pad(d.getUTCMinutes())+(h<12?' ص':' م')}
 function tick12(t,type){var d=new Date(t*1000);return type>=3?h12(t):type===2?d.getUTCDate()+' '+AR[d.getUTCMonth()]:null}
 function when(t){var d=new Date(t*1000);return d.getUTCDate()+' '+AR[d.getUTCMonth()]+' • '+h12(t)+' الرياض'}
 function draw(){
  if(chart){chart.remove();chart=null;S={}}
  chart=LC.createChart(el,{autoSize:true,
   layout:{background:{type:'solid',color:'transparent'},textColor:v('--mut'),attributionLogo:true,fontSize:11},
   grid:{vertLines:{visible:false},horzLines:{color:v('--l2')}},
   rightPriceScale:{borderVisible:false,scaleMargins:{top:0.12,bottom:0.08}},
   timeScale:{borderVisible:false,timeVisible:true,secondsVisible:false,minBarSpacing:0.5,rightOffset:2,tickMarkFormatter:tick12},
   crosshair:{mode:LC.CrosshairMode.Magnet,vertLine:{color:v('--mut'),width:1,style:3,labelBackgroundColor:v('--ink')},
              horzLine:{color:v('--mut'),width:1,style:3,labelBackgroundColor:v('--ink')}},
   localization:{priceFormatter:function(p){return money(p)},timeFormatter:function(t){return when(t)}},
   handleScale:{axisPressedMouseMove:true,mouseWheel:true,pinch:true},handleScroll:{mouseWheel:false,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:false}});
  D.series.forEach(function(s){
   var c=v(s.c), o={color:c,lineWidth:s.k==='A'?2.5:1.75,lineType:1,lastValueVisible:true,priceLineVisible:false,
     crosshairMarkerRadius:4,crosshairMarkerBorderColor:v('--card'),visible:!hidden[s.k],title:''};
   var ser;
   if(s.t==='area') ser=chart.addSeries(LC.AreaSeries,Object.assign(o,{lineColor:c,topColor:c+'40',bottomColor:c+'05'}));
   else ser=chart.addSeries(LC.LineSeries,Object.assign(o,{lineStyle:s.t==='dash'?2:0}));
   ser.setData(s.d.map(function(p){return {time:p[0],value:p[1]}}));
   if(s.k==='A') ser.createPriceLine({price:0,color:v('--mut'),lineWidth:1,lineStyle:1,axisLabelVisible:false});
   S[s.k]={ser:ser,meta:s};
  });
  applyRange();
  var tip=document.getElementById('eqtip'); el.appendChild(tip);
  chart.subscribeCrosshairMove(function(p){
   if(!p||!p.time||!p.point||p.point.x<0||p.point.y<0){tip.hidden=true;return}
   var h='<div class="tt">'+when(p.time)+'</div>';
   D.series.forEach(function(s){ if(hidden[s.k]) return;
    var d=p.seriesData.get(S[s.k].ser); if(!d) return;
    h+='<div class="rw"><span><i style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'+v(s.c)+';margin-inline-end:6px"></i>'+s.n+'</span><b style="color:'+(d.value>0?v('--g'):d.value<0?v('--r'):v('--txt'))+'">'+money(d.value)+'</b></div>'});
   var tr=D.detail[p.time];
   if(tr){h+='<div class="tr">'+tr.slice(0,5).map(function(t){
     return t.s+' • '+t.k+' • <b style="color:'+(t.p>0?v('--g'):v('--r'))+'">'+money(t.p)+'</b>'+(t.r===null?'':' • <b>'+(t.r>0?'+':'')+t.r.toFixed(2)+'R</b>')}).join('<br>')
     +(tr.length>5?'<br>+'+(tr.length-5)+' أخرى':'')+'</div>'}
   tip.innerHTML=h; tip.hidden=false;
   var w=el.clientWidth, tw=tip.offsetWidth, th=tip.offsetHeight, x=p.point.x+16, y=p.point.y+16;
   if(x+tw>w) x=p.point.x-tw-16; if(x<0) x=4; if(y+th>el.clientHeight) y=Math.max(4,p.point.y-th-16);
   tip.style.left=x+'px'; tip.style.top=y+'px';
  });
 }
 function applyRange(){
  if(!chart) return;
  var all=D.series[0].d, last=all[all.length-1][0];
  if(range==='0'){chart.timeScale().fitContent();return}
  var days=+range, dates=[];
  all.forEach(function(p){var k=Math.floor(p[0]/86400); if(dates.indexOf(k)<0) dates.push(k)});
  var from=dates[Math.max(0,dates.length-days)]*86400;
  chart.timeScale().setVisibleRange({from:from,to:last+60});
 }
 [].forEach.call(document.querySelectorAll('.eqlegend button'),function(b){b.addEventListener('click',function(){
  var k=b.getAttribute('data-k'); hidden[k]=!hidden[k]; b.classList.toggle('on',!hidden[k]);
  if(S[k]) S[k].ser.applyOptions({visible:!hidden[k]})})});
 [].forEach.call(document.querySelectorAll('.eqrange button'),function(b){b.addEventListener('click',function(){
  range=b.getAttribute('data-r'); [].forEach.call(document.querySelectorAll('.eqrange button'),function(x){x.classList.toggle('on',x===b)}); applyRange()})});
 window.huRedrawEquity=draw;
 draw();
 new MutationObserver(draw).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
})();
</script>"""
