# -*- coding: utf-8 -*-
"""SEC EDGAR نقطي الزمن: الإفصاحات بوقت القبول، والأسهم القائمة من XBRL بوقت الإفصاح الذي حملها.

قرار التوقيت (متحفظ دائماً):
  acceptanceDateTime يأتي بصيغة مثل 2024-02-02T16:06:46.000Z. نقرأ الساعة كتوقيت نيويورك دائماً.
  لو كانت فعلاً UTC فهذا يؤخر معرفة الإفصاح 4–5 ساعات فقط (خسارة صغيرة)، ولا يسبّب نظراً للمستقبل أبداً.
  diagnose_hours() تفحص توزيع الساعات وتسجّل أي التفسيرين تدعمه البيانات.
  بلا acceptanceDateTime: نعتبر الإفصاح معروفاً 22:00 نيويورك من يوم filingDate (نهاية ساعات EDGAR).
"""
import gzip
import json
import os
import time
from datetime import date, datetime, time as dtime
from typing import Dict, Iterable, List, Optional, Tuple

from pit.models import ET, SHARES_STALE_DAYS, UTC, Filing, SharesFact, SplitEvent, et_dt

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
SHARES_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/dei/EntityCommonStockSharesOutstanding.json"
NO_ACCEPT_TIME = dtime(22, 0)


# ====================================================================== التحليل (بلا شبكة)
def parse_accepted(raw: Optional[str], filed: Optional[str]) -> Optional[datetime]:
    if raw:
        try:
            wall = datetime.fromisoformat(raw.replace("Z", "").split(".")[0])
            return wall.replace(tzinfo=ET).astimezone(UTC)
        except ValueError:
            pass
    if filed:
        try:
            return et_dt(date.fromisoformat(filed), NO_ACCEPT_TIME)
        except ValueError:
            pass
    return None


def _columns(block: dict) -> List[dict]:
    keys = [k for k, v in block.items() if isinstance(v, list)]
    n = max((len(block[k]) for k in keys), default=0)
    return [{k: (block[k][i] if i < len(block[k]) else None) for k in keys} for i in range(n)]


def parse_filings(cik: int, blocks: Iterable[dict]) -> List[Filing]:
    """blocks = filings.recent + صفحات filings.files (نفس البنية العمودية)."""
    out, seen = [], set()
    for block in blocks:
        for r in _columns(block or {}):
            acc = r.get("accessionNumber")
            if not acc or acc in seen:
                continue
            at = parse_accepted(r.get("acceptanceDateTime"), r.get("filingDate"))
            if at is None:
                continue
            seen.add(acc)
            items = tuple(x.strip() for x in str(r.get("items") or "").split(",") if x.strip())
            out.append(Filing(cik=cik, accession=acc, form=str(r.get("form") or ""), items=items,
                              filed=date.fromisoformat(r["filingDate"]) if r.get("filingDate") else at.astimezone(ET).date(),
                              accepted_raw=str(r.get("acceptanceDateTime") or ""), accepted_at=at))
    out.sort(key=lambda f: f.accepted_at)
    return out


def parse_shares(cik: int, concept: dict, filings: List[Filing]) -> List[SharesFact]:
    """الحقيقة تُعرف وقت قبول الإفصاح الذي حملها (accn)، وإلا 22:00 نيويورك من يوم filed."""
    by_acc = {f.accession: f for f in filings}
    rows = ((concept or {}).get("units") or {}).get("shares") or []
    out, seen = [], set()
    for r in rows:
        try:
            val, end, filed = float(r["val"]), date.fromisoformat(r["end"]), r.get("filed")
        except (KeyError, TypeError, ValueError):
            continue
        key = (r.get("accn"), end, val)
        if key in seen or val <= 0:
            continue
        seen.add(key)
        f = by_acc.get(r.get("accn") or "")
        known = f.known_ts if f else parse_accepted(None, filed)
        if known is None:
            continue
        out.append(SharesFact(cik=cik, value=val, period_end=end, known_ts=known,
                              split_adjusted=False, stale=False, accession=r.get("accn")))
    out.sort(key=lambda s: (s.known_ts, s.period_end))
    return out


def shares_as_of(facts: List[SharesFact], splits: List[SplitEvent], as_of: datetime) -> Optional[SharesFact]:
    """آخر حقيقة معروفة عند as_of (الأحدث period_end بين المعروف)، مصححة بالتجميعات بعد تاريخها."""
    known = [f for f in facts if f.known_ts <= as_of]
    if not known:
        return None
    best = max(known, key=lambda f: (f.period_end, f.known_ts))
    factor, adjusted = 1.0, False
    for s in splits:
        if s.ex_date > best.period_end and s.known_ts <= as_of:
            factor *= s.ratio
            adjusted = True
    age = (as_of.astimezone(ET).date() - best.period_end).days
    return SharesFact(cik=best.cik, value=best.value * factor, period_end=best.period_end, known_ts=best.known_ts,
                      split_adjusted=adjusted, stale=age > SHARES_STALE_DAYS, accession=best.accession)


def diagnose_hours(filings: Iterable[Filing]) -> dict:
    """ساعات EDGAR 06:00–22:00 نيويورك. إن وقعت الساعات المكتوبة في هذا النطاق فهي توقيت نيويورك."""
    hrs = []
    for f in filings:
        try:
            hrs.append(int(f.accepted_raw[11:13]))
        except ValueError:
            continue
    if not hrs:
        return {"n": 0, "verdict": "no data"}
    in_et = sum(6 <= h <= 22 for h in hrs) / len(hrs)
    in_utc = sum(h >= 10 or h <= 3 for h in hrs) / len(hrs)
    early = sum(6 <= h <= 9 for h in hrs) / len(hrs)
    verdict = "et_wallclock" if in_et > 0.98 and early > 0.01 else ("utc" if in_utc > 0.98 and early < 0.005 else "unclear")
    return {"n": len(hrs), "share_06_22": round(in_et, 4), "share_06_09": round(early, 4), "verdict": verdict,
            "policy": "always read as New York wall-clock (never earlier than truth)"}


# ====================================================================== الجلب (مع تخزين)
class EdgarClient:
    def __init__(self, cache_dir: str, user_agent: Optional[str] = None, per_sec: float = 6.0, session=None):
        self.ua = user_agent or os.environ.get("SEC_USER_AGENT", "")
        if "@" not in self.ua:
            raise ValueError("SEC_USER_AGENT يجب أن يحوي اسماً وبريداً (شرط SEC)")
        import requests
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": self.ua, "Accept-Encoding": "gzip, deflate"})
        self.cache_dir, self.gap, self._last = cache_dir, 1.0 / per_sec, 0.0
        self.calls = 0
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, url: str) -> str:
        return os.path.join(self.cache_dir, url.split("://", 1)[1].replace("/", "_") + ".gz")

    def get(self, url: str, max_age_h: Optional[float] = None) -> Optional[dict]:
        p = self._path(url)
        if os.path.exists(p) and (max_age_h is None or time.time() - os.path.getmtime(p) < max_age_h * 3600):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                d = json.load(f)
            return d or None
        for attempt in range(4):
            wait = self.gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            self.calls += 1
            try:
                r = self.s.get(url, timeout=30)
            except Exception:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                d = {}
                break
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt + 1)
                continue
            if r.status_code != 200:
                return None
            d = r.json()
            break
        else:
            return None
        with gzip.open(p, "wt", encoding="utf-8") as f:
            json.dump(d, f)
        return d or None

    def ticker_map(self) -> Dict[str, Tuple[int, str]]:
        d = self.get(TICKERS_URL, max_age_h=24) or {}
        return {v["ticker"].upper().replace(".", "-"): (int(v["cik_str"]), v.get("title", "")) for v in d.values()}

    def company(self, cik: int, max_age_h: float = 20) -> Tuple[List[Filing], List[SharesFact], dict]:
        sub = self.get(SUBMISSIONS_URL.format(cik=cik), max_age_h=max_age_h) or {}
        blocks = [((sub.get("filings") or {}).get("recent")) or {}]
        for page in ((sub.get("filings") or {}).get("files")) or []:
            blocks.append(self.get(SUBMISSIONS_PAGE_URL.format(name=page["name"])) or {})   # الصفحات القديمة لا تتغير
        filings = parse_filings(cik, blocks)
        concept = self.get(SHARES_URL.format(cik=cik), max_age_h=max_age_h * 7) or {}
        meta = {"name": sub.get("name"), "tickers": sub.get("tickers") or [], "former_names": sub.get("formerNames") or []}
        return filings, parse_shares(cik, concept, filings), meta
