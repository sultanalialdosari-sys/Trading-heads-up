# -*- coding: utf-8 -*-
"""سوق وهمي لاختبار النظام بدون إنترنت: Alpaca + SEC + Telegram.
السيناريو (يوم 2026-11-20، توقيت نيويورك):
  EARL: صعد صباحاً إلى 2.00 ثم تماسك بقيعان أعلى وحجم متصاعد تحت 2.00 — إشارة مبكرة متوقعة،
        ثم اخترق 2.00 الساعة 12:40 وصعد إلى 2.30.
  DILU: نفس الشكل لكن عليه نشرة طرح 424B5 قبل 3 أيام — يجب أن يُستبعد.
  RUNR: ارتفع 40% وعند قمم جديدة — فات التبكير (يظهر في «فاتت»).
  JUMP: حجم قفز لكن السعر يهبط تحت VWAP — لا تنبيه، لكنه يدخل طريقة المقارنة.
  FLAT: سهم هادئ.
"""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import numpy as np
import pandas as pd

UTC = timezone.utc
ET_OFF = timedelta(hours=-5)   # نوفمبر = EST
DAYS = ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13",
        "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
TODAY = DAYS[-1]


def et(day, hh, mm):
    return (datetime.fromisoformat(day).replace(hour=hh, minute=mm) - ET_OFF).replace(tzinfo=UTC)


def _session(day, path_fn, vol_fn, rng, wick, start=(9, 30), n=390):
    rows = []
    t0 = et(day, *start)
    prev = None
    for k in range(n):
        t = t0 + timedelta(minutes=k)
        p = path_fn(k)
        o = prev if prev is not None else p
        c = p
        h = max(o, c) + abs(rng.normal(0, wick))
        l = min(o, c) - abs(rng.normal(0, wick))
        rows.append((t, o, h, l, c, max(1.0, vol_fn(k) * rng.uniform(0.7, 1.3))))
        prev = c
    return rows


def build():
    rng = np.random.default_rng(7)
    data = {}

    def normal_days(base, vpm, drift=0.0):
        rows = []
        for j, day in enumerate(DAYS[:-1]):
            b = base * (1 + drift * j)
            rows += _session(day, lambda k, b=b: b * (1 + 0.006 * math.sin(k / 37) + rng.normal(0, 0.0008)),
                             lambda k: vpm, rng, b * 0.0015)
        return rows

    def early_today(prev_close, top, k_break=None, run_to=None):
        def path(k):
            if k <= 60:                        # 9:30–10:30 صعود إلى القمة
                return prev_close + (top - prev_close) * (k / 60)
            if k <= 105:                       # تراجع إلى قاع أول
                return top - (top - (top * 0.94)) * ((k - 60) / 45)
            if k_break is None or k < k_break:   # تماسك بقيعان أعلى تحت القمة
                low = top * 0.94 + (k - 105) * top * 0.00022
                low = min(low, top * 0.968)
                return low + top * 0.006 * (1 + math.sin(k / 2.5)) / 2
            return min(run_to, top * 1.002 + (k - k_break) * (run_to - top) / 50)
        def vol(k):
            if k <= 60:
                return 2400
            if k <= 105:
                return 1200
            return 1300 + (k - 105) * 25
        return _session(TODAY, path, vol, rng, top * 0.0012)

    data["EARL"] = normal_days(1.68, 600) + early_today(1.70, 2.00, k_break=190, run_to=2.30)   # 12:40
    data["DILU"] = normal_days(2.95, 400) + early_today(3.00, 3.50)
    runr_prev = normal_days(1.00, 900)
    data["RUNR"] = runr_prev + _session(TODAY, lambda k: 1.0 + 0.40 * min(1, k / 150), lambda k: 5000, rng, 0.002)
    data["JUMP"] = normal_days(3.0, 500) + _session(TODAY, lambda k: 3.0 - 0.25 * k / 390, lambda k: 2500, rng, 0.004)
    data["FLAT"] = normal_days(4.0, 300) + _session(TODAY, lambda k: 4.0 + rng.normal(0, 0.002), lambda k: 300, rng, 0.003)
    # PREM: يقفز قبل الافتتاح (7:00–9:29) بحجم كبير ثم يخترق قمة ما قبل الافتتاح الساعة 9:40
    pm = _session(TODAY, lambda k: 2.0 + 0.60 * min(1, k / 60) - (0.06 * (1 + math.sin(k / 4)) / 2 if k > 60 else 0),
                  lambda k: 3000, rng, 0.004, start=(7, 0), n=150)
    reg = _session(TODAY, lambda k: 2.58 + (0 if k < 10 else min(0.62, (k - 10) * 0.02)) - (0 if k < 70 else (k - 70) * 0.002),
                   lambda k: 6000, rng, 0.004)
    data["PREM"] = normal_days(2.0, 800) + pm + reg
    # AFTR: هادئ في الجلسة ثم يتحرك +20% بعد الإغلاق (16:00–17:30)
    ah = _session(TODAY, lambda k: 3.0 + 0.6 * min(1, k / 40), lambda k: 2500, rng, 0.004, start=(16, 0), n=90)
    data["AFTR"] = normal_days(3.0, 700) + _session(TODAY, lambda k: 3.0 + rng.normal(0, 0.003), lambda k: 700, rng, 0.003) + ah
    out = {}
    for s, rows in data.items():
        df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume"]).set_index("t")
        out[s] = df
    return out


class Resp:
    def __init__(self, code, js, text=None):
        self.status_code, self._js, self.headers = code, js, {}
        self.text = text if text is not None else json.dumps(js)[:200]

    def json(self):
        return self._js


def _bars_json(df):
    return [{"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": round(r.open, 4), "h": round(r.high, 4), "l": round(r.low, 4),
             "c": round(r.close, 4), "v": int(r.volume), "n": 10, "vw": round((r.high + r.low + r.close) / 3, 4)}
            for t, r in df.iterrows()]


class FakeWorld:
    def __init__(self, now_et=(12, 17), sip_allowed=True):
        self.m1 = build()
        self.now = et(TODAY, *now_et)
        self.sip_allowed = sip_allowed
        self.headers = {}
        self.calls = []
        self.tg = []
        self.halts = []          # [(symbol, reason, halt_et "HH:MM:SS", resume_et أو "")] — خلاصة Nasdaq المزيفة
        self.cik = {"EARL": 1001, "DILU": 1002, "RUNR": 1003, "JUMP": 1004, "FLAT": 1005, "PREM": 1006, "AFTR": 1007}

    # ---- Alpaca
    def _frame(self, sym, feed, tf, start, end):
        df = self.m1.get(sym)
        if df is None:
            return pd.DataFrame()
        df = df[(df.index >= start) & (df.index < min(end, self.now))]
        if feed == "iex":
            df = df.assign(volume=(df.volume * 0.05).round())
        if tf == "1Min":
            return df
        rule = {"5Min": "5min", "1Day": "1D"}[tf]
        if tf == "1Day":
            g = df.groupby(df.index.date)
            d = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(), "volume": g.volume.sum()})
            d.index = [pd.Timestamp(x).tz_localize(UTC) + timedelta(hours=5) for x in d.index]
            return d
        return df.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()

    def get(self, url, params=None, timeout=None, headers=None, **kw):
        params = dict(params or {})
        self.calls.append((url, params.get("feed")))
        if "api.telegram.org" in url:
            self.tg.append(params.get("text", ""))
            return Resp(200, {"ok": True, "result": {}})
        if "sec.gov" in url:
            return self.sec(url)
        if "nasdaqtrader.com" in url:
            d = datetime.fromisoformat(TODAY).strftime("%m/%d/%Y")
            items = "".join(
                f"<item><title>{s}</title><ndaq:HaltDate>{d}</ndaq:HaltDate><ndaq:HaltTime>{ht}</ndaq:HaltTime>"
                f"<ndaq:IssueSymbol>{s}</ndaq:IssueSymbol><ndaq:IssueName>{s} Corp</ndaq:IssueName><ndaq:Market>NASDAQ</ndaq:Market>"
                f"<ndaq:ReasonCode>{rc}</ndaq:ReasonCode><ndaq:PauseThresholdPrice>2.10</ndaq:PauseThresholdPrice>"
                f"<ndaq:ResumptionDate>{d if rt else ''}</ndaq:ResumptionDate><ndaq:ResumptionQuoteTime></ndaq:ResumptionQuoteTime>"
                f"<ndaq:ResumptionTradeTime>{rt}</ndaq:ResumptionTradeTime></item>" for s, rc, ht, rt in self.halts)
            xml = ('<?xml version="1.0"?><rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/"><channel>'
                   f"<title>NASDAQTrader.com</title>{items}</channel></rss>")
            return Resp(200, None, xml)
        if url.endswith("/v2/clock"):
            open_ = et(TODAY, 9, 30) <= self.now < et(TODAY, 16, 0)
            nxt = et(TODAY, 9, 30) if self.now < et(TODAY, 9, 30) else et("2026-11-23", 9, 30)
            return Resp(200, {"timestamp": self.now.isoformat().replace("+00:00", "Z"), "is_open": open_,
                              "next_open": nxt.isoformat().replace("+00:00", "Z")})
        if url.endswith("/v2/calendar"):
            d = params.get("start")
            return Resp(200, [{"date": d, "open": "09:30", "close": "16:00"}] if d in DAYS + ["2026-11-23"] else [])
        if url.endswith("/v2/assets"):
            return Resp(200, [{"symbol": s, "name": f"{s} Corp", "exchange": "NASDAQ", "tradable": True} for s in self.m1] +
                        [{"symbol": "OTCX", "name": "otc", "exchange": "OTC", "tradable": True}])
        if url.endswith("/v2/stocks/snapshots"):
            feed = params.get("feed")
            if feed == "delayed_sip" and not self.sip_allowed:
                return Resp(403, {"message": "subscription does not permit"})
            ref = self.now - timedelta(minutes=15) if feed == "delayed_sip" else self.now
            out = {}
            for s in params["symbols"].split(","):
                df = self.m1.get(s)
                if df is None:
                    continue
                av = df[df.index < ref]
                tdy = av[(av.index >= et(TODAY, 9, 30)) & (av.index < et(TODAY, 16, 0))]
                prv = df[(df.index >= et(DAYS[-2], 9, 30)) & (df.index < et(DAYS[-2], 16, 0))]
                dbt = et(TODAY, 5, 0)
                if tdy.empty:      # قبل الافتتاح: آخر شمعة يومية = جلسة أمس
                    tdy, dbt = prv, et(DAYS[-2], 5, 0)
                    prv = df[(df.index >= et(DAYS[-3], 9, 30)) & (df.index < et(DAYS[-3], 16, 0))]
                vmul = 0.05 if feed != "delayed_sip" else 1
                last = av.iloc[-1]
                p = float(last.close)
                out[s] = {"latestTrade": {"t": (ref - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "p": p},
                          "latestQuote": {"t": (ref - timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "bp": round(p - 0.003, 4), "ap": round(p + 0.003, 4)},
                          "dailyBar": {"t": dbt.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": float(tdy.open.iloc[0]), "h": float(tdy.high.max()),
                                       "l": float(tdy.low.min()), "c": float(tdy.close.iloc[-1]) if dbt.date() != et(TODAY, 5, 0).date() or ref >= et(TODAY, 16, 0) else p,
                                       "v": float(tdy.volume.sum() * vmul), "vw": float((tdy.close * tdy.volume).sum() / tdy.volume.sum())},
                          "prevDailyBar": {"c": float(prv.close.iloc[-1]), "v": float(prv.volume.sum() * vmul)}}
            return Resp(200, out)
        if url.endswith("/v2/stocks/bars"):
            feed = params.get("feed") or "iex"
            start = pd.Timestamp(params["start"]).tz_convert(UTC)
            end = pd.Timestamp(params["end"]).tz_convert(UTC) if params.get("end") else pd.Timestamp(self.now)
            if feed == "sip" and (not self.sip_allowed or end > pd.Timestamp(self.now - timedelta(minutes=15))):
                return Resp(403, {"message": "subscription does not permit querying recent SIP data"})
            out = {}
            for s in params["symbols"].split(","):
                f = self._frame(s, feed, params["timeframe"], start, end)
                if len(f):
                    out[s] = _bars_json(f)
            return Resp(200, {"bars": out, "next_page_token": None})
        m_t = re.search(r"/v2/stocks/([A-Z]+)/(trades|quotes)$", url)
        if m_t:                                   # صفقات/عروض SIP مصطنعة من شموع الدقيقة: o ثم h ثم l ثم c
            sym, kind = m_t.groups()
            start = pd.Timestamp(params["start"]).tz_convert(UTC)
            end = pd.Timestamp(params["end"]).tz_convert(UTC)
            if not self.sip_allowed or end > pd.Timestamp(self.now - timedelta(minutes=15)):
                return Resp(403, {"message": "subscription does not permit querying recent SIP data"})
            f = self._frame(sym, "sip", "1Min", start - timedelta(minutes=1), end)
            rows = []
            for t0, r in f.iterrows():
                for sec, p in ((1, r.open), (20, r.high), (40, r.low), (59, r.close)):
                    ts = t0 + timedelta(seconds=sec)
                    if not (start <= ts < end):
                        continue
                    z = ts.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    if kind == "trades":
                        rows.append({"t": z, "p": round(p, 4), "s": 200, "c": ["@"]})
                    else:
                        tq = (ts + timedelta(milliseconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                        rows.append({"t": tq, "bp": round(p - 0.01, 4), "ap": round(p + 0.01, 4), "bs": 500, "as": 500})
            return Resp(200, {kind: rows, "next_page_token": None})
        if "/v1beta1/news" in url:
            syms = params.get("symbols", "").split(",")
            n = []
            if "EARL" in syms:
                n.append({"id": 1, "headline": "EARL awarded $12 million contract by US Navy", "summary": "",
                          "created_at": (self.now - timedelta(hours=3)).isoformat().replace("+00:00", "Z"), "symbols": ["EARL"], "source": "benzinga"})
            if "DILU" in syms:
                n.append({"id": 2, "headline": "DILU explores revolutionary AI-powered vision", "summary": "",
                          "created_at": (self.now - timedelta(hours=2)).isoformat().replace("+00:00", "Z"), "symbols": ["DILU"], "source": "benzinga"})
            return Resp(200, {"news": n, "next_page_token": None})
        return Resp(404, {"message": "not found " + url})

    # ---- SEC
    def sec(self, url):
        if url.endswith("company_tickers.json"):
            return Resp(200, {str(i): {"cik_str": c, "ticker": s, "title": f"{s} Holdings Inc"} for i, (s, c) in enumerate(self.cik.items())})
        m = re.search(r"CIK(\d{10})", url)
        cik = int(m.group(1))
        sym = next(s for s, c in self.cik.items() if c == cik)
        if "submissions" in url:
            rec = {"form": [], "filingDate": [], "acceptanceDateTime": [], "items": []}
            def add(form, days_ago, items=""):
                t = self.now - timedelta(days=days_ago)
                rec["form"].append(form); rec["filingDate"].append(t.strftime("%Y-%m-%d"))
                rec["acceptanceDateTime"].append(t.strftime("%Y-%m-%dT%H:%M:%S.000Z")); rec["items"].append(items)
            if sym == "DILU":
                add("424B5", 3)
            if sym == "EARL":
                add("8-K", 0.2, "1.01,9.01")
                add("10-Q", 20)
                add("424B5", -1)    # مستقبلي: يجب تجاهله (لا نظر للمستقبل)
            return Resp(200, {"cik": cik, "name": f"{sym} Holdings Inc", "filings": {"recent": rec}})
        if "EntityCommonStockSharesOutstanding" in url:
            return Resp(200, {"units": {"shares": [{"end": "2026-08-10", "val": 41_500_000, "filed": "2026-08-12", "form": "10-Q"},
                                                   {"end": "2026-11-30", "val": 99_000_000, "filed": "2026-12-01", "form": "10-Q"}]}})
        return Resp(404, {})
