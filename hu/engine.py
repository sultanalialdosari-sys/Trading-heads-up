# -*- coding: utf-8 -*-
"""دورة واحدة: مسح ← تقييم ← تنبيه ← صفقات افتراضية ← متابعة. تُشغَّل كل 15 دقيقة من GitHub Actions.

مراحل المسح:
 1) لقطة لكل الأسهم الأمريكية المدرجة (بدون OTC) — السوق الكامل متأخر 15 دقيقة (delayed_sip)، وإن رُفض: IEX.
 2) قائمة قصيرة (أسهم تحت 5$ بحجم دولاري كافٍ ولم تتحرك كثيراً) ← شموع 5m: SIP حتى قبل 16 دقيقة + IEX للحظة.
 3) القواعد المبكرة ← SEC والأخبار للمؤهلين فقط ← الدرجة ← تنبيه.
"""
import json
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

import intraday as I
from hu import charts as CH
from hu import config as C
from hu import halts as HL
from hu import extended as X
from hu import news as N
from hu import rules as RL
from hu import ticks as TK
from hu import timefmt as TF
from hu import trades as TR
from hu.sec import SecClient
from hu.store import Store

ET, UTC = I.ET, I.UTC


def load_cfg(path: str) -> dict:
    """يقرأ config.json (v3 أو القديم المسطح) ويتحقق منه، ويرجع قاموس النظام الحي بنفس الشكل القديم.
    أي إعداد مرفوض يوقف التشغيل برسالة واضحة (ConfigError) بدل أن يعمل بقيم خاطئة."""
    app = C.load(path)
    cfg = app.live_dict()
    cfg["_config_hash"] = C.file_hash(path)
    return cfg


def fmt_vol(v: Optional[float]) -> str:
    if v is None:
        return "غير متوفر"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:.1f}{suf}"
    return f"{v:.0f}"


def et_str(iso: Optional[str]) -> str:
    """الوقت بتوقيت الرياض (الاسم القديم أُبقي حتى لا تنكسر الاستدعاءات)."""
    if not iso:
        return "غير متوفر"
    return TF.h12(datetime.fromisoformat(iso).astimezone(I.RIYADH))


SWEEP_EXTRA: dict = {}


# ====================================================================== المرحلة 1
def sweep(client: I.AlpacaClient, cfg: dict, now: datetime) -> Tuple[pd.DataFrame, str]:
    assets = client._get(I.PAPER_BASE, "/v2/assets", {"status": "active", "asset_class": "us_equity"})
    names, uni = {}, []
    for a in assets:
        sym = a.get("symbol") or ""
        if not a.get("tradable") or a.get("exchange") not in I.SCAN_EXCHANGES:
            continue
        if not sym.replace(".", "").isalpha() or ("." in sym and sym.split(".")[-1] not in ("A", "B", "C")):
            continue
        uni.append(sym); names[sym] = a.get("name") or ""
    feed_note = "السوق الكامل متأخر 15 دقيقة (delayed_sip)"
    try:
        snaps = client.snapshots(uni[:100], feed="delayed_sip")
        feed = "delayed_sip"
    except I.ApiError:
        feed, feed_note = "iex", "IEX فقط (رُفضت بيانات السوق الكامل المتأخرة) — الأحجام جزئية"
        snaps = {}
    snaps.update(client.snapshots(uni[100:] if feed == "delayed_sip" else uni, feed=feed))
    et_now = now.astimezone(ET)
    mins = max(1, min(390, (et_now.hour * 60 + et_now.minute) - 570))
    frac = mins / 390
    rows = []
    for s in uni:
        sn = snaps.get(s) or {}
        db, pb, lt = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}, sn.get("latestTrade") or {}
        px = lt.get("p") or db.get("c")
        if not px or not pb.get("c"):
            continue
        v, pv = float(db.get("v") or 0), float(pb.get("v") or 0)
        vw = float(db.get("vw") or px)
        rows.append({"symbol": s, "name": names.get(s, ""), "price": float(px), "prev_close": float(pb["c"]),
                     "chg": (float(px) / float(pb["c"]) - 1) * 100, "day_high": float(db.get("h") or px),
                     "volume": v, "dollar_vol": v * vw, "prev_vol": pv,
                     "vol_jump": (v / (pv * frac)) if pv > 0 else None,
                     "trade_t": lt.get("t")})
    df = pd.DataFrame(rows)
    # ملاحظة أداء: لا نضع اللقطات في df.attrs — pandas ينسخ attrs نسخاً عميقاً مع كل عملية على الجدول
    # (كان ذلك يجعل الفحص يأخذ 4–5 دقائق بدل ثوانٍ).
    SWEEP_EXTRA.update(universe=len(uni), snaps=snaps, names=names)
    return df, feed_note


def shortlist(df: pd.DataFrame, cfg: dict, n: int = 60) -> pd.DataFrame:
    if df.empty:
        return df
    m = (df.price.between(cfg["min_price"], cfg["max_price"] * 1.05) & (df.dollar_vol >= cfg["min_dollar_volume"] * 0.5)
         & df.chg.between(-5, cfg["max_day_change_pct"] + 5))
    x = df[m].copy()
    x["rank"] = x["vol_jump"].fillna(0)
    return x.sort_values("rank", ascending=False).head(n)


def baseline_pick(df: pd.DataFrame, cfg: dict, taken: set, n: int = 10) -> pd.DataFrame:
    """طريقة المقارنة: أي سهم تحت 5$ قفز حجمه (≥ 2× المعتاد لهذا الوقت)."""
    if df.empty:
        return df
    m = (df.price.between(cfg["min_price"], cfg["max_price"]) & (df.dollar_vol >= cfg["min_dollar_volume"])
         & (df.vol_jump >= 2) & ~df.symbol.isin(taken))
    return df[m].sort_values("vol_jump", ascending=False).head(n)


# ====================================================================== التنبيه
def alert_text(st: RL.Setup, data_note: str) -> str:
    L = [f"🔔 إشارة مبكرة — {st.symbol}" + (f" ({st.name[:40]})" if st.name else ""),
         f"💵 السعر {I.fmt_price(st.price)} — آخر صفقة على IEX {et_str(st.price_time)} الرياض"
         + (f" (قبل {st.price_age_sec:.0f} ث)" if st.price_age_sec is not None else ""),
         f"⏱️ {data_note}",
         f"📊 الحجم اليوم حتى الآن {fmt_vol(st.day_volume)}"
         + (f" = {st.vol_vs_avg:.1f}× متوسط يوم كامل (20 يوماً)" if st.vol_vs_avg else " (متوسط 20 يوماً غير متوفر)")
         + (f" • RVOL {st.rvol:.1f}×" if st.rvol else ""),
         "🟡 الحالة: مبكرة — لم يحدث اختراق بعد",
         "🔎 لماذا ظهر:"] + [f"• {r}" for r in st.reasons[:7]] + [
         f"🧱 أقرب دعم {I.fmt_price(st.support)} • أقرب مقاومة {I.fmt_price(st.resistance)}",
         f"▶️ شرط الدخول: اختراق {I.fmt_price(st.trigger)} بحجم قوي",
         f"⛔ إبطال الفكرة: كسر {I.fmt_price(st.invalidation)}"]
    L.append("⚠️ المخاطر: " + (" | ".join(st.risks[:4]) if st.risks else "لا مخاطر ظاهرة في إفصاحات SEC والأخبار المتاحة"))
    so = f"{fmt_vol(st.shares_out)} (حتى {st.shares_out_date})" if st.shares_out else "غير متوفر"
    L.append(f"🏷️ الأسهم القائمة: {so} • Float: غير متوفر")
    if st.spread_pct is not None:
        L.append(f"↔️ السبريد على IEX: {st.spread_pct:.2f}%")
    if st.missing:
        L.append("❔ غير متوفر: " + "، ".join(dict.fromkeys(st.missing)))
    L.append(f"⭐ الدرجة {st.score}/100 • جودة البيانات: {RL.quality_label(st.data_quality)} ({st.data_quality}/100)")
    L.append("ليست توصية شراء.")
    return "\n".join(L)


# ====================================================================== الدورة
class Engine:
    def __init__(self, client: I.AlpacaClient, store: Store, cfg: dict, sec: SecClient,
                 notifier: Optional[I.TelegramNotifier] = None, log=print):
        self.c, self.db, self.cfg, self.sec, self.tg, self.log = client, store, cfg, sec, notifier, log
        self.errors: List[str] = []
        self.timing: Dict[str, float] = {}
        self._tm = time.time()

    def lap(self, name: str) -> None:
        """توقيت كل مرحلة (لمعرفة أين يذهب الوقت)."""
        t = time.time()
        self.timing[name] = round(self.timing.get(name, 0) + t - self._tm, 1)
        self._tm = t

    def send(self, text: str) -> bool:
        if self.tg is None or not self.tg.ready:
            return False
        ok = self.tg.send(text)
        if not ok:
            self.errors.append("Telegram: " + (self.tg.last_error or "فشل"))
        return ok

    # ---------------------------------------------------------------- scan
    def run(self, kind: str = "scan", scheduled_utc: Optional[datetime] = None, sweep_cache: Optional[dict] = None) -> dict:
        """sweep_cache: {"df", "at", "note"} — لقطة السوق الكامل متأخرة 15 دقيقة أصلاً، فلا داعي لإعادة مسح
        كل الأسهم (≈130 طلباً) كل 5 دقائق؛ نعيد استخدامها حتى 14 دقيقة، والتحليل اللحظي للقائمة القصيرة يتجدد كل مرة."""
        t0 = time.time()
        clock, clock_err = {}, None
        try:
            clock = self.c.clock()          # يضبط فرق الساعة مع Alpaca أولاً
        except I.ApiError as e:
            clock_err = e.message
        now = self.c.now_utc()
        day = now.astimezone(ET).strftime("%Y-%m-%d")
        info = {"kind": kind, "started_utc": now.isoformat(), "scheduled_utc": scheduled_utc.isoformat() if scheduled_utc else None,
                "delay_min": round((now - scheduled_utc).total_seconds() / 60, 1) if scheduled_utc else None,
                "universe": 0, "swept": 0, "shortlist": 0, "qualified": 0, "alerts": 0,
                "config_hash": (self.cfg.get("_config_hash") or "")[:12] or None}
        calls0 = I.API_STATS["total"]
        self.run_id = self.db.insert("runs", {"kind": kind, "started_utc": info["started_utc"]})
        info["market_open"] = int(bool(clock.get("is_open")))
        if clock_err:
            self.errors.append("clock: " + clock_err)
        snap_df = pd.DataFrame()
        if info["market_open"] or kind in ("final", "pre", "post"):
            try:
                fresh = (sweep_cache is not None and sweep_cache.get("df") is not None and sweep_cache.get("kind") == kind
                         and (now - sweep_cache["at"]).total_seconds() < 14 * 60)
                if fresh:
                    snap_df, info["feed_note"] = sweep_cache["df"], sweep_cache["note"] + " (لقطة مُعاد استخدامها)"
                else:
                    snap_df, info["feed_note"] = sweep(self.c, self.cfg, now)
                    if sweep_cache is not None:
                        sweep_cache.update(df=snap_df, at=now, note=info["feed_note"], kind=kind, **SWEEP_EXTRA)
                info["swept"] = len(snap_df)
                info["universe"] = (sweep_cache or {}).get("universe") or SWEEP_EXTRA.get("universe", 0)
            except I.ApiError as e:
                self.errors.append("sweep: " + e.message)
        self.halted: Dict[str, HL.Halt] = {}
        if info["market_open"] or kind in ("pre", "post", "final"):
            self.load_halts(now, day)
        if info["market_open"] and kind == "scan" and not snap_df.empty:
            self.scan(now, day, snap_df, info)
        if kind in ("pre", "post") and not snap_df.empty:
            try:
                self.ext_scan(now, kind.upper(), snap_df, info, clock)
            except I.ApiError as e:
                self.errors.append(f"{kind}: " + e.message)
        # متابعة كل الصفقات والتنبيهات المفتوحة لهذا اليوم
        self.lap("scan")
        try:
            self.follow(now, day, final=(kind == "final"))
        except I.ApiError as e:
            self.errors.append("follow: " + e.message)
        self.lap("follow")
        if kind == "final":
            if not snap_df.empty:
                self.missed(day, snap_df)
            if now - timedelta(minutes=16) >= TR.exit_time(day).astimezone(UTC):
                try:                                   # أي صفقة (اليوم أو قبله) حُسبت بالشموع: نعيد حسابها بالثانية
                    nxt = (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%d")
                    info["refill"] = json.dumps(self.refill(now, nxt), ensure_ascii=False)
                except Exception as e:                 # noqa: BLE001 — الدقة إضافة: لا توقف الإغلاق
                    self.errors.append(f"refill: {type(e).__name__}: {e}"[:200])
            self.daily_summary(day)
            try:
                CH.finalize_day(self.c, self.db, day, now)
                CH.backfill(self.c, self.db, now)
            except Exception as e:                 # الرسوم إضافة: لا توقف الإغلاق
                self.errors.append(f"charts: {type(e).__name__}: {e}"[:200])
            # تنظيف: المرشحون غير المؤهلين لأكثر من 3 أيام لا حاجة لهم (التنبيهات والصفقات تبقى)
            old = (now.astimezone(ET) - timedelta(days=3)).strftime("%Y-%m-%d")
            self.db.db.execute("DELETE FROM candidates WHERE day < ? AND qualifies = 0", (old,))
            self.db.db.execute("DELETE FROM runs WHERE started_utc < ? AND alerts = 0 AND (errors IS NULL OR errors = '')",
                               ((now - timedelta(days=14)).isoformat(),))
        self.lap("rest")
        info["timing"] = json.dumps(self.timing, ensure_ascii=False)
        info["api_calls"] = I.API_STATS["total"] - calls0
        info["sec_calls"] = self.sec.calls
        info["duration_sec"] = round(time.time() - t0, 1)
        info["errors"] = " | ".join(self.errors + self.sec.errors[:5])[:2000]
        self.db.update("runs", self.run_id, **{k: v for k, v in info.items() if k != "kind"})
        self.db.commit()
        return info

    def scan(self, now: datetime, day: str, snap_df: pd.DataFrame, info: dict) -> None:
        # أول مرة يظهر فيها سهم تحت 5$ مرتفعاً 10%+ (لقياس هل الفحص الدوري متأخر)
        mv = snap_df[(snap_df.price <= self.cfg["max_price"]) & (snap_df.chg >= 10)]
        for _, r in mv.iterrows():
            self.db.db.execute("INSERT OR IGNORE INTO movers VALUES (?,?,?,?,?)",
                               (day, r.symbol, now.isoformat(), round(r.chg, 1), r.price))
        sl = shortlist(snap_df, self.cfg)
        info["shortlist"] = len(sl)
        syms = list(sl.symbol)
        if not syms:
            return
        self.lap("sweep")
        cache = I.BarCache(hybrid=True)
        b5, _ = cache.get(self.c, syms)
        self.lap("bars5m")
        try:
            daily = self.c.bars(syms, "1Day", now - timedelta(days=45), now - timedelta(minutes=16), feed="sip")
        except I.ApiError:
            daily = {}
        self.lap("daily")
        live = self.c.snapshots(syms)                       # IEX لحظي
        self.lap("live")
        meta = sl.set_index("symbol")
        setups: List[RL.Setup] = []
        for s in syms:
            df = b5.get(s)
            if df is None or df.empty:
                continue
            d5 = I.regular_session(I.to_et(I.drop_forming_bar(df, 5, now)))
            if len(d5) < 40:
                continue
            d5 = I.add_indicators(d5, self.c.cfg)
            dd = daily.get(s)
            dd = I.to_et(dd) if dd is not None and len(dd) else None
            st = RL.evaluate_setup(s, d5, self.cfg, dd, float(meta.at[s, "prev_close"]))
            st.name = meta.at[s, "name"]
            pc = I.price_from_snapshot(s, live.get(s), True, now, self.c.cfg)
            RL.apply_price(st, pc, self.cfg)
            if pc.price and st.resistance and pc.price >= st.resistance:
                st.gates_failed.append(f"السعر اللحظي {I.fmt_price(pc.price)} تجاوز المقاومة — فات التبكير")
                st.stage = "BROKEN"
            st.data_note = cache.describe(s) or "البيانات: IEX فقط"
            setups.append(st)
        # SEC والأخبار للمؤهلين فقط (اقتصاد في الطلبات)
        pre = [st for st in setups if st.stage == "EARLY" and not st.gates_failed]
        raw_news = self.c.news([st.symbol for st in pre], 24) if pre else []
        for st in pre:
            si = self.sec.info(st.symbol, now)
            RL.apply_sec(st, si)
            RL.apply_news(st, N.summarize(I.news_for_symbol(raw_news, st.symbol), si.recent_8k_days))
        for st in setups:
            h = self.halted.get(st.symbol)
            if h:                                   # موقوف الآن (المصدر الرسمي): لا يمكن الدخول
                st.gates_failed.append(f"موقوف الآن: {h.reason_ar} ({h.reason}) منذ {h.halt_time or '—'} نيويورك — المصدر Nasdaq")
            ratio = cache.vol_ratio.get(st.symbol)
            RL.finalize(st, (1 / ratio) if ratio else None, cache.sip_ok)
        run_id = self.run_id
        # نحفظ أفضل 25 فقط (والمؤهلين) حتى لا تكبر القاعدة مع الفحص كل 5 دقائق
        keep = sorted(setups, key=lambda x: -x.score)[:25]
        keep += [x for x in setups if x.qualifies and x not in keep]
        for st in keep:
            self.db.insert("candidates", {"run_id": run_id, "day": day, "symbol": st.symbol, "score": st.score,
                                          "stage": st.stage, "qualifies": int(st.qualifies and st.score >= self.cfg["min_score"]),
                                          "data": st.to_dict()})
        good = sorted([st for st in setups if st.qualifies and st.score >= self.cfg["min_score"]], key=lambda x: -x.score)
        info["qualified"] = len(good)
        for st in good:
            if self.maybe_alert(st, now, day, run_id, meta):
                info["alerts"] += 1
                self.chart_save(day, st.symbol, b5.get(st.symbol), "scan")
        # طريقة المقارنة
        taken = {r["symbol"] for r in self.db.q("SELECT symbol FROM trades WHERE day=? AND kind='BASE'", (day,))}
        for _, r in baseline_pick(snap_df, self.cfg, taken).iterrows():
            pc = I.price_from_snapshot(r.symbol, live.get(r.symbol), True, now, self.c.cfg) if r.symbol in live else None
            px = pc.price if pc and pc.freshness in ("FRESH", "DELAYED") else None
            if not px:
                continue
            entry = RL.tick_up(pc.ask if pc.ask and pc.spread_pct is not None and pc.spread_pct < 5 else px * (1 + self.cfg["slippage_pct"] / 100))
            stop_pct = self.median_stop_pct(day)
            stop = RL.tick_down(entry * (1 - stop_pct / 100))
            at = max(now, self.c.now_utc())   # لحظة القرار الفعلية
            self.db.insert("trades", {"kind": "BASE", "day": day, "symbol": r.symbol, "created_utc": at.isoformat(),
                                      "ref_price": px, "entry_utc": at.isoformat(), "entry": entry, "stop": stop,
                                      "target": RL.tick_up(entry + self.cfg["target_r"] * (entry - stop)),
                                      "shares": self.cfg["shares_per_trade"], "status": "OPEN",
                                      "note": f"حجم قفز {r.vol_jump:.1f}× — وقف {stop_pct:.1f}%"})

    def median_stop_pct(self, day: str) -> float:
        rows = self.db.q("SELECT entry, stop FROM trades WHERE kind='A' AND entry > stop")
        v = sorted((r["entry"] - r["stop"]) / r["entry"] * 100 for r in rows)
        return round(v[len(v) // 2], 2) if v else 5.0

    def maybe_alert(self, st: RL.Setup, now: datetime, day: str, run_id: int, meta: pd.DataFrame) -> bool:
        prev = self.db.one("SELECT * FROM alerts WHERE day=? AND symbol=? AND kind='EARLY' ORDER BY id DESC LIMIT 1", (day, st.symbol))
        if prev:
            if prev["state"] != "OPEN":
                return False   # تفعّلت أو أُبطلت اليوم: لا تنبيه مبكر جديد لنفس السهم
            old = json.loads(prev["data"])
            new_risk = set(st.risks) - set(old.get("risks", []))
            if st.score - prev["score"] >= 15:
                head = f"🔁 تحديث جوهري — {st.symbol}: الدرجة ارتفعت من {prev['score']} إلى {st.score}"
            elif new_risk:
                head = f"🔁 تحديث — {st.symbol}: خطر جديد: {next(iter(new_risk))[:100]}"
            elif prev["price"] and st.price and abs(st.price / prev["price"] - 1) >= 0.10:
                head = f"🔁 تحديث — {st.symbol}: السعر تحرك {(st.price / prev['price'] - 1) * 100:+.0f}% منذ التنبيه ولم يخترق"
            else:
                return False
            if not self.db.once(f"upd:{day}:{st.symbol}:{st.score // 15}:{len(st.risks)}", day, now.isoformat()):
                return False
            self.send(head + "\n" + f"شرط الدخول {I.fmt_price(st.trigger)} • الإبطال {I.fmt_price(st.invalidation)}\nليست توصية شراء.")
            self.db.update("alerts", prev["id"], score=st.score, trigger=st.trigger, invalidation=st.invalidation, data=json.dumps(st.to_dict(), ensure_ascii=False, default=str))
            return False
        if st.price is None or st.trigger is None or st.invalidation is None or st.invalidation >= st.price:
            return False
        msg = alert_text(st, "بيانات السوق الكامل متأخرة 15 دقيقة، والسعر اللحظي من IEX فقط\n🧮 " + st.data_note)
        sent = self.send(msg)
        now = max(now, self.c.now_utc())      # لحظة الإرسال الفعلية (المسح والتحليل يأخذان وقتاً بعد بداية الدورة)
        aid = self.db.insert("alerts", {"day": day, "symbol": st.symbol, "run_id": run_id, "alert_utc": now.isoformat(),
                                        "kind": "EARLY", "score": st.score, "price": st.price, "trigger": st.trigger,
                                        "invalidation": st.invalidation, "data_quality": st.data_quality,
                                        "gain_from_open": st.day_change_pct, "state": "OPEN", "sent": int(sent),
                                        "message": msg, "data": st.to_dict()})
        sh = int(self.cfg["shares_per_trade"])
        entry = st.ask if (st.ask and st.spread_pct is not None and st.spread_pct <= self.cfg["max_spread_pct"]) \
            else st.price * (1 + self.cfg["slippage_pct"] / 100)
        entry = RL.tick_up(entry)
        if entry > st.invalidation:
            self.db.insert("trades", {"alert_id": aid, "kind": "A", "day": day, "symbol": st.symbol, "created_utc": now.isoformat(),
                                      "ref_price": st.price, "trigger": st.trigger, "entry_utc": now.isoformat(), "entry": entry,
                                      "stop": st.invalidation, "target": RL.tick_up(entry + self.cfg["target_r"] * (entry - st.invalidation)),
                                      "shares": sh, "status": "OPEN", "note": "دخول عند التنبيه"})
        self.db.insert("trades", {"alert_id": aid, "kind": "B", "day": day, "symbol": st.symbol, "created_utc": now.isoformat(),
                                  "ref_price": st.price, "trigger": st.trigger, "stop": st.invalidation, "shares": sh,
                                  "status": "PENDING", "note": "أمر معلّق فوق مستوى التفعيل"})
        return True

    # ---------------------------------------------------------------- follow-up
    def follow(self, now: datetime, day: str, final: bool) -> None:
        cut = now - timedelta(minutes=16)            # السوق الكامل المسموح مجاناً (أقدم من 15 دقيقة)

        def t0(x):
            return datetime.fromisoformat(x)
        trades = [t for t in self.db.q("SELECT * FROM trades WHERE day=? AND status IN ('OPEN','PENDING')", (day,))
                  if t0(t["created_utc"]) < cut]
        alerts = [a for a in self.db.q("SELECT * FROM alerts WHERE day=? AND state='OPEN'", (day,))
                  if max(t0(a["alert_utc"]), t0(a["eval_from"]) if a.get("eval_from") else t0(a["alert_utc"])) < cut]
        syms = sorted({t["symbol"] for t in trades} | {a["symbol"] for a in alerts})
        if not syms:
            return                                    # لا شيء أقدم من 16 دقيقة — لا نطلب بيانات حديثة غير مسموحة
        start = min([t0(t["created_utc"]) for t in trades] +
                    [max(t0(a["alert_utc"]), t0(a["eval_from"]) if a.get("eval_from") else t0(a["alert_utc"])) for a in alerts])
        bars: Dict[str, pd.DataFrame] = {}
        for i in range(0, len(syms), 50):
            bars.update(self.c.bars(syms[i:i + 50], "1Min", start - timedelta(minutes=1), cut, feed="sip"))
        charted = set(CH.chart_symbols(self.db, day))
        for s in syms:
            if s in charted:
                self.chart_save(day, s, bars.get(s), "follow")
        for t in trades:
            r = self.fill(t, bars.get(t["symbol"]), cut)
            upd = {k: v for k, v in r.items() if k in
                   ("status", "entry", "entry_utc", "stop", "target", "note", "exit", "exit_utc", "pnl", "r_mult", "pct",
                    "fill_src", "fees", "fill_detail")}
            b = bars.get(t["symbol"])
            if b is not None and len(b):
                seen = b[b.index < pd.Timestamp(cut)]
                if len(seen):                                   # آخر سعر حتى لحظة القطع (للربح غير المحقق في التقويم)
                    upd["last_px"] = float(seen["close"].iloc[-1])
                    upd["last_utc"] = seen.index[-1].isoformat()
            self.db.update("trades", t["id"], **upd)
        for a in alerts:
            ev = TR.alert_events(a, bars.get(a["symbol"]), cut)
            if not ev:
                continue
            if ev["state"] == "ACTIVATED":
                self.db.update("alerts", a["id"], state="ACTIVATED", activated_utc=ev["at"], lead_min=ev["lead_min"])
                if self.db.once(f"act:{a['id']}", day, now.isoformat()):
                    self.send(f"✅ تفعّلت — {a['symbol']}: اخترق {I.fmt_price(a['trigger'])} الساعة {et_str(ev['at'])} الرياض "
                              f"(بعد {ev['lead_min']:.0f} دقيقة من التنبيه).\nالإبطال ما زال {I.fmt_price(a['invalidation'])}. "
                              f"البيانات من السوق الكامل متأخرة 15 دقيقة. ليست توصية.")
            elif ev["state"] == "INVALIDATED":
                self.db.update("alerts", a["id"], state="INVALIDATED", invalidated_utc=ev["at"])
                if self.db.once(f"inv:{a['id']}", day, now.isoformat()):
                    self.send(f"❌ أُلغيت — {a['symbol']}: كسر مستوى الإبطال {I.fmt_price(a['invalidation'])} "
                              f"الساعة {et_str(ev['at'])} الرياض قبل أن يخترق.")
            elif ev["state"] == "EXPIRED":
                self.db.update("alerts", a["id"], state="EXPIRED")

    def fill(self, t: dict, bars, cut: datetime) -> dict:
        """تنفيذ الصفقة الورقية: بالثانية من صفقات وعروض SIP إن أمكن، وإلا بشموع الدقيقة (تحفظاً)."""
        r = TK.simulate(t, bars, cut, self.c, self.cfg["slippage_pct"], self.cfg["target_r"], self.cfg["max_spread_pct"])
        if r is None:
            r = TR.simulate(t, bars, cut, self.cfg["slippage_pct"], self.cfg["target_r"])
            r["fill_src"] = "bar"
        return self.fill_finish(t, r)

    def fill_finish(self, t: dict, r: dict) -> dict:
        r["fill_detail"] = json.dumps(r.get("fill_detail") or {}, ensure_ascii=False)
        if r.get("exit") is not None and r.get("entry") and r.get("pnl") is not None:
            fees = TK.sell_fees(r["exit"], int(t["shares"]), self.cfg.get("fees") or {})
            r["fees"] = fees
            r["pnl"] = round(r["pnl"] - fees, 2)          # الربح صافٍ بعد رسوم البيع
        return r

    def refill(self, now: datetime, before_day: str) -> dict:
        """إعادة حساب الصفقات الورقية المغلقة للأيام السابقة بدقة الثانية (لتكون السجلات كلها بنفس المعيار).
        تُستبدل النتيجة فقط إن توفرت بيانات الثانية؛ وإلا تبقى نتيجة الشموع كما هي."""
        rows = self.db.q("SELECT * FROM trades WHERE day < ? AND COALESCE(fill_src,'') != 'tick' ORDER BY day, symbol", (before_day,))
        done = kept = 0
        for (day, sym), grp in pd.DataFrame(rows).groupby(["day", "symbol"]) if rows else []:
            d0 = datetime.fromisoformat(day).replace(tzinfo=ET)
            try:
                bars = self.c.bars([sym], "1Min", (d0.replace(hour=4)).astimezone(UTC), (d0.replace(hour=20)).astimezone(UTC), feed="sip").get(sym)
            except Exception as e:                    # noqa: BLE001
                self.errors.append(f"refill {sym} {day}: {e}")
                kept += len(grp)
                continue
            for t in grp.to_dict("records"):
                r = TK.simulate(t, bars, now - timedelta(minutes=16), self.c, self.cfg["slippage_pct"], self.cfg["target_r"],
                                self.cfg["max_spread_pct"])
                if r is None:
                    kept += 1
                    continue
                r = self.fill_finish(t, r)
                self.db.update("trades", t["id"], **{k: v for k, v in r.items() if k in
                               ("status", "entry", "entry_utc", "stop", "target", "note", "exit", "exit_utc", "pnl", "r_mult", "pct",
                                "fill_src", "fees", "fill_detail")})
                done += 1
        return {"refilled": done, "kept_bar": kept}

    def load_halts(self, now: datetime, day: str) -> None:
        """توقفات اليوم الرسمية: تُحفظ، وتمنع تنبيه سهم موقوف، وتُرسل تنبيه توقف لأي سهم نتابعه (مرة لكل توقف)."""
        try:
            halts = HL.fetch(getattr(self.sec, "s", None))
        except Exception as e:                      # التوقفات إضافة: لا توقف الفحص
            self.errors.append(f"halts: {type(e).__name__}: {e}"[:160])
            return
        seen = now.isoformat()
        for h in halts:
            self.db.db.execute(                          # day = يوم الرؤية (اليوم)؛ تاريخ بدء التوقف في halt_date
                "INSERT OR REPLACE INTO halts(day,symbol,name,market,reason,halt_date,halt_time,pause_price,resume_date,"
                "resume_quote,resume_trade,seen_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, h.symbol, h.name, h.market, h.reason, h.halt_date, h.halt_time or "",
                 h.pause_price, h.resume_date, h.resume_quote, h.resume_trade, seen))
        self.halted = HL.active_map(halts, now.astimezone(HL.NY))
        mine = {r["symbol"] for r in self.db.q("SELECT symbol FROM alerts WHERE day=?", (day,))}
        mine |= {r["symbol"] for r in self.db.q("SELECT symbol FROM trades WHERE day=? AND kind!='BASE' AND status IN ('OPEN','PENDING')", (day,))}
        for sym, h in self.halted.items():
            if sym in mine and self.db.once(f"halt:{day}:{sym}:{h.halt_time}", day, seen):
                self.send(f"⏸️ توقف تداول — {sym}: {h.reason_ar} ({h.reason})\n"
                          f"منذ {et_str(h.halted_at().isoformat()) if h.halted_at() else '—'} الرياض"
                          + (f" • سعر الإيقاف {I.fmt_price(h.pause_price)}" if h.pause_price else "")
                          + (f"\nالاستئناف المعلن: {h.resume_trade} نيويورك" if h.resume_trade else "\nلم يُعلن وقت الاستئناف بعد")
                          + f"\nأثناء التوقف لا يمكن البيع ولا الشراء. المصدر: {HL.SOURCE}.")

    def chart_save(self, day: str, symbol: str, df, source: str) -> None:
        try:
            CH.save(self.db, day, symbol, df, source)
        except Exception as e:                     # الرسوم إضافة: لا توقف الفحص
            self.errors.append(f"chart {symbol}: {type(e).__name__}"[:120])

    # ---------------------------------------------------------------- ما قبل الافتتاح / بعد الإغلاق
    def ext_scan(self, now: datetime, phase: str, snap_df: pd.DataFrame, info: dict, clock: dict) -> None:
        snaps, names = SWEEP_EXTRA.get("snaps") or {}, SWEEP_EXTRA.get("names") or {}
        rows = X.ext_rows(snaps, names, phase, now, self.cfg)
        info["shortlist"] = len(rows)
        if rows.empty:
            return
        # الجلسة التي تُقاس فيها النتيجة: افتتاح اليوم (قبل الافتتاح) أو افتتاح يوم التداول التالي (بعد الإغلاق)
        eval_from = I.parse_ts(clock["next_open"]) if clock.get("next_open") else None
        eval_day = eval_from.astimezone(ET).strftime("%Y-%m-%d") if eval_from else now.astimezone(ET).strftime("%Y-%m-%d")
        bars = X.ext_bars(self.c, list(rows.symbol), phase, now)
        setups = [X.evaluate_ext(r, bars.get(r["symbol"]), phase, self.cfg) for r in rows.to_dict("records")]
        pre = [st for st in setups if not st.gates_failed]
        raw_news = self.c.news([st.symbol for st in pre], 18) if pre else []
        for st in pre:
            si = self.sec.info(st.symbol, now)
            if si.ok:
                st.name = si.name or st.name
                st.shares_out, st.shares_out_date = si.shares_out, si.shares_out_date
                st.excluded.extend(si.exclude)
                st.risks.extend(m for _, m in si.penalties)
                if si.penalty:
                    st.parts["خصم"] = st.parts.get("خصم", 0) - si.penalty
            else:
                st.missing.append("إفصاحات SEC")
            X.apply_news(st, N.summarize(I.news_for_symbol(raw_news, st.symbol), si.recent_8k_days))
            X.finalize(st, si.ok)
        for st in setups:
            if st.gates_failed:
                X.finalize(st, False)
            st.data_note = "السوق الكامل متأخر 15 دقيقة + IEX"
            self.db.insert("candidates", {"run_id": self.run_id, "day": eval_day, "symbol": st.symbol, "score": st.score,
                                          "stage": phase, "phase": phase,
                                          "qualifies": int(st.qualifies and st.score >= self.cfg["ext_min_score"]),
                                          "data": st.to_dict()})
        good = sorted([st for st in setups if st.qualifies and st.score >= self.cfg["ext_min_score"]], key=lambda x: -x.score)
        info["qualified"] = len(good)
        for st in good:
            if self.db.one("SELECT id FROM alerts WHERE day=? AND symbol=? AND kind=?", (eval_day, st.symbol, phase)):
                continue          # تنبيه واحد لكل سهم لكل فترة
            if not st.trigger or not st.invalidation or st.invalidation >= st.trigger:
                continue
            msg = X.alert_text(st, eval_from)
            sent = self.send(msg)
            aid = self.db.insert("alerts", {
                "day": eval_day, "symbol": st.symbol, "run_id": self.run_id, "alert_utc": now.isoformat(), "kind": phase,
                "phase": phase, "score": st.score, "price": st.price, "trigger": st.trigger, "invalidation": st.invalidation,
                "data_quality": st.data_quality, "gain_from_open": st.change_pct, "state": "OPEN", "sent": int(sent),
                "message": msg, "data": st.to_dict(), "eval_from": eval_from.isoformat() if eval_from else None})
            info["alerts"] += 1
            # صفقة افتراضية: أمر معلّق عند قمة الفترة يبدأ من افتتاح الجلسة
            self.db.insert("trades", {"alert_id": aid, "kind": phase, "day": eval_day, "symbol": st.symbol,
                                      "created_utc": (eval_from or now).isoformat(), "ref_price": st.price,
                                      "trigger": st.trigger, "stop": st.invalidation, "shares": int(self.cfg["shares_per_trade"]),
                                      "status": "PENDING", "note": f"قائمة {X.PHASE_AR[phase]}: يُنفَّذ إذا اخترق القمة في الجلسة"})

    # ---------------------------------------------------------------- end of day
    def missed(self, day: str, snap_df: pd.DataFrame) -> None:
        """الأسهم تحت 5$ التي ارتفعت 30%+ اليوم بلا تنبيه — حتى لا نتجاهل ما فات."""
        if snap_df.empty:
            return
        m = snap_df[(snap_df.prev_close <= self.cfg["max_price"]) & (snap_df.prev_close >= self.cfg["min_price"])
                    & (snap_df.dollar_vol >= self.cfg["min_dollar_volume"])]
        m = m.assign(max_gain=(m.day_high / m.prev_close - 1) * 100)
        m = m[m.max_gain >= 30]
        alerted = {r["symbol"] for r in self.db.q("SELECT symbol FROM alerts WHERE day=?", (day,))}
        for _, r in m.iterrows():
            if r.symbol in alerted:
                continue
            c = self.db.q("SELECT score, data FROM candidates WHERE day=? AND symbol=? ORDER BY score DESC LIMIT 1", (day, r.symbol))
            if c:
                d = json.loads(c[0]["data"])
                reason = "؛ ".join((d.get("excluded") or []) + (d.get("gates_failed") or []))[:300] or f"الدرجة {c[0]['score']} أقل من الحد"
            else:
                f = self.db.one("SELECT * FROM movers WHERE day=? AND symbol=?", (day, r.symbol))
                if f and f["first_chg"] > self.cfg["max_day_change_pct"]:
                    reason = (f"تحرك بين فحصين: أول ظهور الساعة {et_str(f['first_utc'])} الرياض وقد ارتفع "
                              f"{f['first_chg']:.0f}% بالفعل — الفحص الدوري كان متأخراً")
                elif f:
                    reason = (f"ظهر الساعة {et_str(f['first_utc'])} مرتفعاً {f['first_chg']:.0f}% لكنه لم يدخل القائمة القصيرة "
                              "(حجم دولاري أو قفزة حجم غير كافية)")
                else:
                    reason = "لم يظهر في أي فحص قبل الحركة — قفز بين فحصين أو بعد آخر فحص"
            self.db.db.execute("INSERT OR REPLACE INTO missed VALUES (?,?,?,?,?,?,?,?,?)",
                               (day, r.symbol, r.prev_close, r.day_high, round(r.max_gain, 1), round(r.chg, 1),
                                int(bool(c)), c[0]["score"] if c else None, reason))

    def daily_summary(self, day: str) -> None:
        if not self.db.once(f"daily:{day}", day, datetime.now(UTC).isoformat()):
            return
        al_all = self.db.q("SELECT * FROM alerts WHERE day=?", (day,))
        al = [a for a in al_all if a["kind"] == "EARLY"]
        ext = [a for a in al_all if a["kind"] in ("PRE", "POST")]
        tr = self.db.q("SELECT * FROM trades WHERE day=?", (day,))
        sA, sB, sBase, sPre, sPost = (TR.summary([t for t in tr if t["kind"] == k]) for k in ("A", "B", "BASE", "PRE", "POST"))
        ms = self.db.q("SELECT COUNT(*) AS n FROM missed WHERE day=?", (day,))[0]["n"]
        act = [a for a in al if a["state"] == "ACTIVATED"]
        inv = [a for a in al if a["state"] == "INVALIDATED"]

        def line(name, s):
            if not s.get("trades"):
                return f"{name}: لا صفقات مغلقة"
            return (f"{name}: {s['trades']} صفقة • ربح {s['win_rate']:.0%} • صافي ${s['total_pnl']:+,.0f} • "
                    f"متوسط {s['avg_pct']:+.1f}%")
        txt = [f"📋 ملخص {day}",
               f"تنبيهات مبكرة: {len(al)} • تفعّلت {len(act)} • أُلغيت {len(inv)} • بلا حسم {len(al) - len(act) - len(inv)}",
               line("A دخول عند التنبيه", sA), line("B دخول عند الاختراق", sB), line("المقارنة (أي قفزة حجم)", sBase),
               f"قوائم ما قبل الافتتاح/بعد الإغلاق لهذه الجلسة: {len(ext)} • تفعّلت {sum(1 for a in ext if a['state'] == 'ACTIVATED')}",
               line("قائمة ما قبل الافتتاح (عند اختراق القمة)", sPre), line("قائمة بعد إغلاق أمس (عند اختراق القمة)", sPost),
               f"فاتت (ارتفعت 30%+ بلا تنبيه): {ms}",
               "صفقات افتراضية فقط — لا أوامر حقيقية."]
        if act:
            leads = sorted(a["lead_min"] for a in act if a["lead_min"] is not None)
            if leads:
                txt.insert(2, f"متوسط السبق قبل الاختراق: {sum(leads) / len(leads):.0f} دقيقة")
        self.send("\n".join(txt))
