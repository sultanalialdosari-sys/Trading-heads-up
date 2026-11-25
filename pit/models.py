# -*- coding: utf-8 -*-
"""هياكل بيانات طبقة البيانات نقطية الزمن (Point-in-Time).

كل سجل يحمل وقتين: متى حدث، ومتى أصبح معروفاً للسوق (known_ts). كل استعلام يمر بـ as_of
ويرجع فقط ما known_ts <= as_of. كل الأوقات timezone-aware بتوقيت UTC.
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Literal, Optional, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

TF_DELTA = {"1Min": timedelta(minutes=1), "5Min": timedelta(minutes=5)}
DAILY_KNOWN_AT = time(16, 0)          # الشمعة اليومية تُعرف عند إغلاق الجلسة 16:00 نيويورك
SEC_LAG = timedelta(minutes=10)       # تأخير نشر EDGAR بعد القبول (تقدير متحفظ)
SHARES_STALE_DAYS = 400


def et_dt(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(UTC)


def daily_known_ts(d: date) -> datetime:
    """شمعة يوم d لا تُستخدم قبل 16:00 نيويورك من نفس اليوم (أيام الإغلاق المبكر: تحفظ إضافي، لا تسرّب)."""
    return et_dt(d, DAILY_KNOWN_AT)


@dataclass(frozen=True)
class Asset:
    asset_id: str                 # مفتاح ثابت من Alpaca
    symbol: str
    name: str
    exchange: str                 # NASDAQ / NYSE / AMEX (لا OTC)
    status: Literal["active", "inactive"]
    first_bar: Optional[date]
    last_bar: Optional[date]      # آخر يوم تداول معروف في البيانات
    cik: Optional[int]
    symbol_shared: bool = False   # الرمز نفسه لأكثر من أصل (أُعيد استخدامه): تاريخ الشموع قد يكون مختلطاً

    def traded_on(self, d: date) -> bool:
        return (self.first_bar is not None and self.first_bar <= d
                and (self.last_bar is None or d <= self.last_bar))

    def masked(self, as_of: datetime) -> "Asset":
        """ما يُرى عند as_of: الحالة الحالية وتاريخ الشطب معلومات من المستقبل."""
        d = as_of.astimezone(ET).date()
        last = self.last_bar if self.last_bar is not None and self.last_bar < d else None
        # البورصة الحالية OTC تعني غالباً أنه شُطب لاحقاً؛ شموعه من SIP أي كان مدرجاً وقتها
        exch = "LISTED" if self.exchange == "OTC" else self.exchange
        return replace(self, status="active", last_bar=last, exchange=exch)


@dataclass(frozen=True)
class Filing:
    cik: int
    accession: str
    form: str                      # "8-K", "424B5", "S-3" ...
    items: Tuple[str, ...]         # ("3.01", "5.03")
    filed: date
    accepted_raw: str              # النص كما جاء من EDGAR
    accepted_at: datetime          # تفسيرنا المتحفظ (UTC)

    @property
    def known_ts(self) -> datetime:
        return self.accepted_at + SEC_LAG

    def first_session(self, trading_days) -> Optional[date]:
        """أول جلسة يمكنها التفاعل: نفس اليوم إذا عُرف قبل 16:00، وإلا يوم التداول التالي."""
        k = self.known_ts.astimezone(ET)
        for d in trading_days:
            if d > k.date() or (d == k.date() and k.time() < DAILY_KNOWN_AT):
                return d
        return None


@dataclass(frozen=True)
class SplitEvent:
    symbol: str
    ex_date: date
    ratio: float                   # أسهم جديدة لكل سهم قديم: تجميع عكسي 1:10 = 0.1

    @property
    def known_ts(self) -> datetime:
        return et_dt(self.ex_date, time(9, 30))


@dataclass(frozen=True)
class SharesFact:
    cik: int
    value: float
    period_end: date
    known_ts: datetime             # من وقت قبول الإفصاح الذي حملها
    split_adjusted: bool
    stale: bool                    # أقدم من SHARES_STALE_DAYS
    accession: Optional[str] = None
