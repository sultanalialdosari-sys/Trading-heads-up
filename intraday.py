# -*- coding: utf-8 -*-
"""
intraday.py — نظام تحليل تداول يومي (Intraday) للأسهم الأمريكية عبر Alpaca (خطة Basic المجانية).

مبادئ ثابتة في هذا الملف:
- لا يوجد أي استدعاء لـ endpoints الأوامر (/v2/orders). النظام تحليلي وافتراضي فقط.
- لا تُختلق أي أسعار: إذا فشل الجلب تُعاد None وتظهر الحالة "DATA UNAVAILABLE".
- كل سعر يُعرض مع timestamp المصدر وعمره بالثواني وحالة حداثته.
- بيانات IEX تمثل بورصة واحدة فقط وليست السوق كاملاً (SIP).
- المفاتيح تُقرأ من Colab Secrets أو متغيرات البيئة فقط، ولا تُطبع أبداً.
"""
from __future__ import annotations

import csv
import json
import math
import os
import threading
import time
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

ET = ZoneInfo("America/New_York")
RIYADH = ZoneInfo("Asia/Riyadh")
UTC = timezone.utc

PAPER_BASE = "https://paper-api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
STREAM_IEX = "wss://stream.data.alpaca.markets/v2/iex"

# ======================================================================
# الإعدادات
# ======================================================================


@dataclass
class Config:
    feed: str = "iex"                 # الخطة المجانية: iex لحظي، sip متأخر 15 دقيقة
    fresh_sec: int = 60               # عمر آخر صفقة حتى يُعتبر السعر FRESH
    delayed_sec: int = 300            # بين fresh و delayed = DELAYED، بعدها STALE
    max_calls_per_min: int = 180      # أقل من حد 200/min للهامش
    rvol_lookback_days: int = 10      # عدد الجلسات السابقة لحساب RVOL
    history_calendar_days: int = 20   # أيام تقويمية لجلب بيانات 5m التاريخية
    risk_per_trade_usd: float = 50.0  # المخاطرة الافتراضية لكل صفقة
    max_position_usd: float = 5000.0  # الحد الأعلى لقيمة المركز الافتراضي
    liquidity_cap_pct: float = 0.05   # لا يتجاوز عدد الأسهم 5% من متوسط حجم شمعة 5m
    min_rr: float = 2.0               # أقل نسبة عائد/مخاطرة مقبولة
    max_stop_atr: float = 1.5         # أقصى مسافة وقف بوحدات ATR(5m)
    chase_vwap_atr: float = 2.0       # بعد السعر عن VWAP بأكثر من هذا = مطاردة
    chase_ema9_atr: float = 1.5       # بعد السعر عن EMA9 بأكثر من هذا = مطاردة
    no_new_entries_after: str = "15:30"  # ET
    slippage_pct: float = 0.001       # انزلاق افتراضي لكل جهة في الاختبار التاريخي
    news_lookback_hours: int = 24
    market_filter: bool = True        # لا تأكيد دخول عندما يكون السوق العام (SPY و QQQ) هابطاً
    # شروط الدخول (تُستخدم في تحليل أهمية كل شرط؛ القيم الافتراضية = الاستراتيجية الأصلية)
    req_vwap: bool = True
    req_ema: bool = True
    req_rvol: bool = True
    req_no_chase: bool = True
    min_entry_score: int = 75
    # شروط تجريبية (متوقفة افتراضياً — فعّلها بعد أن يثبت الاختبار فائدتها)
    max_spread_stop_pct: float = 0.0  # الفارق بين الشراء والبيع ≤ هذه النسبة من مسافة الوقف (0 = متوقف)
    skip_open_min: int = 0            # لا دخول قبل مرور هذه الدقائق من الافتتاح (0 = متوقف)
    req_rel_spy: bool = False         # السهم أقوى من SPY منذ افتتاح اليوم
    req_news_catalyst: bool = False   # يلزم خبر إيجابي (بلا علامات مخاطرة) خلال المدة
    target_r: float = 2.0             # الهدف الأول بوحدات R (الهدف الثاني = +1R)
    # حجم المركز كنسبة من الحساب (risk_mode="pct")
    risk_mode: str = "usd"            # "usd" مبلغ ثابت | "pct" نسبة من الحساب
    account_size_usd: float = 0.0
    risk_pct: float = 0.5             # % من الحساب لكل صفقة


# ======================================================================
# المفاتيح (بدون طباعة)
# ======================================================================


def load_keys() -> Tuple[str, str]:
    """يقرأ المفاتيح من Colab Secrets أولاً ثم من متغيرات البيئة."""
    key = secret = None
    try:
        from google.colab import userdata  # type: ignore
        try:
            key = userdata.get("ALPACA_API_KEY")
            secret = userdata.get("ALPACA_API_SECRET")
        except Exception as e:  # SecretNotFoundError / NotebookAccessError
            raise RuntimeError(
                "تعذر قراءة Colab Secrets. تأكد من إضافة ALPACA_API_KEY و ALPACA_API_SECRET "
                "من أيقونة المفتاح 🔑 وتفعيل Notebook access لكليهما. (" + type(e).__name__ + ")"
            ) from None
    except ImportError:
        key = os.environ.get("ALPACA_API_KEY")
        secret = os.environ.get("ALPACA_API_SECRET")
    if not key or not secret:
        raise RuntimeError("المفاتيح غير موجودة. أضف ALPACA_API_KEY و ALPACA_API_SECRET إلى Secrets.")
    return key.strip(), secret.strip()


def mask(s: str) -> str:
    return (s[:4] + "…" + s[-2:]) if s and len(s) > 8 else "****"


# ======================================================================
# عميل HTTP مع إعادة المحاولة وحدود الطلبات
# ======================================================================


class ApiError(Exception):
    def __init__(self, status: Optional[int], message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class RateLimiter:
    def __init__(self, per_min: int):
        self.per_min = per_min
        self.calls: List[float] = []
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            self.calls = [t for t in self.calls if now - t < 60]
            if len(self.calls) >= self.per_min:
                sleep_for = 60 - (now - self.calls[0]) + 0.05
                time.sleep(max(sleep_for, 0))
            self.calls.append(time.monotonic())


import collections as _collections

API_STATS = {"calls": _collections.deque(maxlen=2000), "errors": _collections.deque(maxlen=40), "total": 0}


def api_note_error(path: str, msg: str) -> None:
    API_STATS["errors"].append((time.time(), path, str(msg)[:200]))


def api_calls_last(sec: float = 60.0) -> int:
    t = time.time() - sec
    return sum(1 for x in list(API_STATS["calls"]) if x >= t)


class AlpacaClient:
    """عميل قراءة فقط. لا يحتوي أي دالة لإرسال أوامر."""

    def __init__(self, key: str, secret: str, cfg: Optional[Config] = None,
                 session: Optional[requests.Session] = None):
        self.cfg = cfg or Config()
        self.s = session or requests.Session()
        self.s.headers.update({
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "Accept": "application/json",
        })
        self.rl = RateLimiter(self.cfg.max_calls_per_min)
        self.clock_skew_sec: float = 0.0  # server_time - local_time
        self.call_count = 0

    # ---------------- low level ----------------
    def _get(self, base: str, path: str, params: Optional[dict] = None,
             retries: int = 4, timeout: float = 15) -> Any:
        if "/orders" in path:
            raise ApiError(None, "ممنوع: النظام لا يرسل أوامر تداول.")
        url = base + path
        delay = 1.0
        last_err: Optional[str] = None
        for attempt in range(retries):
            self.rl.wait()
            try:
                self.call_count += 1
                API_STATS["calls"].append(time.time()); API_STATS["total"] += 1
                r = self.s.get(url, params=params, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = f"انقطاع اتصال/مهلة: {type(e).__name__}"
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise ApiError(200, "رد غير صالح (ليس JSON)")
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", delay))
                last_err = "تجاوز حد الطلبات 429"
                time.sleep(max(wait, 1))
                delay = min(delay * 2, 30)
                continue
            if r.status_code in (500, 502, 503, 504):
                last_err = f"خطأ خادم {r.status_code}"
                time.sleep(delay)
                delay = min(delay * 2, 20)
                continue
            msg = _err_text(r)
            api_note_error(path, f"HTTP {r.status_code}: {msg}")
            if r.status_code in (401, 403):
                if "subscription" in msg.lower() or "sip" in msg.lower():
                    raise ApiError(r.status_code, "الخطة المجانية لا تسمح بهذا الطلب: " + msg)
                raise ApiError(r.status_code, "رفض المصادقة — تأكد من مفاتيح Paper الصحيحة: " + msg)
            raise ApiError(r.status_code, f"HTTP {r.status_code}: {msg}")
        api_note_error(path, f"فشل بعد {retries} محاولات: {last_err}")
        raise ApiError(None, f"فشل بعد {retries} محاولات: {last_err}")

    def _paged(self, base: str, path: str, params: dict, key: str) -> list:
        out: list = []
        params = dict(params)
        for _ in range(50):
            data = self._get(base, path, params)
            chunk = data.get(key) or []
            if isinstance(chunk, dict):  # multi-symbol bars
                out.append(chunk)
            else:
                out.extend(chunk)
            tok = data.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
        return out

    # ---------------- trading API (read-only) ----------------
    def account(self) -> dict:
        return self._get(PAPER_BASE, "/v2/account")

    def clock(self) -> dict:
        t_local = datetime.now(UTC)
        c = self._get(PAPER_BASE, "/v2/clock")
        try:
            t_srv = parse_ts(c["timestamp"])
            self.clock_skew_sec = (t_srv - t_local).total_seconds()
        except Exception:
            pass
        return c

    def calendar(self, start: str, end: str) -> list:
        return self._get(PAPER_BASE, "/v2/calendar", {"start": start, "end": end})

    def asset(self, symbol: str) -> dict:
        return self._get(PAPER_BASE, f"/v2/assets/{symbol}")

    def now_utc(self) -> datetime:
        """الوقت الحالي مصححاً بفرق الساعة مع خادم Alpaca."""
        return datetime.now(UTC) + timedelta(seconds=self.clock_skew_sec)

    # ---------------- market data ----------------
    def latest_trade(self, symbol: str, feed: Optional[str] = None) -> dict:
        return self._get(DATA_BASE, f"/v2/stocks/{symbol}/trades/latest",
                         {"feed": feed or self.cfg.feed}).get("trade") or {}

    def latest_quote(self, symbol: str, feed: Optional[str] = None) -> dict:
        return self._get(DATA_BASE, f"/v2/stocks/{symbol}/quotes/latest",
                         {"feed": feed or self.cfg.feed}).get("quote") or {}

    def snapshots(self, symbols: List[str], feed: Optional[str] = None) -> dict:
        out: dict = {}
        for i in range(0, len(symbols), 100):
            part = symbols[i:i + 100]
            d = self._get(DATA_BASE, "/v2/stocks/snapshots",
                          {"symbols": ",".join(part), "feed": feed or self.cfg.feed})
            out.update(d or {})
        return out

    def bars(self, symbols: List[str], timeframe: str, start: datetime,
             end: Optional[datetime] = None, feed: Optional[str] = None,
             adjustment: str = "all") -> Dict[str, pd.DataFrame]:
        params = {
            "symbols": ",".join(symbols), "timeframe": timeframe,
            "start": iso(start), "limit": 10000, "adjustment": adjustment,
            "feed": feed or self.cfg.feed, "sort": "asc",
        }
        if end is not None:
            params["end"] = iso(end)
        pages = self._paged(DATA_BASE, "/v2/stocks/bars", params, "bars")
        merged: Dict[str, list] = {}
        for page in pages:
            for sym, rows in page.items():
                merged.setdefault(sym, []).extend(rows)
        return {s: bars_to_df(r) for s, r in merged.items()}

    def news(self, symbols: List[str], hours: int = 24, max_pages: int = 3) -> Optional[list]:
        """None = الخدمة غير متاحة أو فشل الطلب (وليس "لا أخبار")."""
        start = self.now_utc() - timedelta(hours=hours)
        params = {"symbols": ",".join(symbols), "start": iso(start), "limit": 50, "sort": "desc"}
        out: list = []
        try:
            for _ in range(max_pages):
                d = self._get(DATA_BASE, "/v1beta1/news", params)
                out.extend(d.get("news") or [])
                tok = d.get("next_page_token")
                if not tok:
                    break
                params["page_token"] = tok
            return out
        except ApiError:
            return None

    def most_actives(self, top: int = 50, by: str = "volume") -> list:
        d = self._get(DATA_BASE, "/v1beta1/screener/stocks/most-actives", {"by": by, "top": top})
        return d.get("most_actives", []), d.get("last_updated")

    def movers(self, top: int = 20) -> dict:
        return self._get(DATA_BASE, "/v1beta1/screener/stocks/movers", {"top": top})


def _err_text(r: requests.Response) -> str:
    try:
        j = r.json()
        return str(j.get("message") or j)
    except Exception:
        return (r.text or "")[:200]


# ======================================================================
# أدوات الوقت والبيانات
# ======================================================================


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> datetime:
    # Alpaca يرجع nanoseconds أحياناً: نقصها إلى microseconds
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac = ""
        tz = ""
        for i, ch in enumerate(rest):
            if ch.isdigit():
                frac += ch
            else:
                tz = rest[i:]
                break
        s = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).astimezone(UTC)


def bars_to_df(rows: list) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "trades", "bar_vwap"])
    df = pd.DataFrame(rows)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
                            "n": "trades", "vw": "bar_vwap"}).set_index("t").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df


def to_et(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d.index = d.index.tz_convert(ET)
    return d


def regular_session(df_et: pd.DataFrame) -> pd.DataFrame:
    """09:30–16:00 ET فقط (توقيت بداية الشمعة)."""
    if df_et.empty:
        return df_et
    t = df_et.index.time
    mask = (t >= datetime.strptime("09:30", "%H:%M").time()) & (t < datetime.strptime("16:00", "%H:%M").time())
    return df_et[mask]


def drop_forming_bar(df: pd.DataFrame, minutes: int, now_utc: datetime) -> pd.DataFrame:
    """يحذف الشمعة التي لم تغلق بعد (لا نبني إشارة على شمعة مفتوحة)."""
    if df.empty:
        return df
    last_start = df.index[-1].tz_convert(UTC).to_pydatetime()
    if last_start + timedelta(minutes=minutes) > now_utc:
        return df.iloc[:-1]
    return df


def resample(df_et: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if df_et.empty:
        return df_et
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df_et.resample(f"{minutes}min", label="left", closed="left", origin="start_day").agg(agg)
    return out.dropna(subset=["open"])


def fmt_price(p: Optional[float]) -> str:
    if p is None or (isinstance(p, float) and (math.isnan(p) or math.isinf(p))):
        return "—"
    return f"{p:.4f}" if p < 1 else f"{p:.2f}"


def round_px(p: float) -> float:
    return round(p, 4) if p < 1 else round(p, 2)


# ======================================================================
# فحص الحداثة
# ======================================================================


@dataclass
class PriceCheck:
    symbol: str
    price: Optional[float]
    trade_time_utc: Optional[datetime]
    age_sec: Optional[float]
    freshness: str            # FRESH / DELAYED / STALE / MARKET CLOSED / DATA UNAVAILABLE
    feed: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    quote_time_utc: Optional[datetime] = None
    spread_pct: Optional[float] = None
    note: str = ""


def classify_age(age: Optional[float], market_open: bool, cfg: Config) -> str:
    if age is None:
        return "DATA UNAVAILABLE"
    if not market_open:
        return "MARKET CLOSED"
    if age <= cfg.fresh_sec:
        return "FRESH"
    if age <= cfg.delayed_sec:
        return "DELAYED"
    return "STALE"


def check_price(client: AlpacaClient, symbol: str, market_open: bool) -> PriceCheck:
    cfg = client.cfg
    now = client.now_utc()
    try:
        tr = client.latest_trade(symbol)
    except ApiError as e:
        return PriceCheck(symbol, None, None, None, "DATA UNAVAILABLE", cfg.feed, note=e.message)
    if not tr or "p" not in tr:
        return PriceCheck(symbol, None, None, None, "DATA UNAVAILABLE", cfg.feed,
                          note="لا توجد صفقات مسجلة على هذا الـ feed")
    t = parse_ts(tr["t"])
    age = (now - t).total_seconds()
    pc = PriceCheck(symbol, float(tr["p"]), t, age, classify_age(age, market_open, cfg), cfg.feed)
    try:
        q = client.latest_quote(symbol)
        if q and q.get("bp") and q.get("ap"):
            pc.bid, pc.ask = float(q["bp"]), float(q["ap"])
            pc.quote_time_utc = parse_ts(q["t"])
            mid = (pc.bid + pc.ask) / 2
            if mid > 0 and pc.ask >= pc.bid:
                pc.spread_pct = (pc.ask - pc.bid) / mid * 100
    except ApiError as e:
        pc.note = "Quote غير متاح: " + e.message
    if pc.freshness == "MARKET CLOSED":
        pc.note = (pc.note + " | " if pc.note else "") + "السوق مغلق: هذا آخر سعر تداول وليس سعراً لحظياً"
    elif pc.freshness in ("DELAYED", "STALE"):
        pc.note = (pc.note + " | " if pc.note else "") + \
            f"آخر صفقة على {cfg.feed.upper()} قبل {age:.0f} ثانية — لا يُعتبر لحظياً"
    return pc


def bar_coverage(df1m_session_et: pd.DataFrame, now_et: datetime) -> Tuple[float, int]:
    """نسبة الدقائق التي فيها شمعة 1m منذ الافتتاح، وأطول فجوة بالدقائق."""
    if df1m_session_et.empty:
        return 0.0, -1
    open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    end = min(now_et, close_t)
    expected = max(int((end - open_t).total_seconds() // 60), 1)
    today = df1m_session_et[df1m_session_et.index >= open_t]
    cov = min(len(today) / expected, 1.0)
    idx = list(today.index) + [end]
    gaps = [(b - a).total_seconds() / 60 - 1 for a, b in zip([open_t] + idx[:-1], idx)]
    return cov, int(max(gaps)) if gaps else -1


# ======================================================================
# المؤشرات الفنية
# ======================================================================


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi_wilder(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0)
    dn = (-d).clip(lower=0)
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad
    r = 100 - 100 / (1 + rs)
    r = r.where(ad != 0, 100.0)
    r = r.where(~((au == 0) & (ad == 0)), 50.0)
    return r.where(au.notna())


def atr_wilder(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def session_vwap(df_et: pd.DataFrame) -> pd.Series:
    """VWAP يبدأ من جديد كل جلسة (يُستدعى على بيانات الجلسة النظامية)."""
    tp = (df_et["high"] + df_et["low"] + df_et["close"]) / 3
    day = df_et.index.date
    pv = (tp * df_et["volume"]).groupby(day).cumsum()
    vv = df_et["volume"].groupby(day).cumsum()
    return (pv / vv.replace(0, np.nan)).ffill()


def rvol_time_of_day(df_et: pd.DataFrame, lookback_days: int) -> pd.Series:
    """RVOL = الحجم التراكمي اليوم حتى هذه الشمعة ÷ متوسط الحجم التراكمي لنفس التوقيت في الجلسات السابقة.
    سببي تماماً (يستخدم جلسات سابقة فقط)."""
    if df_et.empty:
        return pd.Series(dtype=float)
    d = df_et.copy()
    d["day"] = d.index.date
    d["tod"] = d.index.strftime("%H:%M")
    d["cumv"] = d.groupby("day")["volume"].cumsum()
    piv = d.pivot_table(index="day", columns="tod", values="cumv", aggfunc="last").sort_index()
    piv = piv.reindex(sorted(piv.columns), axis=1).ffill(axis=1)  # شموع بدون صفقات = نفس التراكمي
    base = piv.shift(1).rolling(lookback_days, min_periods=3).mean()
    out = []
    for ts, row in d.iterrows():
        b = base.at[row["day"], row["tod"]] if row["tod"] in base.columns else np.nan
        out.append(row["cumv"] / b if b and not np.isnan(b) and b > 0 else np.nan)
    return pd.Series(out, index=d.index)


PIVOT_W = 5  # 5 شموع × 5 دقائق = 25 دقيقة على كل جانب (مستويات أوضح من القمم الصغيرة)


def add_indicators(df5_et: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """df5_et: شموع 5m للجلسة النظامية عبر عدة أيام."""
    d = df5_et.copy()
    d["vwap"] = session_vwap(d)
    d["ema9"] = ema(d["close"], 9)
    d["ema20"] = ema(d["close"], 20)
    d["rsi"] = rsi_wilder(d["close"], 14)
    d["atr"] = atr_wilder(d, 14)
    d["rvol"] = rvol_time_of_day(d, cfg.rvol_lookback_days)
    d["vol_avg20"] = d["volume"].rolling(20, min_periods=5).mean()
    # pivots: قمة/قاع محلي بنافذة PIVOT_W يسار و PIVOT_W يمين — مؤكدة فقط بعد PIVOT_W شمعة
    h, l = d["high"], d["low"]
    w = 2 * PIVOT_W + 1
    d["pivot_high"] = (h == h.rolling(w, center=True).max())
    d["pivot_low"] = (l == l.rolling(w, center=True).min())
    return d


def attach_relative(d: pd.DataFrame, spy: Optional[pd.DataFrame]) -> pd.DataFrame:
    """يضيف عائد السهم و SPY منذ افتتاح كل يوم (للقوة النسبية). بدون نظر للمستقبل."""
    d = d.copy()
    day = d.index.date
    d["day_ret"] = d["close"] / d.groupby(day)["open"].transform("first") - 1
    if spy is None or spy.empty:
        d["spy_ret"] = float("nan")
        return d
    sday = spy.index.date
    sr = spy["close"] / spy.groupby(sday)["open"].transform("first") - 1
    sr = sr[~sr.index.duplicated()]
    aligned = sr.reindex(d.index.union(sr.index)).sort_index()
    aligned = aligned.groupby(aligned.index.date).ffill()  # لا تحمل قيمة يوم سابق
    d["spy_ret"] = aligned.reindex(d.index).values
    return d


def add_15m_trend(d5: pd.DataFrame) -> pd.DataFrame:
    d15 = resample(d5[["open", "high", "low", "close", "volume"]], 15)
    # نربط كل شمعة 5m بآخر شمعة 15m اكتملت عند أو قبل إغلاق شمعة الـ 5m (بدون نظر للمستقبل)
    right = pd.DataFrame({
        "done": (d15.index + pd.Timedelta(minutes=15)).tz_convert(UTC),
        "close_15": d15["close"].values,
        "ema20_15": ema(d15["close"], 20).values,
    }).sort_values("done")
    left = pd.DataFrame({"end": (d5.index + pd.Timedelta(minutes=5)).tz_convert(UTC),
                         "pos": np.arange(len(d5))}).sort_values("end")
    m = pd.merge_asof(left, right, left_on="end", right_on="done", direction="backward").sort_values("pos")
    out = d5.copy()
    out["close_15"] = m["close_15"].values
    out["ema20_15"] = m["ema20_15"].values
    return out


# ======================================================================
# الدعم والمقاومة
# ======================================================================


def cluster_levels(levels: List[float], tol: float) -> List[float]:
    levels = sorted(x for x in levels if x and not math.isnan(x))
    out: List[List[float]] = []
    for x in levels:
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [float(np.mean(g)) for g in out]


def levels_at(d: pd.DataFrame, i: int, sessions: int = 3) -> List[float]:
    """مستويات معروفة حتى الشمعة i فقط (pivot مؤكد يحتاج PIVOT_W شمعة بعده)."""
    upto = d.iloc[: i + 1]
    days = sorted(set(upto.index.date))
    keep_days = days[-sessions:]
    w = upto[np.isin(upto.index.date, keep_days)]
    confirmed = w.iloc[:-PIVOT_W] if len(w) > PIVOT_W else w.iloc[:0]
    lv = list(confirmed.loc[confirmed["pivot_high"], "high"]) + list(confirmed.loc[confirmed["pivot_low"], "low"])
    today = d.index[i].date()
    prev = [x for x in days if x < today]
    if prev:
        pdf = upto[upto.index.date == prev[-1]]
        lv += [pdf["high"].max(), pdf["low"].min()]
    tdf = upto[upto.index.date == today]
    orb = tdf.iloc[:3]  # opening range أول 15 دقيقة
    if len(tdf) > 3:
        lv += [orb["high"].max(), orb["low"].min()]
    atr = d["atr"].iloc[i]
    tol = 0.25 * atr if atr and not math.isnan(atr) else 0.0
    return cluster_levels(lv, tol)


# ======================================================================
# الاستراتيجية + التقييم + إدارة المخاطر
# ======================================================================

STATUSES = ["STRONG WATCH", "WATCH", "ENTRY CONFIRMED", "NO TRADE", "INVALIDATED"]


@dataclass
class Signal:
    symbol: str
    bar_time_et: str
    status: str
    score: int
    score_parts: Dict[str, int]
    last_close: float
    vwap: float
    ema9: float
    ema20: float
    rsi: float
    rvol: float
    atr: float
    resistance: Optional[float]
    support: Optional[float]
    breakout_level: Optional[float]
    entry: Optional[float] = None
    stop: Optional[float] = None
    target1: Optional[float] = None
    target2: Optional[float] = None
    rr: Optional[float] = None
    room_rr: Optional[float] = None
    shares: Optional[int] = None
    max_loss: Optional[float] = None
    reasons_pass: List[str] = field(default_factory=list)
    reasons_fail: List[str] = field(default_factory=list)
    invalidation: List[str] = field(default_factory=list)
    chasing: bool = False
    news_risk: List[str] = field(default_factory=list)


def _nz(x, default=np.nan):
    try:
        return default if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)
    except Exception:
        return default


def effective_risk_usd(cfg: Config) -> float:
    """المخاطرة لكل صفقة: مبلغ ثابت أو نسبة من حجم الحساب."""
    if cfg.risk_mode == "pct" and cfg.account_size_usd > 0:
        return round(cfg.account_size_usd * cfg.risk_pct / 100.0, 2)
    return cfg.risk_per_trade_usd


def effective_max_position(cfg: Config) -> float:
    m = cfg.max_position_usd
    if cfg.account_size_usd > 0:
        m = min(m, cfg.account_size_usd)
    return m


def evaluate(d: pd.DataFrame, i: int, cfg: Config, symbol: str,
             spread_pct: Optional[float] = None, news: Optional[List[dict]] = None,
             data_ok: bool = True, data_note: str = "") -> Signal:
    """يقيّم الشمعة المغلقة رقم i باستخدام البيانات حتى i فقط."""
    row = d.iloc[i]
    close, vwap, e9, e20 = row["close"], row["vwap"], row["ema9"], row["ema20"]
    rsi, atr, rvol = _nz(row["rsi"]), _nz(row["atr"]), _nz(row["rvol"])
    lv = levels_at(d, i)
    res_above = sorted(x for x in lv if x > close)
    sup_below = sorted((x for x in lv if x <= close), reverse=True)
    passes, fails = [], []
    parts = {"Trend": 0, "VWAP": 0, "Volume/Liquidity": 0, "Momentum": 0, "Breakout/Retest": 0, "News": 0}

    # ---- Trend (20)
    c15, e15 = _nz(row.get("close_15")), _nz(row.get("ema20_15"))
    if not math.isnan(c15) and c15 > e15:
        parts["Trend"] += 8; passes.append("15m: الإغلاق فوق EMA20 (اتجاه عام صاعد)")
    else:
        fails.append("15m: الإغلاق ليس فوق EMA20")
    if e9 > e20:
        parts["Trend"] += 8; passes.append("5m: EMA9 أعلى EMA20")
    else:
        fails.append("5m: EMA9 ليست أعلى EMA20")
    if i >= 3 and e20 > d["ema20"].iloc[i - 3]:
        parts["Trend"] += 4

    # ---- VWAP (20)
    above_vwap = close > vwap
    if above_vwap:
        parts["VWAP"] += 12; passes.append("السعر أعلى VWAP")
    else:
        fails.append("السعر ليس أعلى VWAP")
    ext_vwap = (close - vwap) / atr if atr and atr > 0 else np.nan
    if above_vwap and not math.isnan(ext_vwap) and ext_vwap <= 1.5:
        parts["VWAP"] += 8

    # ---- Volume & Liquidity (20)
    if not math.isnan(rvol):
        parts["Volume/Liquidity"] += 10 if rvol >= 2 else 7 if rvol >= 1.5 else 4 if rvol >= 1.2 else 0
        (passes if rvol >= 1.5 else fails).append(f"RVOL = {rvol:.2f} (المطلوب ≥ 1.5)")
    else:
        fails.append("RVOL غير متاح (تاريخ غير كافٍ)")
    if spread_pct is not None:
        if spread_pct < 0.5:
            parts["Volume/Liquidity"] += 5
        else:
            fails.append(f"Spread واسع {spread_pct:.2f}%")
    day_bars = d.iloc[: i + 1]
    day_bars = day_bars[day_bars.index.date == d.index[i].date()]
    active = (day_bars["volume"] > 0).mean() if len(day_bars) else 0
    if active >= 0.8:
        parts["Volume/Liquidity"] += 5
    else:
        fails.append(f"سيولة متقطعة: {active:.0%} فقط من شموع 5m فيها تداول على هذا الـ feed")

    # ---- Momentum (15)
    if not math.isnan(rsi):
        if 55 <= rsi <= 75:
            parts["Momentum"] += 10; passes.append(f"RSI = {rsi:.1f} (زخم صحي)")
        elif 50 <= rsi < 55:
            parts["Momentum"] += 5
        elif rsi > 80:
            fails.append(f"RSI = {rsi:.1f} تشبع شرائي")
    rng = row["high"] - row["low"]
    if rng > 0 and (row["close"] - row["low"]) / rng >= 0.7 and row["close"] > row["open"]:
        parts["Momentum"] += 5

    # ---- Breakout / Retest (15)
    breakout_level = None
    bo_idx = None
    for k in range(i, max(i - 3, 0), -1):  # آخر 3 شموع
        lv_k = levels_at(d, k - 1) if k >= 1 else []
        prev_c, cur_c = d["close"].iloc[k - 1], d["close"].iloc[k]
        crossed = [x for x in lv_k if prev_c <= x < cur_c]
        if crossed:
            breakout_level, bo_idx = max(crossed), k
            break
    failed_breakout = False
    if breakout_level is not None:
        if close > breakout_level:
            parts["Breakout/Retest"] += 8
            passes.append(f"إغلاق شمعة 5m فوق المقاومة {fmt_price(breakout_level)}")
            bo_vol = d["volume"].iloc[bo_idx]
            avgv = _nz(d["vol_avg20"].iloc[bo_idx])
            if not math.isnan(avgv) and avgv > 0 and bo_vol >= 1.5 * avgv:
                parts["Breakout/Retest"] += 4; passes.append("حجم شمعة الاختراق ≥ 1.5× المتوسط")
            else:
                fails.append("حجم شمعة الاختراق ليس أعلى بوضوح من المتوسط")
            after = d.iloc[bo_idx + 1: i + 1]
            if len(after) and (after["low"] <= breakout_level + 0.25 * atr).any() and (after["close"] > breakout_level).all():
                parts["Breakout/Retest"] += 3; passes.append("Retest للمستوى وصمد")
        else:
            failed_breakout = True
            fails.append(f"اختراق فاشل: عاد الإغلاق تحت {fmt_price(breakout_level)}")
    else:
        fails.append("لا يوجد اختراق مؤكد لمقاومة في آخر 3 شموع 5m")

    # ---- News (10)
    news_risk: List[str] = []
    if news is None:
        fails.append("الأخبار: غير متاحة/غير مفحوصة (0 نقاط)")
    elif len(news) == 0:
        fails.append(f"لا أخبار عن السهم خلال آخر {cfg.news_lookback_hours} ساعة (0 نقاط)")
    else:
        news_risk = sorted({t for n in news for t in n.get("tags", []) if t in RISK_TAGS})
        if news_risk:
            fails.append("⚠️ أخبار مخاطرة: " + "، ".join(TAG_LABELS[t] for t in news_risk) +
                         " — 0 نقاط للأخبار، ولا تأكيد دخول قبل أن تقرأها بنفسك")
        else:
            parts["News"] = 10
            passes.append(f"{len(news)} خبر خلال {cfg.news_lookback_hours} ساعة بدون علامات مخاطرة (محفز محتمل — اقرأه بنفسك)")

    score = int(sum(parts.values()))

    sig = Signal(symbol=symbol, bar_time_et=d.index[i].strftime("%Y-%m-%d %H:%M"), status="NO TRADE",
                 score=score, score_parts=parts, last_close=close, vwap=vwap, ema9=e9, ema20=e20,
                 rsi=rsi, rvol=rvol, atr=atr, resistance=res_above[0] if res_above else None,
                 support=sup_below[0] if sup_below else None, breakout_level=breakout_level,
                 reasons_pass=passes, reasons_fail=fails)

    # ---- خطة الدخول وإدارة المخاطر (إن وجد اختراق قائم)
    chasing = False
    if not math.isnan(atr) and atr > 0:
        if (close - vwap) > cfg.chase_vwap_atr * atr or (close - e9) > cfg.chase_ema9_atr * atr:
            chasing = True
            fails.append("مطاردة: السعر ممتد كثيراً عن VWAP/EMA9 — انتظر Retest")

    plan_ok = False
    if breakout_level is not None and close > breakout_level and not math.isnan(atr) and atr > 0:
        tick = 0.0001 if close < 1 else 0.01
        entry = round_px(close)
        bo_low = d["low"].iloc[bo_idx]
        stop = math.floor(min(breakout_level - 0.25 * atr, bo_low) / tick + 1e-9) * tick  # تقريب للأسفل
        stop = round_px(stop)
        risk = round(entry - stop, 6)
        if risk <= 0:
            fails.append("وقف غير منطقي")
        elif risk > cfg.max_stop_atr * atr:
            fails.append(f"مسافة الوقف {risk / atr:.2f}×ATR أكبر من {cfg.max_stop_atr} — لا دخول")
        else:
            t1, t2 = round_px(entry + cfg.target_r * risk), round_px(entry + (cfg.target_r + 1) * risk)
            next_res = [x for x in lv if x > entry]
            room = (next_res[0] - entry) / risk if next_res else float("inf")
            sig.entry, sig.stop = entry, stop
            sig.target1, sig.target2 = t1, t2
            sig.rr = round((t1 - entry) / risk, 2)
            sig.room_rr = None if room == float("inf") else round(room, 2)
            avg_vol = _nz(d["vol_avg20"].iloc[i], 0)
            sh = math.floor(effective_risk_usd(cfg) / risk)
            sh = min(sh, math.floor(effective_max_position(cfg) / entry))
            if avg_vol > 0:
                sh = min(sh, math.floor(cfg.liquidity_cap_pct * avg_vol))
            sig.shares = max(sh, 0)
            sig.max_loss = round(sig.shares * risk, 2)
            sig.invalidation = [
                f"إغلاق شمعة 5m تحت {fmt_price(breakout_level)} (فشل الاختراق)",
                f"إغلاق شمعة 5m تحت VWAP ({fmt_price(vwap)})",
                f"لمس وقف الخسارة {fmt_price(stop)}",
                "توقف تحديث البيانات (STALE) أو انقطاع الاتصال",
                f"الوصول إلى {cfg.no_new_entries_after} ET دون تفعيل الدخول",
            ]
            need_room = min(cfg.min_rr, cfg.target_r)
            if room < need_room:
                fails.append(f"مقاومة قريبة عند {fmt_price(next_res[0])}: المجال المتاح {room:.2f}R فقط (< {need_room:g})")
            elif sig.shares < 1:
                fails.append("حجم المركز = 0 (المخاطرة أو السيولة لا تسمح)")
            else:
                plan_ok = True

    sig.chasing = chasing
    sig.news_risk = news_risk
    # ---- الحالة النهائية
    time_ok = d.index[i].strftime("%H:%M") < cfg.no_new_entries_after
    core = ((above_vwap or not cfg.req_vwap) and (e9 > e20 or not cfg.req_ema)
            and breakout_level is not None and close > breakout_level)
    rvol_ok = (not cfg.req_rvol) or (not math.isnan(rvol) and rvol >= 1.5)
    chase_ok = (not cfg.req_no_chase) or not chasing
    extra_ok = True
    # 1) الفارق بين الشراء والبيع مقارنة بمسافة الوقف
    if cfg.max_spread_stop_pct > 0 and sig.entry and sig.stop:
        if spread_pct is None:
            fails.append("الفارق (Spread): غير متاح الآن — لم يُفحص")
        else:
            sp_usd = spread_pct / 100 * close
            ratio = sp_usd / max(sig.entry - sig.stop, 1e-9) * 100
            if ratio > cfg.max_spread_stop_pct:
                extra_ok = False
                fails.append(f"الفارق {fmt_price(sp_usd)} = {ratio:.0f}% من مسافة الوقف (الحد {cfg.max_spread_stop_pct:g}%)")
            else:
                passes.append(f"الفارق {ratio:.0f}% فقط من مسافة الوقف")
    # 2) لا دخول في أول دقائق الافتتاح (وقت إغلاق الشمعة)
    if cfg.skip_open_min > 0:
        t = d.index[i]
        mins = t.hour * 60 + t.minute - 570 + 5
        if mins < cfg.skip_open_min:
            extra_ok = False
            fails.append(f"أول {cfg.skip_open_min} دقيقة بعد الافتتاح — تقلب عالٍ، لا دخول")
    # 3) القوة النسبية مقابل SPY منذ افتتاح اليوم
    if cfg.req_rel_spy:
        spy_r = _nz(row.get("spy_ret")) if "spy_ret" in d.columns else float("nan")
        own_r = _nz(row.get("day_ret")) if "day_ret" in d.columns else float("nan")
        if math.isnan(spy_r) or math.isnan(own_r):
            extra_ok = False
            fails.append("القوة مقابل SPY: بيانات SPY غير متاحة")
        elif own_r <= spy_r:
            extra_ok = False
            fails.append(f"أضعف من SPY اليوم ({own_r * 100:+.2f}% مقابل {spy_r * 100:+.2f}%)")
        else:
            passes.append(f"أقوى من SPY اليوم ({own_r * 100:+.2f}% مقابل {spy_r * 100:+.2f}%)")
    # 4) خبر إيجابي شرط
    if cfg.req_news_catalyst:
        if not news or news_risk:
            extra_ok = False
            fails.append("لا يوجد خبر إيجابي محفز (شرط مفعّل)")
    if failed_breakout:
        sig.status = "INVALIDATED"
    elif not data_ok:
        sig.status = "NO TRADE"
        fails.insert(0, "البيانات غير حديثة: " + (data_note or "لا يمكن توليد إشارة لحظية"))
    elif core and plan_ok and chase_ok and time_ok and score >= cfg.min_entry_score and rvol_ok and extra_ok:
        sig.status = "WATCH" if news_risk else "ENTRY CONFIRMED"
    elif score >= 85:
        sig.status = "STRONG WATCH"
    elif score >= 60:
        sig.status = "WATCH"
    else:
        sig.status = "NO TRADE"
    if not time_ok and sig.status == "ENTRY CONFIRMED":
        sig.status = "WATCH"
    return sig


# ======================================================================
# التحليل الكامل لسهم (المراحل 4–7)
# ======================================================================


@dataclass
class Analysis:
    symbol: str
    price: PriceCheck
    market_open: bool
    signal: Optional[Signal]
    coverage_1m: Optional[float]
    max_gap_1m: Optional[int]
    bars5: Optional[pd.DataFrame]
    bars1_today: Optional[pd.DataFrame]
    analyzed_at_utc: datetime
    notes: List[str] = field(default_factory=list)
    news: Optional[List[dict]] = None
    change_pct: Optional[float] = None
    prev_close: Optional[float] = None
    data_note: Optional[str] = None


def fetch_bars_bundle(client: AlpacaClient, symbols: List[str]) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    now = client.now_utc()
    start5 = now - timedelta(days=client.cfg.history_calendar_days)
    b5 = client.bars(symbols, "5Min", start5)
    start1 = now - timedelta(days=4)
    b1 = client.bars(symbols, "1Min", start1)
    return b5, b1


def analyze(client: AlpacaClient, symbol: str, market_open: Optional[bool] = None,
            bundles: Optional[Tuple[dict, dict]] = None, news: Any = "fetch",
            price: Optional["PriceCheck"] = None, spy_d5: Optional[pd.DataFrame] = None) -> Analysis:
    cfg = client.cfg
    notes: List[str] = []
    if market_open is None:
        try:
            market_open = bool(client.clock()["is_open"])
        except ApiError as e:
            market_open = False
            notes.append("تعذر جلب حالة السوق: " + e.message)
    now = client.now_utc()
    pc = price if price is not None else check_price(client, symbol, market_open)
    try:
        b5, b1 = bundles if bundles else fetch_bars_bundle(client, [symbol])
    except ApiError as e:
        return Analysis(symbol, pc, market_open, None, None, None, None, None, now, notes + ["فشل جلب الشموع: " + e.message])
    df5 = b5.get(symbol, pd.DataFrame())
    df1 = b1.get(symbol, pd.DataFrame())
    if df5.empty:
        return Analysis(symbol, pc, market_open, None, None, None, None, None, now,
                        notes + [f"لا توجد شموع 5m على {cfg.feed.upper()} لهذا السهم في الفترة المطلوبة"])
    df5 = drop_forming_bar(df5, 5, now)
    df1 = drop_forming_bar(df1, 1, now) if not df1.empty else df1
    d5 = regular_session(to_et(df5))
    d1 = regular_session(to_et(df1)) if not df1.empty else df1
    if len(d5) < 30:
        return Analysis(symbol, pc, market_open, None, None, None, d5, d1, now,
                        notes + [f"عدد شموع 5m غير كافٍ ({len(d5)}) لحساب المؤشرات بدقة"])
    d5 = add_15m_trend(add_indicators(d5, cfg))
    if cfg.req_rel_spy:
        if spy_d5 is None and symbol != "SPY":
            try:
                sb, _ = fetch_bars_bundle(client, ["SPY"])
                if not sb.get("SPY", pd.DataFrame()).empty:
                    spy_d5 = regular_session(to_et(drop_forming_bar(sb["SPY"], 5, now)))
            except ApiError:
                spy_d5 = None
        d5 = attach_relative(d5, spy_d5 if symbol != "SPY" else d5)
    now_et = now.astimezone(ET)
    cov = gap = None
    if market_open and not d1.empty:
        cov, gap = bar_coverage(d1, now_et)
        if cov < 0.5:
            notes.append(f"تغطية شموع 1m اليوم {cov:.0%} فقط — تداول متقطع على {cfg.feed.upper()}")
    last_bar_date = d5.index[-1].date()
    if market_open and last_bar_date != now_et.date():
        notes.append("لا توجد شموع لجلسة اليوم بعد — التحليل على جلسة سابقة")
    if isinstance(news, str) and news == "fetch":
        news = news_for_symbol(client.news([symbol], cfg.news_lookback_hours), symbol)
    data_ok = market_open and pc.freshness == "FRESH" and last_bar_date == now_et.date()
    data_note = pc.freshness if pc.freshness != "FRESH" else "لا شموع لليوم"
    if not market_open:
        data_note = "السوق مغلق — التحليل على آخر جلسة وليس لحظياً"
    sig = evaluate(d5, len(d5) - 1, cfg, symbol, pc.spread_pct, news, data_ok, data_note)
    # تأكيد 1m: آخر شمعة 1m مغلقة فوق مستوى الاختراق
    if sig.status == "ENTRY CONFIRMED" and not d1.empty and sig.breakout_level:
        if d1["close"].iloc[-1] <= sig.breakout_level:
            sig.status = "WATCH"
            sig.reasons_fail.append("1m: آخر شمعة دقيقة أغلقت تحت مستوى الاختراق — لا تأكيد")
        else:
            sig.reasons_pass.append("1m: آخر شمعة دقيقة فوق مستوى الاختراق (تأكيد)")
    out = Analysis(symbol, pc, market_open, sig, cov, gap, d5, d1, now, notes, news)
    days = sorted(set(d5.index.date))
    if len(days) >= 2:
        out.prev_close = float(d5[d5.index.date == days[-2]]["close"].iloc[-1])
        cur = pc.price if pc.price is not None else float(d5["close"].iloc[-1])
        out.change_pct = (cur / out.prev_close - 1) * 100 if out.prev_close else None
    return out


# ======================================================================
# العرض (Dashboard)
# ======================================================================

STATUS_COLORS = {
    "ENTRY CONFIRMED": "#1b7f3b", "STRONG WATCH": "#2d6cdf", "WATCH": "#b07d00",
    "NO TRADE": "#6b6b6b", "INVALIDATED": "#b3261e",
}
FRESH_COLORS = {"FRESH": "#1b7f3b", "DELAYED": "#b07d00", "STALE": "#b3261e",
                "MARKET CLOSED": "#6b6b6b", "DATA UNAVAILABLE": "#b3261e"}


def _t(dt: Optional[datetime]) -> str:
    if not dt:
        return "—"
    return dt.astimezone(ET).strftime("%H:%M:%S ET") + " / " + dt.astimezone(RIYADH).strftime("%H:%M:%S RUH")


def dashboard_rows(analyses: List[Analysis]) -> pd.DataFrame:
    rows = []
    for a in analyses:
        s, p = a.signal, a.price
        rows.append({
            "Symbol": a.symbol,
            "Last Price": fmt_price(p.price),
            "Last Update Time": _t(p.trade_time_utc),
            "Age (s)": "—" if p.age_sec is None else f"{p.age_sec:.0f}",
            "Freshness": p.freshness,
            "Data Feed": p.feed.upper() + " (جزء من السوق)",
            "Bid / Ask": f"{fmt_price(p.bid)} / {fmt_price(p.ask)}" if p.bid else "—",
            "Volume (today, feed)": (f"{int(a.bars5[a.bars5.index.date == a.bars5.index[-1].date()]['volume'].sum()):,}"
                                     if a.bars5 is not None and len(a.bars5) else "—"),
            "VWAP": fmt_price(s.vwap) if s else "—",
            "EMA 9 / EMA 20": f"{fmt_price(s.ema9)} / {fmt_price(s.ema20)}" if s else "—",
            "RSI": f"{s.rsi:.1f}" if s and not math.isnan(s.rsi) else "—",
            "RVOL": f"{s.rvol:.2f}" if s and not math.isnan(s.rvol) else "—",
            "Entry": fmt_price(s.entry) if s else "—",
            "Stop Loss": fmt_price(s.stop) if s else "—",
            "Target 1": fmt_price(s.target1) if s else "—",
            "Target 2": fmt_price(s.target2) if s else "—",
            "Risk/Reward": f"1:{s.rr}" if s and s.rr else "—",
            "Shares": s.shares if s and s.shares is not None else "—",
            "Max Loss $": s.max_loss if s and s.max_loss is not None else "—",
            "Technical Score": s.score if s else "—",
            "Signal Status": s.status if s else "NO TRADE",
            "Bar (ET)": s.bar_time_et if s else "—",
        })
    return pd.DataFrame(rows)


def dashboard_html(analyses: List[Analysis], client: AlpacaClient, market_open: bool) -> str:
    df = dashboard_rows(analyses)
    now = client.now_utc()
    head = (f"<div dir='rtl' style='font-family:-apple-system,sans-serif;font-size:14px;margin-bottom:6px'>"
            f"<b>وقت التحديث:</b> {_t(now)} &nbsp;|&nbsp; <b>السوق:</b> {'مفتوح' if market_open else 'مغلق'}"
            f" &nbsp;|&nbsp; <b>Feed:</b> {client.cfg.feed.upper()} (بورصة IEX فقط، ليست SIP)"
            f" &nbsp;|&nbsp; <b>فرق الساعة مع الخادم:</b> {client.clock_skew_sec:+.1f}s"
            f" &nbsp;|&nbsp; <b>طلبات API:</b> {client.call_count}</div>")

    def cell(col, v):
        style = "padding:4px 8px;border-bottom:1px solid #ddd;white-space:nowrap;"
        if col == "Signal Status":
            style += f"color:white;background:{STATUS_COLORS.get(v, '#444')};font-weight:600;"
        if col == "Freshness":
            style += f"color:{FRESH_COLORS.get(v, '#000')};font-weight:600;"
        return f"<td style='{style}'>{v}</td>"

    th = "".join(f"<th style='padding:4px 8px;background:#222;color:#fff;position:sticky;top:0;white-space:nowrap'>{c}</th>" for c in df.columns)
    trs = "".join("<tr>" + "".join(cell(c, r[c]) for c in df.columns) + "</tr>" for _, r in df.iterrows())
    table = f"<div style='overflow-x:auto;-webkit-overflow-scrolling:touch'><table style='border-collapse:collapse;font-size:13px'><tr>{th}</tr>{trs}</table></div>"
    details = []
    for a in analyses:
        s = a.signal
        items = []
        if a.price.note:
            items.append(f"⚠️ {a.price.note}")
        items += [f"⚠️ {n}" for n in a.notes]
        if a.coverage_1m is not None:
            items.append(f"تغطية 1m اليوم: {a.coverage_1m:.0%} — أطول فجوة: {a.max_gap_1m} دقيقة")
        if s:
            items.append("التقييم: " + ", ".join(f"{k} {v}" for k, v in s.score_parts.items()))
            items += [f"✅ {x}" for x in s.reasons_pass]
            items += [f"❌ {x}" for x in s.reasons_fail]
            if s.invalidation:
                items.append("<b>شروط الإلغاء:</b> " + " • ".join(s.invalidation))
            if s.room_rr is not None:
                items.append(f"المسافة حتى المقاومة التالية: {s.room_rr}R")
        details.append(f"<details dir='rtl'><summary><b>{a.symbol}</b> — لماذا هذه الحالة؟</summary><ul>" +
                       "".join(f"<li>{x}</li>" for x in items) + "</ul>" + news_html(a.news, 3) + "</details>")
    foot = ("<div dir='rtl' style='font-size:12px;color:#666;margin-top:6px'>Technical Score يقيس توافق الشروط الفنية "
            "وليس احتمال الربح. كل الصفقات افتراضية. الحجم وVWAP مبنيان على صفقات IEX فقط.</div>")
    return head + table + "".join(details) + foot


def print_analysis(a: Analysis) -> None:
    p, s = a.price, a.signal
    print(f"=== {a.symbol} ===")
    print(f"آخر سعر: {fmt_price(p.price)}  | المصدر: {p.feed.upper()}  | الحالة: {p.freshness}")
    print(f"وقت آخر صفقة: {_t(p.trade_time_utc)}  | العمر: {'—' if p.age_sec is None else f'{p.age_sec:.0f} ثانية'}")
    if p.bid:
        print(f"Bid/Ask: {fmt_price(p.bid)} / {fmt_price(p.ask)}  | Spread: {p.spread_pct:.2f}%  | وقت الـ Quote: {_t(p.quote_time_utc)}")
    if p.note:
        print("⚠️", p.note)
    for n in a.notes:
        print("⚠️", n)
    if not s:
        print("لا توجد إشارة (بيانات غير كافية).")
        return
    print(f"آخر شمعة 5m مغلقة: {s.bar_time_et} ET | Close {fmt_price(s.last_close)}")
    print(f"VWAP {fmt_price(s.vwap)} | EMA9 {fmt_price(s.ema9)} | EMA20 {fmt_price(s.ema20)} | RSI {s.rsi:.1f} | ATR {fmt_price(s.atr)} | RVOL {'—' if math.isnan(s.rvol) else f'{s.rvol:.2f}'}")
    print(f"أقرب دعم: {fmt_price(s.support)} | أقرب مقاومة: {fmt_price(s.resistance)} | مستوى اختراق: {fmt_price(s.breakout_level)}")
    print(f"Score: {s.score}/100 → {s.score_parts}")
    print(f"الحالة: {s.status}")
    if s.entry:
        print(f"Entry {fmt_price(s.entry)} | Stop {fmt_price(s.stop)} | T1 {fmt_price(s.target1)} | T2 {fmt_price(s.target2)} | R/R 1:{s.rr} | Shares {s.shares} | Max loss ${s.max_loss}")
    for x in s.reasons_pass:
        print("  ✅", x)
    for x in s.reasons_fail:
        print("  ❌", x)
    if s.invalidation:
        print("شروط الإلغاء:")
        for x in s.invalidation:
            print("  •", x)
    if a.news is None:
        print("📰 الأخبار: غير متاحة")
    elif not a.news:
        print("📰 لا أخبار خلال آخر 24 ساعة")
    else:
        print("📰 آخر الأخبار:")
        for n in a.news[:5]:
            tg = " ".join(TAG_LABELS[t] for t in n["tags"])
            print(f"  • {_t(n['time_utc']) if n['time_utc'] else '—'} | {n['headline']} {tg}")


def plot_symbol(a: Analysis, sessions: int = 2):
    """رسم شموع 5m مع VWAP وEMA ومستويات الخطة (plotly)."""
    import plotly.graph_objects as go
    d = a.bars5
    days = sorted(set(d.index.date))[-sessions:]
    d = d[np.isin(d.index.date, days)]
    x = d.index.strftime("%m-%d %H:%M")
    fig = go.Figure()
    fig.add_candlestick(x=x, open=d["open"], high=d["high"], low=d["low"], close=d["close"], name="5m")
    fig.add_scatter(x=x, y=d["vwap"], name="VWAP", line=dict(width=1.5))
    fig.add_scatter(x=x, y=d["ema9"], name="EMA9", line=dict(width=1))
    fig.add_scatter(x=x, y=d["ema20"], name="EMA20", line=dict(width=1))
    s = a.signal
    if s:
        for name, val, dash in [("Entry", s.entry, "solid"), ("Stop", s.stop, "dot"), ("T1", s.target1, "dash"),
                                ("T2", s.target2, "dash"), ("Resistance", s.resistance, "dot"), ("Support", s.support, "dot")]:
            if val:
                fig.add_hline(y=val, line_dash=dash, annotation_text=f"{name} {fmt_price(val)}", line_width=1)
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=40, b=10),
                      title=f"{a.symbol} — 5m ({a.price.feed.upper()}) — {_t(a.analyzed_at_utc)}",
                      xaxis=dict(type="category", nticks=8))
    return fig


# ======================================================================
# الماسح (Scanner)
# ======================================================================


def build_universe(client: AlpacaClient, watchlist: List[str], top_active: int = 30,
                   min_price: float = 1.0, max_price: float = 50.0) -> Tuple[List[str], List[str]]:
    notes: List[str] = []
    syms: List[str] = list(dict.fromkeys(s.upper() for s in watchlist))
    try:
        ma, upd = client.most_actives(top_active)
        notes.append(f"Most actives آخر تحديث: {upd}")
        syms += [x["symbol"] for x in ma]
    except ApiError as e:
        notes.append("Most actives غير متاح: " + e.message)
    try:
        mv = client.movers(20)
        syms += [x["symbol"] for x in mv.get("gainers", [])]
        notes.append(f"Movers آخر تحديث: {mv.get('last_updated')}")
    except ApiError as e:
        notes.append("Movers غير متاح: " + e.message)
    syms = list(dict.fromkeys(syms))
    # فلترة السعر عبر snapshot (المرشحون الإضافيون فقط؛ قائمة المراقبة تبقى)
    try:
        snaps = client.snapshots(syms)
    except ApiError as e:
        notes.append("Snapshots غير متاحة: " + e.message)
        return syms, notes
    keep = []
    for s in syms:
        sn = snaps.get(s) or {}
        p = (sn.get("latestTrade") or {}).get("p")
        if s in watchlist or (p and min_price <= p <= max_price):
            keep.append(s)
    return keep, notes


INDEX_SYMS = ["SPY", "QQQ"]
LAST_MOOD: dict = {"state": "UNKNOWN"}


def self_test(client: "AlpacaClient", notifier=None, send_telegram: bool = False) -> List[dict]:
    """فحص سريع لكل اتصال. لا يطبع المفاتيح ولا يرسل أوامر."""
    res: List[dict] = []
    def step(name, fn):
        t = time.time()
        try:
            note = fn()
            res.append({"الفحص": name, "النتيجة": "✅", "التفاصيل": note, "الزمن ms": int((time.time() - t) * 1000)})
        except Exception as e:
            msg = getattr(e, "message", None) or f"{type(e).__name__}: {e}"
            res.append({"الفحص": name, "النتيجة": "❌", "التفاصيل": str(msg)[:200], "الزمن ms": int((time.time() - t) * 1000)})
    def acc():
        a = client.account()
        return f"الحالة {a.get('status')} — حساب Paper"
    def clk():
        c = client.clock()
        return ("السوق مفتوح" if c.get("is_open") else "السوق مغلق") + f" • فرق ساعة الجهاز {client.clock_skew_sec:+.1f} ث"
    def snap():
        sn = client.snapshots(["SPY", "QQQ"])
        is_open = bool(client.clock().get("is_open"))
        pc = price_from_snapshot("SPY", sn.get("SPY"), is_open, client.now_utc(), client.cfg)
        return f"SPY {fmt_price(pc.price) if pc.price else '—'} • {pc.freshness}"
    def bars():
        b = client.bars(["SPY"], "5Min", client.now_utc() - timedelta(days=3))
        n = len(b.get("SPY", [])) if b else 0
        return f"{n} شمعة 5 دقائق (IEX)"
    def sip():
        end = client.now_utc() - timedelta(minutes=20)
        b = client.bars(["SPY"], "5Min", end - timedelta(days=2), end, feed="sip")
        return f"SIP التاريخي متاح ({len(b.get('SPY', [])) if b else 0} شمعة)"
    def news():
        n = client.news(["SPY"], 24, max_pages=1)
        if n is None:
            raise RuntimeError("الأخبار غير متاحة")
        return f"{len(n)} خبر خلال 24 ساعة"
    step("الحساب (Paper)", acc)
    step("ساعة السوق", clk)
    step("الأسعار اللحظية (Snapshots)", snap)
    step("الشموع (IEX)", bars)
    step("بيانات SIP التاريخية", sip)
    step("الأخبار", news)
    if notifier is not None:
        def tg():
            if not notifier.ready:
                raise RuntimeError("تيليجرام غير مربوط")
            if send_telegram:
                if not notifier.send("🧪 رسالة اختبار من تداولي — الاتصال يعمل"):
                    raise RuntimeError(notifier.last_error or "فشل الإرسال")
                return "أُرسلت رسالة اختبار"
            return "مربوط (لم تُرسل رسالة)"
        step("تيليجرام", tg)
    return res


def market_mood(idx: List["Analysis"]) -> dict:
    """حالة السوق العام من SPY و QQQ: فوق/تحت VWAP واتجاه EMA9/EMA20 على 5 دقائق."""
    parts, pts, below = [], 0, 0
    for a in idx:
        s = a.signal
        if not s:
            continue
        up_v, up_e = s.last_close > s.vwap, s.ema9 > s.ema20
        pts += int(up_v) + int(up_e)
        below += int(not up_v)
        parts.append({"symbol": a.symbol, "price": a.price.price, "chg": a.change_pct, "above_vwap": bool(up_v),
                      "ema_up": bool(up_e), "freshness": a.price.freshness})
    if len(parts) < 2:
        return {"state": "UNKNOWN", "parts": parts, "text": "حالة السوق العام غير متاحة"}
    if pts >= 3:
        st_, txt = "UP", "صاعد"
    elif pts <= 1 and below == len(parts):
        st_, txt = "DOWN", "هابط"
    else:
        st_, txt = "NEUTRAL", "محايد"
    return {"state": st_, "parts": parts, "text": txt}


def apply_market_filter(analyses: List["Analysis"], mood: dict) -> None:
    if mood.get("state") != "DOWN":
        return
    for a in analyses:
        s = a.signal
        if s and s.status == "ENTRY CONFIRMED":
            s.status = "WATCH"
            s.reasons_fail.insert(0, "السوق العام هابط (SPY و QQQ تحت VWAP واتجاههما هابط) — لا تأكيد دخول")


def scan(client: AlpacaClient, symbols: List[str], with_news: bool = True,
         news_map: Optional[Dict[str, Optional[List[dict]]]] = None,
         cache: Optional["BarCache"] = None) -> Tuple[List[Analysis], bool]:
    global LAST_MOOD
    user_syms = list(symbols)
    if client.cfg.market_filter:
        symbols = list(dict.fromkeys(list(symbols) + INDEX_SYMS))
    elif client.cfg.req_rel_spy:
        symbols = list(dict.fromkeys(list(symbols) + ["SPY"]))
    try:
        market_open = bool(client.clock()["is_open"])
    except ApiError:
        market_open = False
    b5, b1 = {}, {}
    if cache is not None:
        try:
            b5, b1 = cache.get(client, symbols)
        except ApiError as e:
            print("⚠️ فشل جلب الشموع:", e.message)
    else:
        for i in range(0, len(symbols), 20):
            part = symbols[i:i + 20]
            try:
                x5, x1 = fetch_bars_bundle(client, part)
                b5.update(x5); b1.update(x1)
            except ApiError as e:
                print("⚠️ فشل جلب شموع لمجموعة:", part, e.message)
    prices: Dict[str, PriceCheck] = {}
    try:  # طلب واحد لكل 100 رمز بدل طلبين لكل رمز
        snaps = client.snapshots(symbols)
        now = client.now_utc()
        for sym in symbols:
            prices[sym] = price_from_snapshot(sym, snaps.get(sym), market_open, now, client.cfg)
    except ApiError:
        prices = {}
    if news_map is None:
        news_map = {s: None for s in symbols}
        if with_news:
            raw = client.news(symbols, client.cfg.news_lookback_hours)  # طلب واحد لكل الرموز
            news_map = {s: news_for_symbol(raw, s) for s in symbols}
    spy_d5 = None
    if client.cfg.req_rel_spy and not b5.get("SPY", pd.DataFrame()).empty:
        spy_d5 = regular_session(to_et(drop_forming_bar(b5["SPY"], 5, client.now_utc())))
    out = []
    for s in symbols:
        an = analyze(client, s, market_open, (b5, b1), news_map.get(s), price=prices.get(s), spy_d5=spy_d5)
        if cache is not None and cache.describe(s):
            an.data_note = cache.describe(s)
        out.append(an)
    if client.cfg.market_filter:
        LAST_MOOD = market_mood([a for a in out if a.symbol in INDEX_SYMS])
        apply_market_filter(out, LAST_MOOD)
        out = [a for a in out if a.symbol in user_syms]
    else:
        out = [a for a in out if a.symbol in user_syms]
    order = {k: i for i, k in enumerate(["ENTRY CONFIRMED", "STRONG WATCH", "WATCH", "INVALIDATED", "NO TRADE"])}
    out.sort(key=lambda a: (order.get(a.signal.status if a.signal else "NO TRADE", 9), -(a.signal.score if a.signal else 0)))
    return out, market_open


# ======================================================================
# الاختبار التاريخي (المرحلة 9)
# ======================================================================


@dataclass
class Trade:
    symbol: str
    signal_time: str
    entry_time: str
    entry: float
    stop: float
    t1: float
    t2: float
    shares: int
    score: int
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    r_multiple: float = 0.0
    pnl: float = 0.0
    hit_t1: bool = False


def backtest_df(d: pd.DataFrame, symbol: str, cfg: Config, min_bar_index: int = 40) -> List[Trade]:
    """محاكاة بدون نظر للمستقبل: الإشارة على إغلاق الشمعة i، الدخول على افتتاح i+1.
    إذا لُمس الوقف والهدف في نفس الشمعة نفترض الوقف أولاً (افتراض متحفظ)."""
    trades: List[Trade] = []
    i = min_bar_index
    n = len(d)
    slip = cfg.slippage_pct
    while i < n - 1:
        day = d.index[i].date()
        sig = evaluate(d, i, cfg, symbol, None, None, True, "")
        if sig.status != "ENTRY CONFIRMED" or d.index[i + 1].date() != day:
            i += 1
            continue
        j = i + 1
        e = d["open"].iloc[j] * (1 + slip)
        stop = sig.stop
        risk = e - stop
        if risk <= 0 or risk > cfg.max_stop_atr * 1.5 * sig.atr:
            i += 1
            continue
        t1, t2 = e + cfg.target_r * risk, e + (cfg.target_r + 1) * risk
        sh = max(min(math.floor(effective_risk_usd(cfg) / risk), math.floor(effective_max_position(cfg) / e)), 0)
        if sh < 1:
            i += 1
            continue
        tr = Trade(symbol, sig.bar_time_et, d.index[j].strftime("%Y-%m-%d %H:%M"), e, stop, t1, t2, sh, sig.score)
        half = sh // 2
        rem = sh
        realized = 0.0
        cur_stop = stop
        k = j
        exit_done = False
        while k < n and d.index[k].date() == day:
            o, h, l, c = d["open"].iloc[k], d["high"].iloc[k], d["low"].iloc[k], d["close"].iloc[k]
            if l <= cur_stop:
                px = min(o, cur_stop) * (1 - slip) if k > j else cur_stop * (1 - slip)
                realized += rem * (px - e); rem = 0
                tr.exit_reason = "STOP" if not tr.hit_t1 else "BREAKEVEN STOP"
                tr.exit_time, tr.exit_price = d.index[k].strftime("%Y-%m-%d %H:%M"), px
                exit_done = True
                break
            if not tr.hit_t1 and h >= t1 and half > 0:
                realized += half * (t1 * (1 - slip) - e); rem -= half
                tr.hit_t1 = True
                cur_stop = e  # نقل الوقف للتعادل
            if h >= t2:
                px = t2 * (1 - slip)
                realized += rem * (px - e); rem = 0
                tr.exit_reason, tr.exit_time, tr.exit_price = "TARGET 2", d.index[k].strftime("%Y-%m-%d %H:%M"), px
                exit_done = True
                break
            if c < d["vwap"].iloc[k]:
                px = c * (1 - slip)
                realized += rem * (px - e); rem = 0
                tr.exit_reason, tr.exit_time, tr.exit_price = "CLOSE < VWAP", d.index[k].strftime("%Y-%m-%d %H:%M"), px
                exit_done = True
                break
            k += 1
        if not exit_done:
            k = k - 1
            px = d["close"].iloc[k] * (1 - slip)
            realized += rem * (px - e); rem = 0
            tr.exit_reason, tr.exit_time, tr.exit_price = "END OF DAY", d.index[k].strftime("%Y-%m-%d %H:%M"), px
        tr.pnl = round(realized, 2)
        tr.r_multiple = round(realized / (sh * risk), 3)
        trades.append(tr)
        i = k + 1  # صفقة واحدة في نفس الوقت لكل سهم
    return trades


def prepare_backtest_data(client: AlpacaClient, symbols: List[str], days: int = 60,
                          feed: str = "sip") -> Dict[str, pd.DataFrame]:
    end = client.now_utc() - timedelta(minutes=16)
    start = end - timedelta(days=days)
    out: Dict[str, pd.DataFrame] = {}
    spy = None
    try:
        sb = client.bars(["SPY"], "5Min", start, end, feed=feed).get("SPY")
        if sb is not None and not sb.empty:
            spy = regular_session(to_et(sb))
    except ApiError:
        spy = None
    for s in symbols:
        try:
            b = client.bars([s], "5Min", start, end, feed=feed)
        except ApiError as e:
            print(f"⚠️ {s}: {e.message}")
            continue
        df = b.get(s)
        if df is None or df.empty:
            continue
        d = regular_session(to_et(df))
        if len(d) < 100:
            continue
        out[s] = attach_relative(add_15m_trend(add_indicators(d, client.cfg)), spy)
    return out


ABLATION_VARIANTS = [   # (المفتاح، الوصف، التغيير، النوع: base/remove/add/alt)
    ("base", "الاستراتيجية الحالية (إعداداتك)", {}, "base"),
    ("no_vwap", "بدون شرط: السعر فوق VWAP", {"req_vwap": False}, "remove"),
    ("no_ema", "بدون شرط: EMA9 فوق EMA20", {"req_ema": False}, "remove"),
    ("no_rvol", "بدون شرط: RVOL ≥ 1.5", {"req_rvol": False}, "remove"),
    ("no_chase", "بدون شرط: عدم المطاردة", {"req_no_chase": False}, "remove"),
    ("score60", "حد النقاط 60 بدل 75", {"min_entry_score": 60}, "alt"),
    ("score85", "حد النقاط 85 بدل 75", {"min_entry_score": 85}, "alt"),
    ("open15", "إضافة: لا دخول أول 15 دقيقة", {"skip_open_min": 15}, "add"),
    ("rel_spy", "إضافة: السهم أقوى من SPY اليوم", {"req_rel_spy": True}, "add"),
    ("t15", "الهدف 1.5R (والثاني 2.5R)", {"target_r": 1.5}, "alt"),
    ("t2", "الهدف 2R (والثاني 3R)", {"target_r": 2.0}, "alt"),
    ("t3", "الهدف 3R (والثاني 4R)", {"target_r": 3.0}, "alt"),
]
ABLATION_UNTESTABLE = ("فلتر الفارق (Spread) وشرط الخبر الإيجابي لا يمكن اختبارهما تاريخياً مجاناً: "
                       "الاختبار لا يملك أسعار العرض/الطلب ولا الأخبار القديمة. حكمهما يأتي من «دقة الإشارات الحية» بعد تفعيلهما.")


def ablation_from_data(data: Dict[str, pd.DataFrame], cfg: Config) -> pd.DataFrame:
    """يشغّل الاختبار التاريخي نفسه بعد إزالة شرط واحد كل مرة، على البيانات نفسها.
    شرط الاختراق فوق المقاومة لا يُزال لأنه أساس خطة الدخول والوقف.
    شرط الأخبار لا يدخل لأن الاختبار التاريخي لا يحتوي أخباراً."""
    rows = []
    base_cfg = replace(cfg, req_news_catalyst=False, max_spread_stop_pct=0.0)  # غير قابلين للاختبار تاريخياً
    for key, label, over, kind in ABLATION_VARIANTS:
        c = replace(base_cfg, **over)
        trades: List[Trade] = []
        for s, d in data.items():
            trades += backtest_df(d, s, c)
        _, st = summarize_trades(trades)
        rows.append({"key": key, "kind": kind, "الحالة": label, "صفقات": st.get("trades", 0),
                     "نسبة الربح": st.get("win_rate"), "متوسط R": st.get("avg_R"),
                     "مجموع R": st.get("total_R"), "أقصى تراجع R": st.get("max_drawdown_R"),
                     "عامل الربح": st.get("profit_factor")})
    df = pd.DataFrame(rows)
    base = df.iloc[0]
    def verdict(r):
        if r["key"] == "base":
            return "المرجع"
        if r["صفقات"] == 0 or base["صفقات"] == 0:
            return "بيانات غير كافية"
        if (r["صفقات"] or 0) < 30:
            note = " (عينة صغيرة)"
        else:
            note = ""
        b, v = base["متوسط R"] or 0, r["متوسط R"] or 0
        better, worse = v > b + 0.05, v < b - 0.05
        if r["kind"] == "remove":
            if worse:
                return "الشرط مفيد — إزالته تُضعف النتيجة" + note
            if better:
                return "الشرط قد يكون مقيِّداً — إزالته حسّنت النتيجة" + note
        elif r["kind"] == "add":
            if better:
                return "الإضافة حسّنت النتيجة — تستحق التفعيل" + note
            if worse:
                return "الإضافة أضعفت النتيجة — لا تفعّلها" + note
        else:
            if better:
                return "أفضل من إعداداتك الحالية" + note
            if worse:
                return "أسوأ من إعداداتك الحالية" + note
        return "أثره صغير على هذه البيانات" + note
    df["الحكم"] = df.apply(verdict, axis=1)
    return df


def ablation(client: AlpacaClient, symbols: List[str], days: int = 60, feed: str = "sip") -> pd.DataFrame:
    return ablation_from_data(prepare_backtest_data(client, symbols, days, feed), client.cfg)


def backtest(client: AlpacaClient, symbols: List[str], days: int = 60, feed: str = "sip") -> Tuple[pd.DataFrame, dict]:
    """feed='sip': بيانات السوق الكامل التاريخية مسموحة مجاناً إذا كانت أقدم من 15 دقيقة."""
    end = client.now_utc() - timedelta(minutes=16)
    start = end - timedelta(days=days)
    all_trades: List[Trade] = []
    for s in symbols:
        try:
            b = client.bars([s], "5Min", start, end, feed=feed)
        except ApiError as e:
            print(f"⚠️ {s}: {e.message}")
            continue
        df = b.get(s)
        if df is None or df.empty:
            print(f"⚠️ {s}: لا توجد بيانات")
            continue
        d = regular_session(to_et(df))
        if len(d) < 100:
            print(f"⚠️ {s}: بيانات قليلة ({len(d)} شمعة)")
            continue
        d = add_15m_trend(add_indicators(d, client.cfg))
        all_trades += backtest_df(d, s, client.cfg)
    return summarize_trades(all_trades)


def summarize_trades(trades: List[Trade]) -> Tuple[pd.DataFrame, dict]:
    df = pd.DataFrame([asdict(t) for t in trades])
    if df.empty:
        return df, {"trades": 0, "note": "لم تتحقق أي إشارة ENTRY CONFIRMED في الفترة"}
    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    eq = df["r_multiple"].cumsum()
    dd = (eq - eq.cummax()).min()
    pf = wins["pnl"].sum() / abs(losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() != 0 else float("inf")
    stats = {
        "trades": int(len(df)),
        "win_rate": round(len(wins) / len(df), 3),
        "avg_R": round(df["r_multiple"].mean(), 3),
        "expectancy_R": round(df["r_multiple"].mean(), 3),
        "total_R": round(df["r_multiple"].sum(), 2),
        "total_pnl_usd": round(df["pnl"].sum(), 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else "∞",
        "max_drawdown_R": round(float(dd), 2),
        "exit_reasons": df["exit_reason"].value_counts().to_dict(),
        "warning": "عينة صغيرة (< 30 صفقة) لا تكفي للحكم" if len(df) < 30 else "",
    }
    return df, stats


# ======================================================================
# المراقبة الافتراضية + السجل (المرحلة 10)
# ======================================================================


class VirtualBook:
    """يسجل الإشارات ويتابع الصفقات الافتراضية. لا يرسل أي أوامر."""

    def __init__(self, folder: str = "."):
        os.makedirs(folder, exist_ok=True)
        self.sig_path = os.path.join(folder, "signals_log.csv")
        self.trd_path = os.path.join(folder, "virtual_trades.csv")
        self.open: Dict[str, dict] = {}
        self._load_open()

    def _load_open(self):
        if os.path.exists(self.trd_path):
            df = pd.read_csv(self.trd_path)
            for _, r in df[df["status"] == "OPEN"].iterrows():
                self.open[r["symbol"]] = r.to_dict()

    def log_signal(self, a: Analysis):
        s = a.signal
        row = {
            "logged_utc": iso(a.analyzed_at_utc), "symbol": a.symbol, "price": a.price.price,
            "price_time_utc": iso(a.price.trade_time_utc) if a.price.trade_time_utc else "",
            "age_sec": None if a.price.age_sec is None else round(a.price.age_sec, 1),
            "freshness": a.price.freshness, "feed": a.price.feed,
            "status": s.status if s else "NO TRADE", "score": s.score if s else "",
            "bar_et": s.bar_time_et if s else "", "entry": s.entry if s else "", "stop": s.stop if s else "",
            "t1": s.target1 if s else "", "t2": s.target2 if s else "", "rr": s.rr if s else "",
            "reasons_fail": " | ".join(s.reasons_fail) if s else " | ".join(a.notes),
        }
        new = not os.path.exists(self.sig_path)
        with open(self.sig_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)

    def _write_all(self, rows: List[dict]):
        pd.DataFrame(rows).to_csv(self.trd_path, index=False)

    def _all(self) -> List[dict]:
        return pd.read_csv(self.trd_path).to_dict("records") if os.path.exists(self.trd_path) else []

    def update(self, a: Analysis, cfg: Config) -> List[str]:
        """يفتح صفقة افتراضية عند ENTRY CONFIRMED ويحدّث المفتوحة بأسعار حديثة فقط."""
        events: List[str] = []
        s, p = a.signal, a.price
        rows = self._all()
        sym = a.symbol
        if sym in self.open:
            t = self.open[sym]
            if p.freshness != "FRESH":
                events.append(f"{sym}: السعر {p.freshness} — لم تُحدَّث الصفقة (لا نستخدم سعراً قديماً)")
            else:
                px = p.price
                reason = None
                if px <= t["stop"]:
                    reason = "STOP"
                elif px >= t["t2"]:
                    reason = "TARGET 2"
                elif s and s.vwap and s.last_close < s.vwap:
                    reason = "INVALIDATED: CLOSE < VWAP"
                elif s and s.status == "INVALIDATED":
                    reason = "INVALIDATED: FAILED BREAKOUT"
                elif a.analyzed_at_utc.astimezone(ET).strftime("%H:%M") >= "15:55":
                    reason = "END OF DAY"
                if not t.get("hit_t1") and px >= t["t1"]:
                    t["hit_t1"] = True
                    t["stop"] = t["entry"]
                    events.append(f"{sym}: وصل T1 {fmt_price(t['t1'])} — الوقف إلى التعادل")
                if reason:
                    risk = t["entry"] - t["orig_stop"]
                    r_mult = (px - t["entry"]) / risk if risk > 0 else 0
                    if t.get("hit_t1"):
                        r_mult = 0.5 * 2 + 0.5 * r_mult
                    t.update(status="CLOSED", exit_price=px, exit_utc=iso(p.trade_time_utc), exit_reason=reason,
                             r_multiple=round(r_mult, 3), pnl=round(r_mult * risk * t["shares"], 2))
                    events.append(f"{sym}: إغلاق افتراضي ({reason}) عند {fmt_price(px)} = {r_mult:.2f}R")
                    del self.open[sym]
                rows = [r for r in rows if not (r["symbol"] == sym and r["status"] == "OPEN")] + [t]
                self._write_all(rows)
        elif s and s.status == "ENTRY CONFIRMED" and p.freshness == "FRESH":
            t = {"symbol": sym, "opened_utc": iso(p.trade_time_utc), "entry": p.price, "stop": s.stop,
                 "orig_stop": s.stop, "t1": s.target1, "t2": s.target2, "shares": s.shares, "score": s.score,
                 "status": "OPEN", "hit_t1": False, "exit_price": "", "exit_utc": "", "exit_reason": "",
                 "r_multiple": "", "pnl": ""}
            self.open[sym] = t
            self._write_all(rows + [t])
            events.append(f"{sym}: فتح صفقة افتراضية عند {fmt_price(p.price)} (ليست أمراً حقيقياً)")
        return events

    def performance(self) -> Tuple[pd.DataFrame, dict]:
        rows = self._all()
        df = pd.DataFrame(rows)
        if df.empty or (df["status"] == "CLOSED").sum() == 0:
            return df, {"closed_trades": 0}
        c = df[df["status"] == "CLOSED"].copy()
        c["r_multiple"] = c["r_multiple"].astype(float)
        return df, {"closed_trades": int(len(c)), "win_rate": round((c["r_multiple"] > 0).mean(), 3),
                    "avg_R": round(c["r_multiple"].mean(), 3), "total_R": round(c["r_multiple"].sum(), 2),
                    "open_trades": int((df["status"] == "OPEN").sum())}


def monitor(client: AlpacaClient, symbols: List[str], book: VirtualBook, minutes: int = 60,
            interval_sec: int = 60, display_fn=None):
    """حلقة مراقبة: كل interval ثانية — تحليل، عرض، تسجيل، تحديث الصفقات الافتراضية."""
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            analyses, mo = scan(client, symbols, with_news=False)
            events = []
            for a in analyses:
                book.log_signal(a)
                events += book.update(a, client.cfg)
            if display_fn:
                display_fn(analyses, mo, events)
            if not mo:
                print("السوق مغلق — إيقاف المراقبة.")
                return
        except Exception as e:  # لا تتوقف الحلقة بسبب خطأ شبكة مؤقت
            print(f"⚠️ خطأ مؤقت ({type(e).__name__}): {e} — إعادة المحاولة بعد {interval_sec}s")
        time.sleep(interval_sec)


# ======================================================================
# اختبار WebSocket (اختياري)
# ======================================================================


def stream_test(key: str, secret: str, symbols: List[str], seconds: int = 60) -> pd.DataFrame:
    """يتصل بتيار IEX ويقيس التأخير = وقت الاستلام − وقت الصفقة. الخطة المجانية: اتصال واحد، 30 رمزاً."""
    import websocket  # pip install websocket-client
    rows: List[dict] = []
    ws = websocket.create_connection(STREAM_IEX, timeout=10)
    try:
        ws.recv()
        ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
        auth = json.loads(ws.recv())
        if not any(m.get("msg") == "authenticated" for m in auth):
            raise RuntimeError(f"فشل مصادقة WebSocket: {auth}")
        ws.send(json.dumps({"action": "subscribe", "trades": symbols, "quotes": symbols}))
        ws.recv()
        end = time.time() + seconds
        ws.settimeout(2)
        while time.time() < end:
            try:
                msgs = json.loads(ws.recv())
            except Exception:
                continue
            recv = datetime.now(UTC)
            for m in msgs:
                if m.get("T") in ("t", "q"):
                    ts = parse_ts(m["t"])
                    rows.append({"type": "trade" if m["T"] == "t" else "quote", "symbol": m["S"],
                                 "price": m.get("p") or m.get("bp"), "event_utc": ts,
                                 "latency_ms": round((recv - ts).total_seconds() * 1000, 1)})
    finally:
        ws.close()
    return pd.DataFrame(rows)


# ======================================================================
# متتبع صفقاتك الحقيقية (إدخال يدوي — النظام لا يتصل بحساب وسيطك)
# ======================================================================

DEAL_COLS = ["id", "time_utc", "symbol", "side", "shares", "price", "fees", "stop", "target", "note"]


class DealsTracker:
    """سجل عمليات الشراء والبيع التي تنفذها بنفسك عند وسيطك.
    الحساب بطريقة FIFO: أول أسهم اشتريتها هي أول ما يُباع."""

    def __init__(self, folder: str = "."):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "my_deals.csv")
        if os.path.exists(self.path):
            self.df = pd.read_csv(self.path, dtype={"symbol": str, "side": str, "note": str})
            self.df["note"] = self.df["note"].fillna("")
        else:
            self.df = pd.DataFrame(columns=DEAL_COLS)

    def reload(self):
        """إعادة القراءة من الملف (إذا أضاف جزء آخر من التطبيق عملية جديدة)."""
        if os.path.exists(self.path):
            self.df = pd.read_csv(self.path, dtype={"symbol": str, "side": str, "note": str})
            self.df["note"] = self.df["note"].fillna("")

    def _save(self):
        tmp = self.path + ".tmp"
        self.df.to_csv(tmp, index=False)
        os.replace(tmp, self.path)  # كتابة آمنة: لا يتلف الملف إذا انقطعت الجلسة أثناء الحفظ

    def held(self, symbol: str) -> float:
        d = self.df[self.df["symbol"] == symbol]
        return float(d.loc[d["side"] == "BUY", "shares"].sum() - d.loc[d["side"] == "SELL", "shares"].sum())

    def add(self, symbol: str, side: str, shares: float, price: float, when_utc: Optional[datetime] = None,
            fees: float = 0.0, stop: Optional[float] = None, target: Optional[float] = None, note: str = "") -> dict:
        symbol = symbol.strip().upper()
        side = side.strip().upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("النوع يجب أن يكون BUY أو SELL")
        if not symbol:
            raise ValueError("أدخل رمز السهم")
        if not shares or shares <= 0:
            raise ValueError("عدد الأسهم يجب أن يكون أكبر من صفر")
        if not price or price <= 0:
            raise ValueError("السعر يجب أن يكون أكبر من صفر")
        if fees is None or fees < 0:
            raise ValueError("الرسوم لا يمكن أن تكون سالبة")
        if side == "SELL":
            h = self.held(symbol)
            if shares > h + 1e-9:
                raise ValueError(f"لا يمكن بيع {shares:g} سهم: رصيدك المسجل من {symbol} هو {h:g} فقط")
        if side == "BUY" and stop is not None and stop >= price:
            raise ValueError("وقف الخسارة يجب أن يكون أقل من سعر الشراء")
        if side == "BUY" and target is not None and target <= price:
            raise ValueError("الهدف يجب أن يكون أعلى من سعر الشراء")
        when_utc = (when_utc or datetime.now(UTC)).astimezone(UTC)
        nid = int(self.df["id"].max()) + 1 if len(self.df) else 1
        row = {"id": nid, "time_utc": iso(when_utc), "symbol": symbol, "side": side, "shares": float(shares),
               "price": float(price), "fees": float(fees or 0), "stop": stop, "target": target, "note": note or ""}
        self.df = pd.concat([self.df, pd.DataFrame([row])], ignore_index=True) if len(self.df) else pd.DataFrame([row])
        self._save()
        return row

    def delete(self, deal_id: int) -> bool:
        d = self.df[self.df["id"] != deal_id]
        if len(d) == len(self.df):
            return False
        # تحقق أن الحذف لا يجعل أي بيع أكبر من الرصيد
        trial = DealsTracker.__new__(DealsTracker)
        trial.df = d
        try:
            trial._fifo()
        except ValueError as e:
            raise ValueError("لا يمكن حذف هذه العملية لأنها تسبق عملية بيع تعتمد عليها: " + str(e))
        self.df = d.reset_index(drop=True)
        self._save()
        return True

    def _fifo(self):
        """يعيد (lots المفتوحة لكل سهم، الصفقات المغلقة)."""
        lots: Dict[str, List[dict]] = {}
        closed: List[dict] = []
        d = self.df.copy()
        d["_t"] = pd.to_datetime(d["time_utc"], utc=True)
        d = d.sort_values(["_t", "id"])
        for _, r in d.iterrows():
            sym, sh, px, fee = r["symbol"], float(r["shares"]), float(r["price"]), float(r["fees"] or 0)
            if r["side"] == "BUY":
                lots.setdefault(sym, []).append({"shares": sh, "price": px, "fee_per_share": fee / sh,
                                                 "time_utc": r["time_utc"], "stop": r.get("stop"),
                                                 "target": r.get("target"), "buy_id": r["id"]})
            else:
                remaining = sh
                sell_fee_ps = fee / sh
                q = lots.get(sym, [])
                while remaining > 1e-9:
                    if not q:
                        raise ValueError(f"بيع {sym} بتاريخ {r['time_utc']} أكبر من الرصيد المسجل قبله")
                    lot = q[0]
                    take = min(lot["shares"], remaining)
                    cost = take * (lot["price"] + lot["fee_per_share"])
                    proceeds = take * (px - sell_fee_ps)
                    risk_ps = None
                    try:
                        if lot["stop"] is not None and not pd.isna(lot["stop"]):
                            risk_ps = lot["price"] - float(lot["stop"])
                    except Exception:
                        risk_ps = None
                    closed.append({"symbol": sym, "shares": take, "buy_time_utc": lot["time_utc"],
                                   "buy_price": lot["price"], "sell_time_utc": r["time_utc"], "sell_price": px,
                                   "pnl": round(proceeds - cost, 2),
                                   "pnl_pct": round((proceeds - cost) / cost * 100, 2) if cost else 0.0,
                                   "r_multiple": round((px - lot["price"]) / risk_ps, 2) if risk_ps and risk_ps > 0 else None,
                                   "buy_id": lot["buy_id"], "sell_id": r["id"]})
                    lot["shares"] -= take
                    remaining -= take
                    if lot["shares"] <= 1e-9:
                        q.pop(0)
        return {k: v for k, v in lots.items() if v}, closed

    def positions(self) -> pd.DataFrame:
        lots, _ = self._fifo()
        rows = []
        for sym, q in lots.items():
            sh = sum(l["shares"] for l in q)
            cost = sum(l["shares"] * (l["price"] + l["fee_per_share"]) for l in q)
            last = q[-1]
            rows.append({"symbol": sym, "shares": sh, "avg_cost": cost / sh, "cost_basis": cost,
                         "stop": last["stop"] if last["stop"] is not None and not pd.isna(last["stop"]) else None,
                         "target": last["target"] if last["target"] is not None and not pd.isna(last["target"]) else None,
                         "opened_utc": q[0]["time_utc"]})
        return pd.DataFrame(rows)

    def closed_trades(self) -> pd.DataFrame:
        _, c = self._fifo()
        return pd.DataFrame(c)

    def live(self, client: "AlpacaClient", market_open: bool) -> Tuple[pd.DataFrame, List[str]]:
        """قيمة المراكز المفتوحة بآخر سعر — مع وقت السعر وحالته. لا تُحسب أرباح بسعر غير متاح."""
        pos = self.positions()
        alerts: List[str] = []
        if pos.empty:
            return pos, alerts
        out = []
        for _, p in pos.iterrows():
            pc = check_price(client, p["symbol"], market_open)
            r = p.to_dict()
            r.update(last_price=pc.price, price_time=_t(pc.trade_time_utc), freshness=pc.freshness, feed=pc.feed.upper())
            if pc.price is not None:
                r["market_value"] = pc.price * p["shares"]
                r["unrealized_pnl"] = r["market_value"] - p["cost_basis"]
                r["unrealized_pct"] = r["unrealized_pnl"] / p["cost_basis"] * 100
                tag = "" if pc.freshness == "FRESH" else f" (السعر {pc.freshness} — تحقق من وسيطك)"
                if r["stop"] and pc.price <= r["stop"]:
                    alerts.append(f"🔴 {p['symbol']}: السعر {fmt_price(pc.price)} وصل/تجاوز وقفك {fmt_price(r['stop'])}{tag}")
                if r["target"] and pc.price >= r["target"]:
                    alerts.append(f"🟢 {p['symbol']}: السعر {fmt_price(pc.price)} وصل هدفك {fmt_price(r['target'])}{tag}")
            else:
                r["market_value"] = r["unrealized_pnl"] = r["unrealized_pct"] = None
            out.append(r)
        return pd.DataFrame(out), alerts

    def summary(self) -> dict:
        c = self.closed_trades()
        if c.empty:
            return {"closed_lots": 0, "realized_pnl": 0.0}
        wins = c[c["pnl"] > 0]
        losses = c[c["pnl"] <= 0]
        return {"closed_lots": int(len(c)), "realized_pnl": round(float(c["pnl"].sum()), 2),
                "win_rate": round(len(wins) / len(c), 3),
                "avg_win": round(float(wins["pnl"].mean()), 2) if len(wins) else 0.0,
                "avg_loss": round(float(losses["pnl"].mean()), 2) if len(losses) else 0.0,
                "best": round(float(c["pnl"].max()), 2), "worst": round(float(c["pnl"].min()), 2)}


def tracker_html(live_df: pd.DataFrame, alerts: List[str], tracker: DealsTracker,
                 news_map: Optional[Dict[str, Optional[List[dict]]]] = None) -> str:
    s = tracker.summary()
    parts = ["<div dir='rtl' style='font-family:-apple-system,sans-serif;font-size:14px'>",
             "<h3 style='margin:4px 0'>📒 صفقاتي</h3>"]
    for a in alerts:
        parts.append(f"<div style='padding:6px;margin:3px 0;background:#fff3cd;border-radius:6px'>{a}</div>")
    if live_df is None or live_df.empty:
        parts.append("<p>لا توجد مراكز مفتوحة.</p>")
    else:
        th = ["Symbol", "Shares", "Avg Cost", "Last Price", "Price Time", "Freshness", "Unrealized $", "Unrealized %", "Stop", "Target"]
        rows = []
        for _, r in live_df.iterrows():
            pnl = r.get("unrealized_pnl")
            color = "#1b7f3b" if pnl and pnl > 0 else "#b3261e" if pnl and pnl < 0 else "#000"
            vals = [r["symbol"], f"{r['shares']:g}", fmt_price(r["avg_cost"]), fmt_price(r["last_price"]), r["price_time"],
                    f"<span style='color:{FRESH_COLORS.get(r['freshness'], '#000')};font-weight:600'>{r['freshness']}</span>",
                    f"<span style='color:{color};font-weight:600'>{'—' if pnl is None else f'{pnl:,.2f}'}</span>",
                    "—" if r.get("unrealized_pct") is None else f"{r['unrealized_pct']:.2f}%",
                    fmt_price(r["stop"]), fmt_price(r["target"])]
            rows.append("<tr>" + "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #ddd;white-space:nowrap'>{v}</td>" for v in vals) + "</tr>")
        parts.append("<div style='overflow-x:auto'><table style='border-collapse:collapse;font-size:13px'><tr>" +
                     "".join(f"<th style='padding:4px 8px;background:#222;color:#fff;white-space:nowrap'>{h}</th>" for h in th) +
                     "</tr>" + "".join(rows) + "</table></div>")
        tot = live_df["unrealized_pnl"].dropna().sum() if "unrealized_pnl" in live_df else 0
        parts.append(f"<p>إجمالي الربح/الخسارة غير المحققة: <b>{tot:,.2f}$</b> "
                     f"<span style='color:#888;font-size:12px'>(بأسعار IEX — قد تختلف قليلاً عن سعر وسيطك)</span></p>")
    if news_map and live_df is not None and not live_df.empty:
        for sym in live_df["symbol"]:
            if sym in news_map:
                parts.append(f"<div style='margin-top:4px'><b>{sym}</b></div>" + news_html(news_map[sym], 3))
    parts.append(f"<p>المحقق: <b>{s.get('realized_pnl', 0):,.2f}$</b> | دفعات مغلقة: {s.get('closed_lots', 0)}"
                 + (f" | نسبة الربح: {s['win_rate']:.0%} | متوسط الربح {s['avg_win']}$ | متوسط الخسارة {s['avg_loss']}$" if s.get("closed_lots") else "")
                 + "</p></div>")
    return "".join(parts)


# ======================================================================
# جلسة كاملة تتبع ساعة السوق
# ======================================================================

IEX_OPEN_ET = "08:00"   # بيانات IEX المجانية تبدأ 08:00 ET
REG_OPEN_ET = "09:30"
REG_CLOSE_ET = "16:00"


def session_phase(now_utc: datetime, is_trading_day: bool, open_et: str = REG_OPEN_ET, close_et: str = REG_CLOSE_ET) -> str:
    """WAIT (قبل بيانات IEX) / PRE (08:00–09:30 ET) / REGULAR / DONE / NO_SESSION"""
    if not is_trading_day:
        return "NO_SESSION"
    t = now_utc.astimezone(ET).strftime("%H:%M")
    if t < IEX_OPEN_ET:
        return "WAIT"
    if t < open_et:
        return "PRE"
    if t < close_et:
        return "REGULAR"
    return "DONE"


def todays_session(client: "AlpacaClient") -> Tuple[bool, str, str]:
    """هل اليوم يوم تداول؟ وما وقت الافتتاح والإغلاق (يدعم أيام الإغلاق المبكر)."""
    today = client.now_utc().astimezone(ET).strftime("%Y-%m-%d")
    cal = client.calendar(today, today)
    if not cal or cal[0].get("date") != today:
        return False, REG_OPEN_ET, REG_CLOSE_ET
    return True, cal[0].get("open", REG_OPEN_ET), cal[0].get("close", REG_CLOSE_ET)


def run_session(client: "AlpacaClient", watchlist: List[str], book: "VirtualBook", tracker: DealsTracker,
                display_fn=None, interval_sec: int = 60, sleep_fn=time.sleep, max_loops: Optional[int] = None,
                news_watcher: Optional["NewsWatcher"] = None, stop_event: Optional[threading.Event] = None,
                scanner: Optional["MarketScanner"] = None, cache: Optional["BarCache"] = None):
    """يعمل من بداية بيانات IEX حتى إغلاق السوق ثم يتوقف تلقائياً.
    - قبل 08:00 ET: ينتظر ويعرض العد التنازلي.
    - 08:00–09:30 ET (ما قبل الافتتاح): يحدّث صفقاتك فقط، بدون إشارات.
    - 09:30–16:00 ET: تحليل + إشارات افتراضية + صفقاتك + تنبيهات الوقف/الهدف.
    إذا انقطع Colab: أعد تشغيل الخلية — السجلات محفوظة في Drive وتكمل من حيث توقفت."""
    loops = 0
    try:
        trading_day, open_et, close_et = todays_session(client)
    except ApiError as e:
        print("❌ تعذر جلب تقويم السوق:", e.message)
        return
    if not trading_day:
        print("اليوم ليس يوم تداول في السوق الأمريكي (عطلة أو نهاية أسبوع).")
        return
    while max_loops is None or loops < max_loops:
        if stop_event is not None and stop_event.is_set():
            return
        loops += 1
        now = client.now_utc()
        phase = session_phase(now, trading_day, open_et, close_et)
        info = {"phase": phase, "now": _t(now), "open_et": open_et, "close_et": close_et}
        try:
            if phase == "DONE":
                tracker.reload()
                lv, al = tracker.live(client, False)
                if display_fn:
                    display_fn(info, [], False, lv, al, ["انتهت جلسة اليوم — توقف تلقائي."])
                return
            if phase == "WAIT":
                start = now.astimezone(ET).replace(hour=8, minute=0, second=0, microsecond=0)
                mins = (start - now.astimezone(ET)).total_seconds() / 60
                if display_fn:
                    display_fn(info, [], False, None, [], [f"بانتظار بدء بيانات IEX بعد {mins:.0f} دقيقة "
                                                            f"({start.astimezone(RIYADH):%H:%M} بتوقيت الرياض)."])
                sleep_fn(min(300, max(30, mins * 60)))
                continue
            market_open = phase == "REGULAR"
            analyses, events = [], []
            news_map = None
            held = list(tracker.positions()["symbol"]) if len(tracker.df) else []
            symbols = list(dict.fromkeys(list(watchlist)))
            if scanner is not None and scanner.enabled:
                events += scanner.maybe_sweep(client, now)
                symbols = list(dict.fromkeys(symbols + scanner.candidate_symbols()))
            info["scanner"] = scanner.summary() if scanner is not None else None
            info["market"] = None
            if news_watcher is not None:
                events += news_watcher.poll(symbols + held)
                news_map = {s: news_watcher.cache.get(s) for s in set(symbols) | set(held)}
            if market_open and symbols:
                analyses, _ = scan(client, symbols, with_news=False, news_map=news_map, cache=cache)
                info["market"] = dict(LAST_MOOD) if client.cfg.market_filter else None
                for a in analyses:
                    book.log_signal(a)
                    events += book.update(a, client.cfg)
            else:
                events.append("ما قبل الافتتاح: لا إشارات (الاستراتيجية للجلسة النظامية فقط). تحديث صفقاتك فقط.")
            tracker.reload()
            lv, al = tracker.live(client, True)  # أسعار IEX متاحة منذ 08:00 ET
            info["news_map"] = news_map
            if display_fn:
                display_fn(info, analyses, market_open, lv, al, events)
        except Exception as e:
            print(f"⚠️ خطأ مؤقت ({type(e).__name__}): {e} — إعادة المحاولة بعد {interval_sec}s")
        sleep_fn(interval_sec)


# ======================================================================
# الأخبار: عناوين + تصنيف بالكلمات المفتاحية + تنبيهات
# (تصنيف شفاف بالكلمات، وليس تخميناً لمشاعر الخبر — الحكم النهائي لك بعد القراءة)
# ======================================================================
import re as _re

TAG_LABELS = {
    "OFFERING": "⚠️ طرح أسهم/تمويل (Offering)",
    "REVERSE_SPLIT": "⚠️ Reverse Split",
    "DELISTING": "⚠️ إشعار إدراج/امتثال",
    "LEGAL": "⚖️ قضية/تحقيق",
    "HALT": "⛔ إيقاف تداول",
    "EARNINGS": "📊 نتائج/إيرادات",
    "FDA": "🧪 FDA/تجارب سريرية",
    "MNA": "🤝 استحواذ/اندماج",
    "ANALYST": "🎯 تقييم محللين",
    "CONTRACT": "📝 عقد/شراكة",
}
RISK_TAGS = {"OFFERING", "REVERSE_SPLIT", "DELISTING", "LEGAL"}
TAG_PATTERNS = {
    "OFFERING": r"\b(public offering|registered direct|private placement|at[- ]the[- ]market|atm (program|offering)|"
                r"shelf registration|offering of|priced? .{0,40}offering|warrants?|dilut\w*|equity line|convertible notes?)\b",
    "REVERSE_SPLIT": r"\b(reverse (stock )?split|share consolidation)\b",
    "DELISTING": r"\b(delist\w*|deficiency|non-?compliance|minimum bid price|continued listing)\b",
    "LEGAL": r"\b(lawsuit|class action|investigation|subpoena|sec charges|fraud)\b",
    "HALT": r"\b(trading halt\w*|halted|volatility trading pause)\b",
    "EARNINGS": r"\b(earnings|quarterly results|q[1-4] (results|revenue)|revenue|eps|guidance|fiscal (year|quarter))\b",
    "FDA": r"\b(fda|clinical trial|phase [123i]+|approval)\b",
    "MNA": r"\b(acquir\w*|acquisition|merger|buyout|takeover)\b",
    "ANALYST": r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiates coverage)\b",
    "CONTRACT": r"\b(contract|partnership|agreement|award(ed)?)\b",
}
_TAG_RX = {k: _re.compile(v, _re.I) for k, v in TAG_PATTERNS.items()}


def classify_news(item: dict) -> List[str]:
    text = f"{item.get('headline', '')} {item.get('summary', '')}"
    tags = [k for k, rx in _TAG_RX.items() if rx.search(text)]
    if "DELISTING" in tags and _re.search(r"\bregain(s|ed)? compliance\b", text, _re.I):
        tags.remove("DELISTING")  # استعادة الامتثال ليست خبر خطر
    return tags


def normalize_news(raw: Optional[list]) -> Optional[List[dict]]:
    if raw is None:
        return None
    out = []
    for n in raw:
        try:
            t = parse_ts(n.get("created_at") or n.get("updated_at"))
        except Exception:
            t = None
        item = {"id": n.get("id"), "headline": (n.get("headline") or "").strip(), "summary": n.get("summary") or "",
                "source": n.get("source") or "", "url": n.get("url") or "", "symbols": n.get("symbols") or [],
                "time_utc": t}
        item["tags"] = classify_news(item)
        out.append(item)
    out.sort(key=lambda x: x["time_utc"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out


def news_for_symbol(raw: Optional[list], symbol: str) -> Optional[List[dict]]:
    """None يبقى None (غير متاح). القائمة الفارغة = لا أخبار فعلاً."""
    items = normalize_news(raw)
    if items is None:
        return None
    return [n for n in items if symbol in n["symbols"]]


def news_html(items: Optional[List[dict]], limit: int = 5) -> str:
    if items is None:
        return "<div dir='rtl' style='color:#8b96a5;font-size:14px'>📰 الأخبار غير متاحة الآن.</div>"
    if not items:
        return "<div dir='rtl' style='color:#8b96a5;font-size:14px'>📰 لا أخبار خلال آخر 24 ساعة.</div>"
    li = []
    for n in items[:limit]:
        tags = " ".join(f"<span style='display:inline-block;background:{'#3a1412' if t in RISK_TAGS else '#1a2636'};color:{'#fecaca' if t in RISK_TAGS else '#bfdbfe'};"
                        f"border-radius:999px;padding:2px 8px;font-size:11px;font-weight:600'>{TAG_LABELS[t]}</span>" for t in n["tags"])
        when = n["time_utc"].astimezone(RIYADH).strftime("%m-%d %H:%M") + " الرياض" if n["time_utc"] else "—"
        head = n["headline"].replace("<", "&lt;")
        link = (f"<a href='{n['url']}' target='_blank' style='color:#cfe3ff;text-decoration:none'>{head}</a>"
                if n["url"].startswith("http") else head)
        li.append(f"<div style='background:#0f141b;border:1px solid #1f2731;border-radius:12px;padding:10px 12px;margin-bottom:6px'>"
                  f"<div dir='ltr' style='text-align:left;font-size:14px;line-height:1.5'>{link}</div>"
                  f"<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px'>"
                  f"<span style='color:#8b96a5;font-size:12px'>{when} • {n['source']}</span>{tags}</div></div>")
    return "<div dir='rtl'><div style='font-weight:700;margin-bottom:8px'>📰 آخر الأخبار</div>" + "".join(li) + "</div>"


class NewsWatcher:
    """يفحص الأخبار كل few دقائق بطلب واحد لكل الرموز، ويتذكر ما رآه (محفوظ) حتى لا يكرر التنبيه."""

    def __init__(self, client: "AlpacaClient", folder: str = ".", every_sec: int = 300):
        self.client = client
        self.every = every_sec
        self.last_poll = float("-inf")
        self.cache: Dict[str, Optional[List[dict]]] = {}
        self.path = os.path.join(folder, "news_seen.json")
        try:
            self.seen = set(json.load(open(self.path)))
        except Exception:
            self.seen = set()
        self.first = not self.seen  # أول تشغيل: لا تنبّه على أخبار قديمة

    def poll(self, symbols: List[str], force: bool = False) -> List[str]:
        alerts: List[str] = []
        symbols = sorted(set(symbols))
        if not symbols or (not force and time.monotonic() - self.last_poll < self.every):
            return alerts
        self.last_poll = time.monotonic()
        raw = self.client.news(symbols, self.client.cfg.news_lookback_hours)
        for s in symbols:
            self.cache[s] = news_for_symbol(raw, s)
        if raw is None:
            return ["📰 تعذر جلب الأخبار في هذه الدورة (لا يعني عدم وجود أخبار)."]
        for n in normalize_news(raw):
            if n["id"] in self.seen:
                continue
            self.seen.add(n["id"])
            if self.first:
                continue
            syms = [s for s in n["symbols"] if s in symbols]
            tags = " ".join(TAG_LABELS[t] for t in n["tags"])
            alerts.append(f"📰 خبر جديد ({', '.join(syms)}) {_t(n['time_utc']) if n['time_utc'] else ''}: "
                          f"{n['headline']} {tags}")
        self.first = False
        try:
            json.dump(sorted(x for x in self.seen if x is not None)[-5000:], open(self.path, "w"))
        except Exception:
            pass
        return alerts


# ======================================================================
# بطاقة تحليل سهم (HTML)
# ======================================================================
def analysis_card_html(a: "Analysis") -> str:
    p, s = a.price, a.signal
    st = s.status if s else "NO TRADE"
    def row(k, v):
        return f"<tr><td style='color:#666;padding:3px 8px'>{k}</td><td style='padding:3px 8px;font-weight:600'>{v}</td></tr>"
    rows = [
        row("السعر", fmt_price(p.price)),
        row("وقت آخر صفقة", _t(p.trade_time_utc)),
        row("عمر السعر", "—" if p.age_sec is None else f"{p.age_sec:.0f} ثانية"),
        row("حالة البيانات", f"<span style='color:{FRESH_COLORS.get(p.freshness,'#000')}'>{p.freshness}</span>"),
        row("المصدر", p.feed.upper() + " (بورصة واحدة، ليست السوق كاملاً)"),
        row("Bid / Ask", f"{fmt_price(p.bid)} / {fmt_price(p.ask)}" if p.bid else "—"),
    ]
    if s:
        rows += [
            row("VWAP", fmt_price(s.vwap)),
            row("EMA 9 / EMA 20", f"{fmt_price(s.ema9)} / {fmt_price(s.ema20)}"),
            row("RSI", "—" if math.isnan(s.rsi) else f"{s.rsi:.1f}"),
            row("RVOL", "—" if math.isnan(s.rvol) else f"{s.rvol:.2f}"),
            row("دعم / مقاومة", f"{fmt_price(s.support)} / {fmt_price(s.resistance)}"),
            row("Technical Score", f"{s.score}/100"),
        ]
        if s.entry:
            rows += [
                row("Entry", fmt_price(s.entry)), row("Stop Loss", fmt_price(s.stop)),
                row("Target 1 / 2", f"{fmt_price(s.target1)} / {fmt_price(s.target2)}"),
                row("Risk/Reward", f"1:{s.rr}"), row("عدد الأسهم", s.shares), row("أقصى خسارة", f"${s.max_loss}"),
            ]
    badge = (f"<div style='display:inline-block;padding:6px 12px;border-radius:8px;color:#fff;"
             f"background:{STATUS_COLORS.get(st,'#444')};font-weight:700'>{st}</div>")
    reasons = ""
    if s:
        reasons += "".join(f"<li>✅ {x}</li>" for x in s.reasons_pass)
        reasons += "".join(f"<li>❌ {x}</li>" for x in s.reasons_fail)
        if s.invalidation:
            reasons += "<li><b>شروط الإلغاء:</b> " + " • ".join(s.invalidation) + "</li>"
    notes = "".join(f"<li>⚠️ {x}</li>" for x in ([p.note] if p.note else []) + a.notes)
    return (f"<div dir='rtl' style='font-family:-apple-system,sans-serif;font-size:15px'>"
            f"<h3 style='margin:6px 0'>{a.symbol}</h3>{badge}"
            f"<table style='border-collapse:collapse;margin-top:8px'>{''.join(rows)}</table>"
            f"<ul style='padding-right:18px'>{notes}{reasons}</ul>{news_html(a.news, 6)}"
            f"<div style='font-size:12px;color:#888'>Score يقيس توافق الشروط الفنية وليس احتمال الربح. كل الخطط افتراضية.</div></div>")


# ======================================================================
# واجهة مبسطة: حالة بكلمات عادية + 6 شروط ثابتة + الخطوة التالية
# ======================================================================
STATUS_AR = {"ENTRY CONFIRMED": "جاهز", "STRONG WATCH": "قريب", "WATCH": "راقب", "NO TRADE": "لا فرصة", "INVALIDATED": "ملغاة"}
STATUS_AR_LONG = {"ENTRY CONFIRMED": "جاهز للدخول", "STRONG WATCH": "قريب — راقب", "WATCH": "راقب",
                  "NO TRADE": "لا فرصة", "INVALIDATED": "ملغاة"}
STATUS_GROUP = {"ENTRY CONFIRMED": "ready", "STRONG WATCH": "watch", "WATCH": "watch", "NO TRADE": "none", "INVALIDATED": "none"}
CHECK_NAMES = ["السعر فوق VWAP", "EMA9 فوق EMA20", "حجم أعلى من المعتاد (RVOL ≥ 1.5)",
               "اختراق مقاومة وإغلاق شمعة فوقها", "ليس مطاردة (السعر غير ممتد)", "لا أخبار مخاطرة"]


def checks(a: "Analysis") -> List[Tuple[str, bool]]:
    s = a.signal
    if s is None:
        return [(n, False) for n in CHECK_NAMES]
    rv = s.rvol
    ok = [
        s.last_close > s.vwap,
        s.ema9 > s.ema20,
        (not math.isnan(rv)) and rv >= 1.5,
        s.breakout_level is not None and s.last_close > s.breakout_level,
        not s.chasing,
        not s.news_risk,
    ]
    return list(zip(CHECK_NAMES, [bool(x) for x in ok]))


def next_step(a: "Analysis") -> str:
    """جملة واحدة: ماذا تنتظر؟ (تظهر عندما لا توجد خطة دخول)."""
    s, p = a.signal, a.price
    if s is None:
        return "لا توجد بيانات كافية لتحليل هذا السهم."
    if not a.market_open:
        return "السوق مغلق: هذا تحليل آخر جلسة وليس لحظياً. لا إشارة دخول قبل الافتتاح."
    if p.freshness != "FRESH":
        return f"السعر {p.freshness}: آخر صفقة على IEX قديمة، فلا يُعتمد عليه الآن. انتظر تحديثاً حديثاً."
    if s.status == "INVALIDATED":
        return f"اخترق {fmt_price(s.breakout_level)} ثم أغلق تحته (اختراق فاشل). تجاهله حتى يتكوّن إعداد جديد."
    if s.news_risk:
        return "يوجد خبر مخاطرة (مثل طرح أسهم). اقرأ الخبر قبل أي قرار."
    if s.last_close <= s.vwap:
        return f"السعر تحت VWAP ({fmt_price(s.vwap)}). لا فرصة شراء حسب الاستراتيجية حتى يعود فوقه."
    if s.chasing:
        lvl = s.breakout_level or s.vwap
        return f"السعر ممتد كثيراً، والدخول الآن مطاردة. انتظر رجوعه نحو {fmt_price(lvl)} ثم ارتداده."
    if s.breakout_level is None and s.resistance:
        return f"لم يخترق المقاومة بعد. راقب إغلاق شمعة 5 دقائق فوق {fmt_price(s.resistance)} بحجم أعلى من المعتاد."
    if math.isnan(s.rvol) or s.rvol < 1.5:
        return "الحجم أقل من المعتاد. الاختراق بدون حجم ضعيف — انتظر زيادة الحجم."
    if s.entry and s.room_rr is not None and s.room_rr < 2:
        return f"توجد مقاومة قريبة ({s.room_rr}R فقط حتى المقاومة التالية). العائد لا يستحق المخاطرة."
    return "الشروط غير مكتملة بعد. تابع السهم."


def journal_stats(tracker: "DealsTracker") -> dict:
    c = tracker.closed_trades()
    if c.empty:
        return {"n": 0, "realized": 0.0, "total_r": None, "win_rate": None, "pf": None, "with_r": 0}
    wins, losses = c[c["pnl"] > 0], c[c["pnl"] <= 0]
    r = c["r_multiple"].dropna()
    gl = -losses["pnl"].sum()
    return {"n": int(len(c)), "realized": round(float(c["pnl"].sum()), 2),
            "total_r": round(float(r.sum()), 2) if len(r) else None, "with_r": int(len(r)),
            "win_rate": round(len(wins) / len(c), 3), "pf": round(float(wins["pnl"].sum() / gl), 2) if gl > 0 else None}


def daily_pnl(tracker: "DealsTracker") -> Dict[str, float]:
    """الربح المحقق لكل يوم تداول (بتاريخ نيويورك) — لتقويم الشهر."""
    c = tracker.closed_trades()
    out: Dict[str, float] = {}
    for _, r in c.iterrows():
        d = pd.Timestamp(r["sell_time_utc"]).tz_convert(ET).strftime("%Y-%m-%d")
        out[d] = out.get(d, 0.0) + float(r["pnl"])
    return out


def backtest_verdict(stats: dict, days: int) -> str:
    n = stats.get("trades", 0)
    if not n:
        return f"على {days} يوماً لم تظهر أي إشارة دخول مؤكدة. الاستراتيجية صارمة؛ جرّب أسهماً أكثر نشاطاً أو مدة أطول."
    tr = stats["total_R"]
    word = "ربحت" if tr > 0 else "خسرت" if tr < 0 else "تعادلت"
    tail = " لكن العينة أقل من 30 صفقة — لا تكفي للحكم." if n < 30 else " والعينة كافية مبدئياً."
    return f"على {days} يوماً: {n} صفقة، الاستراتيجية {word} ({tr:+.1f}R)،{tail}"


# ======================================================================
# تحسين استهلاك الطلبات: أسعار من Snapshots + كاش للشموع التاريخية
# ======================================================================


def price_from_snapshot(symbol: str, snap: Optional[dict], market_open: bool, now: datetime, cfg: Config) -> PriceCheck:
    if not snap or not (snap.get("latestTrade") or {}).get("p"):
        return PriceCheck(symbol, None, None, None, "DATA UNAVAILABLE", cfg.feed, note="لا توجد صفقات على هذا الـ feed")
    tr = snap["latestTrade"]
    t = parse_ts(tr["t"])
    age = (now - t).total_seconds()
    pc = PriceCheck(symbol, float(tr["p"]), t, age, classify_age(age, market_open, cfg), cfg.feed)
    q = snap.get("latestQuote") or {}
    if q.get("bp") and q.get("ap"):
        pc.bid, pc.ask = float(q["bp"]), float(q["ap"])
        try:
            pc.quote_time_utc = parse_ts(q["t"])
        except Exception:
            pass
        mid = (pc.bid + pc.ask) / 2
        if mid > 0 and pc.ask >= pc.bid:
            pc.spread_pct = (pc.ask - pc.bid) / mid * 100
    if pc.freshness == "MARKET CLOSED":
        pc.note = "السوق مغلق: هذا آخر سعر تداول وليس سعراً لحظياً"
    elif pc.freshness in ("DELAYED", "STALE"):
        pc.note = f"آخر صفقة على {cfg.feed.upper()} قبل {age:.0f} ثانية — لا يُعتبر لحظياً"
    return pc


class BarCache:
    """شموع 5m: تاريخ السهم يُجلب مرة يومياً، ثم شموع اليوم فقط في كل دورة.
    الوضع الهجين (الافتراضي): بيانات السوق الكامل SIP — مجانية إذا كانت أقدم من 15 دقيقة —
    لكل ما قبل آخر 15 دقيقة، و IEX لآخر 15 دقيقة فقط مع تقدير حجمها بنسبة حصة IEX المقاسة لنفس السهم اليوم."""

    SIP_DELAY_MIN = 16

    def __init__(self, hybrid: bool = True):
        self.hist: Dict[str, Tuple[str, pd.DataFrame]] = {}
        self.hybrid = hybrid
        self.sip_ok = True
        self.vol_ratio: Dict[str, Optional[float]] = {}
        self.cutoff: Optional[datetime] = None
        self.note: Optional[str] = None

    def _hist_feed(self) -> Optional[str]:
        return "sip" if (self.hybrid and self.sip_ok) else None

    def get(self, client: AlpacaClient, symbols: List[str]) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
        now = client.now_utc()
        today = now.astimezone(ET).strftime("%Y-%m-%d")
        day_start = now.astimezone(ET).replace(hour=4, minute=0, second=0, microsecond=0)
        missing = [s for s in symbols if self.hist.get(s, ("", None))[0] != today]
        for i in range(0, len(missing), 20):
            part = missing[i:i + 20]
            try:
                b = client.bars(part, "5Min", now - timedelta(days=client.cfg.history_calendar_days), day_start, feed=self._hist_feed())
            except ApiError as e:
                if self._hist_feed() == "sip":   # الخطة لا تسمح بـ SIP: ارجع لـ IEX
                    self.sip_ok, self.note = False, "SIP غير متاح، استُخدم IEX فقط: " + e.message
                    b = client.bars(part, "5Min", now - timedelta(days=client.cfg.history_calendar_days), day_start)
                else:
                    raise
            for sym in part:
                self.hist[sym] = (today, b.get(sym, bars_to_df([])))
        cut = now - timedelta(minutes=self.SIP_DELAY_MIN)
        cut = cut.replace(minute=cut.minute - cut.minute % 5, second=0, microsecond=0)
        self.cutoff = cut
        b5: Dict[str, pd.DataFrame] = {}
        b1: Dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), 50):
            part = symbols[i:i + 50]
            t5 = client.bars(part, "5Min", day_start)          # IEX اليوم (للحظي ولقياس حصة IEX)
            t1 = client.bars(part, "1Min", day_start)
            s5: Dict[str, pd.DataFrame] = {}
            if self.hybrid and self.sip_ok and cut > day_start:
                try:
                    s5 = client.bars(part, "5Min", day_start, cut - timedelta(seconds=1), feed="sip")
                except ApiError as e:
                    self.sip_ok, self.note = False, "SIP غير متاح، استُخدم IEX فقط: " + e.message
                    s5 = {}
            for sym in part:
                iex = t5.get(sym, bars_to_df([]))
                sip = s5.get(sym)
                if sip is not None and len(sip):
                    cut_ts = pd.Timestamp(cut)
                    sip_part = sip[sip.index < cut_ts]
                    iex_before = iex[iex.index < cut_ts]["volume"].sum() if len(iex) else 0
                    ratio = float(sip_part["volume"].sum() / iex_before) if iex_before > 0 else None
                    tail = iex[iex.index >= cut_ts].copy() if len(iex) else iex
                    if ratio and len(tail):
                        tail["volume"] = tail["volume"] * ratio
                    self.vol_ratio[sym] = ratio
                    today5 = pd.concat([x for x in (sip_part, tail) if len(x)]) if (len(sip_part) or len(tail)) else bars_to_df([])
                else:
                    self.vol_ratio[sym] = None
                    today5 = iex
                h = self.hist.get(sym, ("", bars_to_df([])))[1]
                frames = [x for x in (h, today5) if x is not None and len(x)]
                df = pd.concat(frames).sort_index() if frames else bars_to_df([])
                b5[sym] = df[~df.index.duplicated(keep="last")]
                b1[sym] = t1.get(sym, bars_to_df([]))
        return b5, b1

    def describe(self, sym: str) -> Optional[str]:
        if not self.hybrid:
            return None
        if not self.sip_ok:
            return self.note
        r = self.vol_ratio.get(sym)
        if r:
            return f"البيانات: السوق الكامل (SIP) حتى قبل 15 دقيقة + IEX لآخر 15 دقيقة (حجمها مقدّر ×{r:.0f})"
        return None


# ======================================================================
# الماسح التلقائي للسوق كله (مرحلتان)
#   1) مسح خفيف لكل الأسهم عبر Snapshots (100 رمز في الطلب) كل بضع دقائق
#   2) تحليل عميق (الشموع والمؤشرات) لأفضل المرشحين فقط
# ======================================================================

SCAN_EXCHANGES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}


@dataclass
class ScanConfig:
    enabled: bool = True
    every_sec: int = 300          # مسح السوق كله كل 5 دقائق
    min_price: float = 1.0
    max_price: float = 50.0
    min_change_pct: float = 2.0   # استراتيجية شراء: نبحث عن أسهم صاعدة اليوم
    min_day_volume: int = 20000   # حجم IEX اليوم (جزء من الحجم الكامل)
    min_rvol_est: float = 1.5     # حجم اليوم مقارنة بأمس بعد تعديل الوقت المنقضي
    max_candidates: int = 20      # أقصى عدد يُضاف من مسح واحد
    batch: int = 100
    auto_repeat: bool = False     # False = مسح واحد يومياً تلقائياً، ثم فقط عند زر التحديث
    max_total: int = 100          # أقصى عدد مرشحين متراكمين في اليوم


class MarketScanner:
    """المرشحون يتراكمون طوال اليوم: المسح الجديد يضيف ولا يحذف. يُصفَّر مع بداية يوم تداول جديد."""

    def __init__(self, cfg: Optional[ScanConfig] = None, folder: Optional[str] = None):
        self.cfg = cfg or ScanConfig()
        self.path = os.path.join(folder, "scan_today.json") if folder else None
        self.day = ""
        self.swept_day = ""
        self.force_next = False
        self.last_added: List[str] = []
        self.universe: List[str] = []
        self.universe_day = ""
        self.last_sweep: Optional[datetime] = None
        self.last_mono = float("-inf")
        self.scanned = 0
        self.passed = 0
        self.candidates: List[dict] = []
        self.error: Optional[str] = None
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            d = json.load(open(self.path, encoding="utf-8"))
            self.day, self.swept_day = d.get("day", ""), d.get("swept_day", "")
            self.candidates = d.get("candidates", [])
            self.scanned, self.passed = d.get("scanned", 0), d.get("passed", 0)
            if d.get("last"):
                self.last_sweep = parse_ts(d["last"])
        except Exception:
            pass

    def _save(self):
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            json.dump({"day": self.day, "swept_day": self.swept_day, "candidates": self.candidates, "scanned": self.scanned,
                       "passed": self.passed, "last": iso(self.last_sweep) if self.last_sweep else None},
                      open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def reset_today(self):
        self.candidates, self.swept_day, self.last_added = [], "", []
        self._save()

    def remove(self, sym: str):
        self.candidates = [x for x in self.candidates if x["symbol"] != sym]
        self._save()

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def load_universe(self, client: AlpacaClient) -> List[str]:
        day = client.now_utc().astimezone(ET).strftime("%Y-%m-%d")
        if self.universe and self.universe_day == day:
            return self.universe
        assets = client._get(PAPER_BASE, "/v2/assets", {"status": "active", "asset_class": "us_equity"})
        syms = []
        for a in assets:
            sym = a.get("symbol") or ""
            if not a.get("tradable") or a.get("exchange") not in SCAN_EXCHANGES:
                continue
            if not sym.replace(".", "").isalpha():
                continue
            if "." in sym and sym.split(".")[-1] not in ("A", "B", "C"):  # استبعد warrants/units مثل .WS و .U ؛ اسمح بـ BRK.B
                continue
            syms.append(sym)
        self.universe, self.universe_day = sorted(set(syms)), day
        return self.universe

    def sweep(self, client: AlpacaClient) -> pd.DataFrame:
        uni = self.load_universe(client)
        now = client.now_utc()
        rows = []
        for i in range(0, len(uni), self.cfg.batch):
            part = uni[i:i + self.cfg.batch]
            snaps = client._get(DATA_BASE, "/v2/stocks/snapshots", {"symbols": ",".join(part), "feed": client.cfg.feed}) or {}
            for sym, sn in snaps.items():
                lt, db, pb = sn.get("latestTrade") or {}, sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
                if not lt.get("p") or not pb.get("c"):
                    continue
                try:
                    age = (now - parse_ts(lt["t"])).total_seconds()
                except Exception:
                    continue
                rows.append({"symbol": sym, "price": float(lt["p"]), "age_sec": age, "prev_close": float(pb["c"]),
                             "day_vol": float(db.get("v") or 0), "prev_vol": float(pb.get("v") or 0),
                             "day_bar_date": (db.get("t") or "")[:10]})
        self.scanned = len(rows)
        return pd.DataFrame(rows)

    def select(self, df: pd.DataFrame, now_utc: datetime) -> List[dict]:
        c = self.cfg
        if df.empty:
            return []
        now_et = now_utc.astimezone(ET)
        today = now_et.strftime("%Y-%m-%d")
        open_t = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        frac = min(max((now_et - open_t).total_seconds() / (390 * 60), 0.05), 1.0)
        d = df.copy()
        d = d[d["day_bar_date"] == today] if (d["day_bar_date"] == today).any() else d
        d["chg"] = (d["price"] / d["prev_close"] - 1) * 100
        d["rvol_est"] = d["day_vol"] / (d["prev_vol"] * frac).where(d["prev_vol"] > 0)
        m = ((d["price"] >= c.min_price) & (d["price"] <= c.max_price) & (d["chg"] >= c.min_change_pct)
             & (d["day_vol"] >= c.min_day_volume) & (d["rvol_est"] >= c.min_rvol_est) & (d["age_sec"] <= 900))
        d = d[m]
        self.passed = int(len(d))
        if d.empty:
            return []
        d["rank"] = d["rvol_est"].clip(upper=10).rank(pct=True) + d["chg"].clip(upper=40).rank(pct=True)
        d = d.sort_values("rank", ascending=False).head(c.max_candidates)
        return [{"symbol": r.symbol, "price": r.price, "chg": round(r.chg, 2), "rvol_est": round(r.rvol_est, 2),
                 "reason": f"{r.chg:+.1f}% اليوم • حجم ~{r.rvol_est:.1f}× المعتاد (IEX، تقديري)"} for r in d.itertuples()]

    def maybe_sweep(self, client: AlpacaClient, now_utc: datetime, force: bool = False) -> List[str]:
        day = now_utc.astimezone(ET).strftime("%Y-%m-%d")
        if day != self.day:                       # يوم تداول جديد: قائمة جديدة
            self.day, self.candidates, self.swept_day, self.last_added = day, [], "", []
        force = force or self.force_next
        if not force:
            if self.cfg.auto_repeat:
                if time.monotonic() - self.last_mono < self.cfg.every_sec:
                    return []
            elif self.swept_day == day:          # مسح واحد يومياً فقط
                return []
        self.force_next = False
        self.last_mono = time.monotonic()
        try:
            df = self.sweep(client)
            had = {x["symbol"] for x in self.candidates}
            found = self.select(df, now_utc)
            when = now_utc.astimezone(RIYADH).strftime("%H:%M")
            added = []
            for x in found:
                if x["symbol"] in had or len(self.candidates) >= self.cfg.max_total:
                    continue
                x["first_seen"] = when
                x["reason"] = x["reason"] + f" • ظهر الساعة {when}"
                self.candidates.append(x)
                added.append(x["symbol"])
            self.last_added = added
            self.swept_day = day
            self.last_sweep = client.now_utc()
            self.error = None
            self._save()
            if not added:
                return ["🔎 مسح السوق: لا مرشحين جدد (القائمة كما هي)"] if had else []
            return [f"🔎 مرشحون جدد من مسح السوق: {', '.join(added)}"]
        except ApiError as e:
            self.error = "فشل مسح السوق: " + e.message
            return ["⚠️ " + self.error]

    def candidate_symbols(self) -> List[str]:
        return [x["symbol"] for x in self.candidates]

    def reason(self, sym: str) -> Optional[str]:
        for x in self.candidates:
            if x["symbol"] == sym:
                return x["reason"]
        return None

    def summary(self) -> dict:
        return {"universe": len(self.universe), "scanned": self.scanned, "passed": self.passed,
                "candidates": len(self.candidates), "last": self.last_sweep, "error": self.error,
                "added": list(self.last_added), "auto_repeat": self.cfg.auto_repeat,
                "reasons": {x["symbol"]: x["reason"] for x in self.candidates}}


# ======================================================================
# تنبيهات تيليجرام (مجانية عبر Telegram Bot API)
# ======================================================================
TG_API = "https://api.telegram.org"


class TelegramNotifier:
    """يرسل رسائل إلى محادثتك مع البوت. التوكن يُحفظ على جهازك فقط ولا يُطبع."""

    def __init__(self, token: str, chat_id: Optional[str], session: Optional[requests.Session] = None):
        self.token = (token or "").strip()
        self.chat_id = str(chat_id).strip() if chat_id else None
        self.s = session or requests.Session()
        self.last_error: Optional[str] = None

    @property
    def ready(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, params: dict) -> Optional[dict]:
        try:
            r = self.s.get(f"{TG_API}/bot{self.token}/{method}", params=params, timeout=15)
            j = r.json()
        except Exception as e:
            self.last_error = f"تعذر الاتصال بتيليجرام ({type(e).__name__})"
            return None
        if not j.get("ok"):
            self.last_error = "رفض تيليجرام الطلب: " + str(j.get("description", ""))[:120]
            return None
        self.last_error = None
        return j

    def discover_chat_id(self) -> Optional[str]:
        """بعد أن ترسل /start للبوت: يقرأ آخر رسالة ويأخذ رقم المحادثة."""
        j = self._call("getUpdates", {"limit": 20})
        if not j:
            return None
        for upd in reversed(j.get("result", [])):
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                self.chat_id = str(chat["id"])
                return self.chat_id
        self.last_error = "لم أجد رسالة. افتح البوت في تيليجرام وأرسل له /start ثم أعد المحاولة."
        return None

    def send(self, text: str) -> bool:
        if not self.ready:
            return False
        return self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:3900],
                                          "disable_web_page_preview": "true"}) is not None


# ======================================================================
# متابعة نتيجة كل إشارة "جاهز" تلقائياً (من شموع الدقيقة)
# ======================================================================
OUT_COLS = ["id", "day", "symbol", "signal_utc", "entry", "stop", "t1", "t2", "score", "status",
            "outcome", "outcome_utc", "minutes", "r", "mfe_r", "mae_r", "last_price"]


class SignalOutcomes:
    """يسجل كل إشارة ENTRY CONFIRMED ثم يتابع: هل لمس الهدف الأول قبل الوقف؟
    إذا لمست شمعة دقيقة واحدة الوقف والهدف معاً نفترض الوقف أولاً (متحفظ)."""

    def __init__(self, folder: str = "."):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "signal_outcomes.csv")
        self.df = pd.read_csv(self.path) if os.path.exists(self.path) else pd.DataFrame(columns=OUT_COLS)

    def _save(self):
        tmp = self.path + ".tmp"
        self.df.to_csv(tmp, index=False)
        os.replace(tmp, self.path)

    def open_rows(self) -> pd.DataFrame:
        return self.df[self.df["status"] == "OPEN"] if len(self.df) else self.df

    def record(self, a: "Analysis", now_utc: datetime) -> Optional[dict]:
        s = a.signal
        if not s or s.status != "ENTRY CONFIRMED" or not s.entry:
            return None
        day = now_utc.astimezone(ET).strftime("%Y-%m-%d")
        if len(self.df):
            same = self.df[(self.df["symbol"] == a.symbol) & (self.df["day"] == day)]
            if (same["status"] == "OPEN").any():
                return None
            # نفس السهم يُسجَّل مرة أخرى في نفس اليوم فقط إذا أُغلقت الإشارة السابقة وظهر مستوى دخول مختلف
            if len(same) and abs(float(same["entry"].iloc[-1]) - s.entry) < 1e-9:
                return None
        nid = int(self.df["id"].max()) + 1 if len(self.df) else 1
        row = {"id": nid, "day": day, "symbol": a.symbol, "signal_utc": iso(now_utc), "entry": s.entry, "stop": s.stop,
               "t1": s.target1, "t2": s.target2, "score": s.score, "status": "OPEN", "outcome": "", "outcome_utc": "",
               "minutes": None, "r": None, "mfe_r": 0.0, "mae_r": 0.0, "last_price": s.entry}
        self.df = pd.concat([self.df, pd.DataFrame([row])], ignore_index=True) if len(self.df) else pd.DataFrame([row])
        self._save()
        return row

    def update(self, a: "Analysis", now_utc: datetime) -> List[str]:
        """يحدّث الإشارات المفتوحة لهذا السهم. يعيد رسائل عند حسم النتيجة."""
        msgs: List[str] = []
        if not len(self.df):
            return msgs
        idx = self.df.index[(self.df["symbol"] == a.symbol) & (self.df["status"] == "OPEN")]
        if not len(idx):
            return msgs
        b1 = a.bars1_today
        for i in idx:
            r = self.df.loc[i]
            entry, stop, t1 = float(r["entry"]), float(r["stop"]), float(r["t1"])
            risk = entry - stop
            if risk <= 0:
                continue
            t0 = pd.Timestamp(r["signal_utc"])
            bars = b1[b1.index.tz_convert(UTC) >= t0] if b1 is not None and len(b1) else pd.DataFrame()
            outcome, when = None, None
            hi_max, lo_min = entry, entry
            for ts, bar in bars.iterrows():
                hi_max, lo_min = max(hi_max, bar["high"]), min(lo_min, bar["low"])
                if bar["low"] <= stop:
                    outcome, when = "STOP", ts
                    break
                if bar["high"] >= t1:
                    outcome, when = "TARGET 1", ts
                    break
            last = a.price.price if a.price.price is not None else (float(bars["close"].iloc[-1]) if len(bars) else entry)
            self.df.at[i, "mfe_r"] = round(max(float(r["mfe_r"] or 0), (hi_max - entry) / risk), 2)
            self.df.at[i, "mae_r"] = round(min(float(r["mae_r"] or 0), (lo_min - entry) / risk), 2)
            self.df.at[i, "last_price"] = last
            if outcome is None and now_utc.astimezone(ET).strftime("%H:%M") >= "15:55":
                outcome, when = "END OF DAY", pd.Timestamp(now_utc)
            if outcome:
                rv = -1.0 if outcome == "STOP" else 2.0 if outcome == "TARGET 1" else round((last - entry) / risk, 2)
                mins = round((pd.Timestamp(when).tz_convert(UTC) - t0).total_seconds() / 60)
                self.df.loc[i, ["status", "outcome", "outcome_utc", "minutes", "r"]] = ["CLOSED", outcome, iso(pd.Timestamp(when).to_pydatetime()), mins, rv]
                word = {"STOP": "🔴 ضربت الوقف", "TARGET 1": "🟢 وصلت الهدف الأول", "END OF DAY": "⏹️ انتهى اليوم دون حسم"}[outcome]
                msgs.append(f"{word}: إشارة {a.symbol} (دخول {fmt_price(entry)}) بعد {mins} دقيقة = {rv:+.2f}R")
        self._save()
        return msgs

    def stats(self, day: Optional[str] = None) -> dict:
        d = self.df[self.df["status"] == "CLOSED"] if len(self.df) else self.df
        if day and len(d):
            d = d[d["day"] == day]
        n = int(len(d))
        if not n:
            return {"n": 0, "open": int(len(self.open_rows()))}
        hit = int((d["outcome"] == "TARGET 1").sum())
        stop = int((d["outcome"] == "STOP").sum())
        decided = hit + stop
        return {"n": n, "hit_t1": hit, "stopped": stop, "eod": int((d["outcome"] == "END OF DAY").sum()),
                "hit_rate": round(hit / decided, 3) if decided else None,
                "avg_r": round(float(pd.to_numeric(d["r"]).mean()), 2),
                "avg_minutes": round(float(pd.to_numeric(d["minutes"]).mean()), 1),
                "open": int(len(self.open_rows())), "warning": "عينة صغيرة (< 30 إشارة) — لا تكفي للحكم" if n < 30 else ""}


# ======================================================================
# حد الخسارة اليومي
# ======================================================================


def day_pnl(tracker: "DealsTracker", live_df: Optional[pd.DataFrame], day_et: str) -> dict:
    """المحقق اليوم (بتاريخ نيويورك) + غير المحقق للمراكز المفتوحة."""
    realized = daily_pnl(tracker).get(day_et, 0.0)
    unreal = 0.0
    if live_df is not None and len(live_df) and "unrealized_pnl" in live_df:
        unreal = float(pd.to_numeric(live_df["unrealized_pnl"], errors="coerce").fillna(0).sum())
    return {"realized": round(realized, 2), "unrealized": round(unreal, 2), "total": round(realized + unreal, 2)}


def loss_limit_hit(pnl: dict, limit: float) -> bool:
    return bool(limit and limit > 0 and pnl["total"] <= -abs(limit))


# ======================================================================
# المشرف: يربط الدورة بالتنبيهات ومتابعة الإشارات وحد الخسارة (مع منع التكرار)
# ======================================================================


class Supervisor:
    def __init__(self, folder: str, notifier: Optional[TelegramNotifier] = None, daily_loss_limit: float = 0.0,
                 alert_types: Optional[dict] = None, max_open_positions: int = 0):
        self.folder = folder
        self.max_open = int(max_open_positions or 0)
        self.open_count = 0
        self.max_open_hit = False
        self.notifier = notifier
        self.limit = daily_loss_limit
        self.types = {"ready": True, "levels": True, "news": True, "loss": True, "outcomes": True, "daily": True, "price": True}
        self.price_alerts: Dict[str, dict] = {}   # {"AMD": {"above": 30.0, "below": 25.0}}
        if alert_types:
            self.types.update(alert_types)
        self.outcomes = SignalOutcomes(folder)
        self.sent_path = os.path.join(folder, "alerts_sent.json")
        try:
            self.sent = json.load(open(self.sent_path))
        except Exception:
            self.sent = {}
        self.limit_hit = False
        self.pnl = {"realized": 0.0, "unrealized": 0.0, "total": 0.0}

    def _once(self, key: str, day: str) -> bool:
        if self.sent.get(key) == day:
            return False
        self.sent[key] = day
        try:
            keep = {k: v for k, v in self.sent.items() if v >= day}  # احذف مفاتيح الأيام السابقة
            json.dump(keep, open(self.sent_path, "w"))
        except Exception:
            pass
        return True

    def _push(self, kind: str, text: str):
        if self.notifier is not None and self.notifier.ready and self.types.get(kind, True):
            self.notifier.send(text)

    def on_cycle(self, info: dict, analyses: List["Analysis"], live_df, events: List[str],
                 tracker: "DealsTracker", now_utc: datetime) -> List[str]:
        day = now_utc.astimezone(ET).strftime("%Y-%m-%d")
        out: List[str] = []
        self.open_count = int(len(live_df)) if live_df is not None else 0
        self.max_open_hit = self.max_open > 0 and self.open_count >= self.max_open
        for a in analyses or []:
            s = a.signal
            if s and s.status == "ENTRY CONFIRMED" and self._once(f"ready:{a.symbol}:{s.entry}", day):
                self.outcomes.record(a, now_utc)
                txt = (f"🟢 {a.symbol} جاهز للدخول (Score {s.score})\n"
                       f"دخول {fmt_price(s.entry)} • وقف {fmt_price(s.stop)} • هدف1 {fmt_price(s.target1)} • هدف2 {fmt_price(s.target2)}\n"
                       f"{s.shares} سهم • أقصى خسارة ${s.max_loss}\nالسعر من IEX، {_t(a.price.trade_time_utc)}. ليست توصية.")
                if self.max_open_hit:
                    txt += f"\n⚠️ لديك {self.open_count} مراكز مفتوحة (الحد {self.max_open}) — لا تفتح جديداً قبل إغلاق واحد."
                self._push("ready", txt)
                out.append(f"🟢 {a.symbol} جاهز للدخول")
            for m in self.outcomes.update(a, now_utc):
                self._push("outcomes", m)
                out.append(m)
            pa = self.price_alerts.get(a.symbol) or {}
            px = a.price.price
            if px is not None and a.price.freshness == "FRESH":
                up, dn = pa.get("above"), pa.get("below")
                if up and px >= float(up) and self._once(f"above:{a.symbol}:{up}", day):
                    m = f"⬆️ {a.symbol} تجاوز {fmt_price(float(up))} — السعر الآن {fmt_price(px)} ({_t(a.price.trade_time_utc)})"
                    self._push("price", m); out.append(m)
                if dn and px <= float(dn) and self._once(f"below:{a.symbol}:{dn}", day):
                    m = f"⬇️ {a.symbol} نزل تحت {fmt_price(float(dn))} — السعر الآن {fmt_price(px)} ({_t(a.price.trade_time_utc)})"
                    self._push("price", m); out.append(m)
        if live_df is not None and len(live_df):
            for _, p in live_df.iterrows():
                last = p.get("last_price")
                if last is None or (isinstance(last, float) and math.isnan(last)):
                    continue
                stop, tgt = p.get("stop"), p.get("target")
                fresh = p.get("freshness")
                tag = "" if fresh == "FRESH" else f" (السعر {fresh} — تحقق من وسيطك)"
                if stop and not pd.isna(stop) and last <= stop and self._once(f"stop:{p['symbol']}:{stop}", day):
                    m = f"🔴 {p['symbol']}: السعر {fmt_price(last)} وصل وقفك {fmt_price(stop)}{tag}"
                    self._push("levels", m); out.append(m)
                if tgt and not pd.isna(tgt) and last >= tgt and self._once(f"tgt:{p['symbol']}:{tgt}", day):
                    m = f"🎯 {p['symbol']}: السعر {fmt_price(last)} وصل هدفك {fmt_price(tgt)}{tag}"
                    self._push("levels", m); out.append(m)
        for e in events or []:
            if e.startswith("📰") and any(TAG_LABELS[t] in e for t in RISK_TAGS) and self._once("news:" + e[:120], day):
                self._push("news", e)
        self.pnl = day_pnl(tracker, live_df, day)
        self.limit_hit = loss_limit_hit(self.pnl, self.limit)
        if self.limit_hit and self._once("loss_limit", day):
            m = (f"⛔ وصلت حد الخسارة اليومي: {self.pnl['total']:+.2f}$ (الحد {self.limit:.0f}$).\n"
                 "توقف عن فتح صفقات جديدة اليوم.")
            self._push("loss", m); out.append(m)
        if info.get("phase") == "DONE" and self._once("daily_summary", day):
            st_ = self.outcomes.stats(day)
            m = (f"📊 ملخص اليوم {day}\nإشارات جاهزة: {st_.get('n', 0)}"
                 + (f" • وصلت الهدف: {st_['hit_t1']} • ضربت الوقف: {st_['stopped']} • متوسط {st_['avg_r']:+.2f}R" if st_.get("n") else "")
                 + f"\nصفقاتك اليوم: {self.pnl['total']:+.2f}$")
            self._push("daily", m); out.append(m)
        return out
