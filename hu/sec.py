# -*- coding: utf-8 -*-
"""SEC EDGAR: إفصاحات المخاطر وعدد الأسهم القائمة.

- مجاني، بلا مفتاح. يتطلب User-Agent فيه اسم وبريد (متغير البيئة SEC_USER_AGENT).
- الحد الرسمي 10 طلبات/ثانية — نلتزم بـ 8 كحد أقصى.
- لا نستخدم أي إفصاح قُبل بعد لحظة القرار (acceptanceDateTime <= now).
- Float غير متاح مجاناً بشكل موثوق، لذلك لا نقدّره: نعرض عدد الأسهم القائمة فقط.
"""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SHARES_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/EntityCommonStockSharesOutstanding.json"

# (النماذج، عدد الأيام، الإجراء، الوزن، الوصف)
#   EXCLUDE = استبعاد السهم ، PENALTY = خصم من الدرجة
RISK_RULES = [
    ({"424B1", "424B2", "424B3", "424B4", "424B5", "424B7"}, 10, "EXCLUDE", 0,
     "نشرة طرح نهائية ({form}) بتاريخ {date} — طرح أسهم جارٍ/حديث (تخفيف)"),
    ({"25-NSE", "25"}, 120, "EXCLUDE", 0, "إشعار شطب من البورصة ({form}) بتاريخ {date}"),
    ({"424B1", "424B2", "424B3", "424B4", "424B5", "424B7"}, 60, "PENALTY", 15,
     "نشرة طرح ({form}) خلال آخر 60 يوماً ({date}) — تخفيف قريب"),
    ({"S-1", "S-1/A", "F-1", "F-1/A"}, 90, "PENALTY", 15, "تسجيل طرح أسهم ({form}) بتاريخ {date} — طرح محتمل"),
    ({"S-3", "S-3/A", "F-3", "F-3/A"}, 180, "PENALTY", 10, "تسجيل رف (Shelf) ({form}) بتاريخ {date} — يسمح بطرح في أي وقت"),
]
EIGHTK_ITEMS = {
    "3.01": (90, "PENALTY", 15, "8-K بند 3.01: إشعار من البورصة بعدم الامتثال/خطر الشطب ({date})"),
    "3.03": (60, "PENALTY", 10, "8-K بند 3.03: تعديل حقوق المساهمين — كثيراً ما يكون تجميعاً عكسياً ({date})"),
    "1.01": (30, "INFO", 0, "8-K بند 1.01: اتفاقية جوهرية ({date})"),
    "8.01": (5, "INFO", 0, "8-K بند 8.01: حدث آخر ({date})"),
}
PROXY_FORMS = {"DEF 14A", "PRE 14A"}  # قد تتضمن تصويتاً على تجميع عكسي — نذكرها كاحتمال فقط


@dataclass
class SecInfo:
    symbol: str
    ok: bool = False                     # هل نجح الجلب؟
    cik: Optional[int] = None
    name: Optional[str] = None
    exclude: List[str] = field(default_factory=list)
    penalties: List[tuple] = field(default_factory=list)   # (وزن، سبب)
    infos: List[str] = field(default_factory=list)
    shares_out: Optional[float] = None
    shares_out_date: Optional[str] = None
    recent_8k_days: Optional[float] = None   # منذ آخر 8-K (للأخبار الجوهرية)
    note: str = ""

    @property
    def penalty(self) -> int:
        return sum(w for w, _ in self.penalties)


class SecClient:
    def __init__(self, user_agent: Optional[str] = None, session: Optional[requests.Session] = None,
                 cache_dir: Optional[str] = None, max_rps: float = 8.0):
        ua = (user_agent or os.environ.get("SEC_USER_AGENT") or "").strip()
        self.enabled = "@" in ua      # SEC تطلب بريداً للتواصل
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": ua or "headsup-personal", "Accept-Encoding": "gzip, deflate"})
        self.min_gap = 1.0 / max_rps
        self._last = 0.0
        self.cache_dir = cache_dir
        self._tickers: Optional[Dict[str, dict]] = None
        self.calls = 0
        self.errors: List[str] = []

    def _get(self, url: str) -> Optional[dict]:
        wait = self.min_gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        try:
            r = self.s.get(url, timeout=20)
        except Exception as e:
            self.errors.append(f"{type(e).__name__}")
            return None
        if r.status_code == 404:
            return {}
        if r.status_code != 200:
            self.errors.append(f"HTTP {r.status_code}")
            return None
        try:
            return r.json()
        except Exception:
            self.errors.append("JSON")
            return None

    def tickers(self) -> Dict[str, dict]:
        if self._tickers is not None:
            return self._tickers
        path = os.path.join(self.cache_dir, "sec_tickers.json") if self.cache_dir else None
        if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < 86400:
            try:
                self._tickers = json.load(open(path, encoding="utf-8"))
                return self._tickers
            except Exception:
                pass
        d = self._get(TICKERS_URL) or {}
        m = {}
        for v in d.values():
            t = str(v.get("ticker", "")).upper()
            if t:
                m[t] = {"cik": int(v["cik_str"]), "name": v.get("title")}
        self._tickers = m
        if path and m:
            json.dump(m, open(path, "w", encoding="utf-8"))
        return m

    def info(self, symbol: str, now: datetime) -> SecInfo:
        si = SecInfo(symbol)
        if not self.enabled:
            si.note = "بيانات SEC غير متوفرة: لم يُضبط SEC_USER_AGENT (اسم + بريد)"
            return si
        t = self.tickers()
        if not t:
            si.note = "بيانات SEC غير متوفرة: تعذر جلب قائمة الرموز"
            return si
        key = symbol.upper().replace(".", "-")
        rec = t.get(key) or t.get(symbol.upper())
        if not rec:
            si.note = "الرمز غير موجود في قائمة SEC (قد يكون صندوقاً أو شركة أجنبية لا تقدم إفصاحات EDGAR)"
            return si
        si.cik, si.name = rec["cik"], rec["name"]
        sub = self._get(SUBMISSIONS_URL.format(cik=si.cik))
        if sub is None:
            si.note = "بيانات SEC غير متوفرة: فشل جلب الإفصاحات"
            return si
        si.ok = True
        evaluate_filings(si, sub, now)
        sh = self._get(SHARES_URL.format(cik=si.cik))
        if sh:
            parse_shares(si, sh, now)
        return si


def _accepted(rec: dict, i: int) -> Optional[datetime]:
    acc = (rec.get("acceptanceDateTime") or [None] * (i + 1))[i]
    if acc:
        try:
            return datetime.fromisoformat(acc.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    fd = (rec.get("filingDate") or [None] * (i + 1))[i]
    if fd:
        return datetime.fromisoformat(fd).replace(tzinfo=timezone.utc)
    return None


def evaluate_filings(si: SecInfo, sub: dict, now: datetime) -> None:
    rec = ((sub.get("filings") or {}).get("recent")) or {}
    forms = rec.get("form") or []
    seen = set()
    for i, form in enumerate(forms):
        when = _accepted(rec, i)
        if when is None or when > now:        # لا نستخدم ما لم يكن متاحاً وقت القرار
            continue
        age = (now - when).total_seconds() / 86400
        date = when.strftime("%Y-%m-%d")
        for forms_set, days, action, w, text in RISK_RULES:
            if form in forms_set and age <= days and (action, text[:12]) not in seen:
                seen.add((action, text[:12]))
                msg = text.format(form=form, date=date)
                if action == "EXCLUDE":
                    si.exclude.append(msg)
                else:
                    si.penalties.append((w, msg))
                break
        if form in ("8-K", "8-K/A", "6-K"):
            if si.recent_8k_days is None or age < si.recent_8k_days:
                si.recent_8k_days = age
            items = str((rec.get("items") or [""] * (i + 1))[i] or "")
            for it in [x.strip() for x in items.split(",") if x.strip()]:
                r = EIGHTK_ITEMS.get(it)
                if r and age <= r[0] and ("8k", it) not in seen:
                    seen.add(("8k", it))
                    msg = r[3].format(date=date)
                    if r[1] == "PENALTY":
                        si.penalties.append((r[2], msg))
                    else:
                        si.infos.append(msg)
        if form in PROXY_FORMS and age <= 60 and ("proxy",) not in seen:
            seen.add(("proxy",))
            si.infos.append(f"{form} بتاريخ {date}: تصويت للمساهمين — قد يشمل تجميعاً عكسياً أو زيادة أسهم (راجع الإفصاح)")


def parse_shares(si: SecInfo, d: dict, now: datetime) -> None:
    rows = ((d.get("units") or {}).get("shares")) or []
    best = None
    for r in rows:
        filed = r.get("filed")
        if not filed or datetime.fromisoformat(filed).replace(tzinfo=timezone.utc) > now:
            continue
        if best is None or (r.get("end", ""), filed) > (best.get("end", ""), best.get("filed")):
            best = r
    if best and best.get("val"):
        si.shares_out = float(best["val"])
        si.shares_out_date = best.get("end")
