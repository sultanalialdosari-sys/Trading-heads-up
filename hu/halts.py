# -*- coding: utf-8 -*-
"""توقفات التداول الرسمية من خلاصة Nasdaq Trader المجانية (Trade Halt RSS).

  https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts   — توقفات اليوم (Nasdaq والبورصات الأخرى)

الشروط: مجانية؛ يُمنع تعديل الخلاصة أو تحريفها — نعرض كل توقف كما جاء مع ذكر المصدر.
التحديث مرة في الدقيقة؛ لا نطلب أكثر من مرة في الدقيقة (حارس داخلي)، والنظام يطلبها مرة لكل فحص.
المحلل متسامح: يقرأ العناصر باسمها المحلي بغض النظر عن البادئة (ndaq:) وحالة الأحرف.
"""
import re
import time
import xml.etree.ElementTree as ET_XML
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional

from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
SOURCE = "Nasdaq Trader — Trade Halt RSS"
MIN_INTERVAL = 60.0

# رموز الأسباب (Nasdaq Trading Halt Codes) — الأكثر شيوعاً في الأسهم الرخيصة
REASON_AR = {
    "LUDP": "إيقاف تقلب (LULD)",
    "LUDS": "إيقاف تقلب — تجاوز حد السعر (LULD)",
    "M": "إيقاف تقلب",
    "T1": "بانتظار خبر",
    "T2": "نُشر الخبر",
    "T3": "الخبر نُشر — بانتظار التسعير",
    "T5": "إيقاف مؤقت: حركة 10%+ في 5 دقائق",
    "T6": "نشاط سوق غير اعتيادي",
    "T7": "إيقاف — تعديل السعر/الكمية",
    "T8": "إيقاف صندوق ETF",
    "T12": "إيقاف — طلب معلومات إضافية",
    "H4": "عدم امتثال لشروط الإدراج",
    "H9": "لم يقدّم التقارير المطلوبة",
    "H10": "تعليق من SEC",
    "H11": "مخاوف تنظيمية",
    "O1": "إيقاف تشغيلي",
    "IPO1": "طرح أولي — لم يبدأ التداول",
    "MWC1": "قاطع السوق — المستوى 1",
    "MWC2": "قاطع السوق — المستوى 2",
    "MWC3": "قاطع السوق — المستوى 3",
    "D": "شطب من الإدراج",
}
VOLATILITY = {"LUDP", "LUDS", "M", "T5"}


@dataclass
class Halt:
    symbol: str
    name: str
    market: str
    reason: str
    halt_date: Optional[str]      # YYYY-MM-DD (نيويورك)
    halt_time: Optional[str]      # HH:MM:SS (نيويورك)
    pause_price: Optional[float]
    resume_date: Optional[str]
    resume_quote: Optional[str]
    resume_trade: Optional[str]

    @property
    def reason_ar(self) -> str:
        return REASON_AR.get(self.reason, self.reason or "غير محدد")

    @property
    def volatility(self) -> bool:
        return self.reason in VOLATILITY

    def halted_at(self) -> Optional[datetime]:
        return _dt(self.halt_date, self.halt_time)

    def resumes_at(self) -> Optional[datetime]:
        return _dt(self.resume_date or self.halt_date, self.resume_trade)

    def active(self, now: datetime) -> bool:
        """موقوف الآن: بدأ التوقف ولم يُعلن وقت استئناف أو لم يحن بعد."""
        h = self.halted_at()
        if h is None or h > now:
            return False
        r = self.resumes_at()
        return r is None or r > now

    def to_row(self, day: str, seen_utc: str) -> dict:
        d = asdict(self)
        d.update(day=day, seen_utc=seen_utc)
        return d


def _dt(d: Optional[str], t: Optional[str]) -> Optional[datetime]:
    if not d or not t:
        return None
    try:
        hh, mm, ss = (t.split(".")[0].split(":") + ["0", "0"])[:3]
        return datetime.combine(date.fromisoformat(d), dtime(int(hh), int(mm), int(float(ss))), tzinfo=NY)
    except (ValueError, TypeError):
        return None


def _date(s: str) -> Optional[str]:
    s = (s or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _time(s: str) -> Optional[str]:
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}" if m else None


def _num(s: str) -> Optional[float]:
    try:
        v = float(str(s).replace(",", "").strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


_ITEM = re.compile(r"<item[\s>](.*?)</item>", re.S | re.I)
_FIELD = re.compile(r"<(?:[\w.-]+:)?([\w.-]+)\s*(?:/>|>(.*?)</(?:[\w.-]+:)?\1\s*>)", re.S)


def _items(xml_text: str) -> List[Dict[str, str]]:
    """عناصر <item> كقواميس {الاسم المحلي بأحرف صغيرة: النص}. الخلاصة الفعلية تستخدم البادئة ndaq: وقد لا
    تعرّفها، فيرفضها قارئ XML القياسي — عندها نقرأ العناصر مباشرة بتعبير منتظم."""
    try:
        root = ET_XML.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
        out = []
        for it in root.iter():
            if _local(it.tag) == "item":
                out.append({_local(ch.tag): (ch.text or "").strip() for ch in it})
        return out
    except ET_XML.ParseError:
        pass
    out = []
    for body in _ITEM.findall(xml_text or ""):
        f: Dict[str, str] = {}
        for name, val in _FIELD.findall(body):
            k = name.lower()
            if k not in f:
                f[k] = re.sub(r"^<!\[CDATA\[|\]\]>$", "", (val or "").strip())
        out.append(f)
    return out


def parse(xml_text: str) -> List[Halt]:
    """يقرأ عناصر <item> بأسمائها المحلية (haltdate، halttime، issuesymbol، mkt، ...)."""
    out = []
    for f in _items(xml_text):
        sym = (f.get("issuesymbol") or f.get("symbol") or "").upper()
        if not sym:
            m = re.search(r"\b([A-Z]{1,5})\b", f.get("title", ""))
            sym = m.group(1) if m else ""
        if not sym:
            continue
        out.append(Halt(symbol=sym, name=f.get("issuename", ""), market=f.get("market") or f.get("mkt", ""),
                        reason=(f.get("reasoncode") or "").upper(), halt_date=_date(f.get("haltdate", "")),
                        halt_time=_time(f.get("halttime", "")), pause_price=_num(f.get("pausethresholdprice")),
                        resume_date=_date(f.get("resumptiondate", "")), resume_quote=_time(f.get("resumptionquotetime", "")),
                        resume_trade=_time(f.get("resumptiontradetime", ""))))
    return out


def fetch(session=None, timeout: float = 15.0, force: bool = False) -> List[Halt]:
    """توقفات اليوم. يحترم «لا أكثر من طلب في الدقيقة» بإعادة آخر نتيجة إذا طُلبت مبكراً."""
    import requests
    s = session or requests.Session()
    c = getattr(s, "_hu_halts", None)            # الذاكرة على كائن الجلسة نفسه (لا على id: المعرّفات يُعاد استخدامها)
    if not force and c and time.time() - c["t"] < MIN_INTERVAL:
        return c["data"]
    r = s.get(URL, timeout=timeout, headers={"User-Agent": "HeadsUp paper-trading monitor (research, non-commercial)"})
    if getattr(r, "status_code", 200) != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = parse(r.text)
    try:
        s._hu_halts = {"t": time.time(), "data": data}
    except AttributeError:
        pass
    return data


def active_map(halts: List[Halt], now: datetime) -> Dict[str, Halt]:
    return {h.symbol: h for h in halts if h.active(now)}
