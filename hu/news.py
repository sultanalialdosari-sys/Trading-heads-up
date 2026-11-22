# -*- coding: utf-8 -*-
"""تمييز الخبر الجوهري من الترويجي — قواعد كلمات واضحة (بدون ذكاء اصطناعي)، وقد تخطئ.

جوهري: حدث له أثر محدد وقابل للتحقق (أرقام، موافقة، عقد بقيمة، نتائج، استحواذ)، ويقوى إذا صاحبه 8-K.
ترويجي: لغة تسويقية أو نوايا بلا أرقام ("يستكشف"، "يخطط"، "رؤية"، "ثوري")، أو مقال رأي/ترويج مدفوع.
"""
import re
from typing import List, Optional

import intraday as I

MATERIAL = re.compile(
    r"\b(fda (approval|approves|clearance|grants)|approved by|phase [123i]+ (results|data)|topline|"
    r"definitive agreement|to be acquired|acquisition of|merger agreement|"
    r"(reports|announces) (first|second|third|fourth|q[1-4]|fiscal|full[- ]year).{0,30}results|revenue (of|increased|grew)|"
    r"awarded .{0,40}\$\s?\d|contract (worth|valued|of) .{0,20}\$\s?\d|\$\s?\d[\d.,]*\s?(million|billion|m|bn)\b|"
    r"purchase order|patent (granted|issued)|uplist\w*)",
    re.I)
PROMO = re.compile(
    r"\b(explor\w+|plans? to|intends? to|aims? to|seeks? to|poised|revolutionary|game[- ]chang\w+|"
    r"disrupt\w*|vision|letter to shareholders|shareholder (letter|update)|ceo (interview|letter)|"
    r"featured|spotlight|sponsored|paid (promotion|advertisement)|investor (presentation|conference)|"
    r"to present at|participate in .{0,30}conference|announces? (launch|rebrand)|memorandum of understanding|mou|"
    r"non-binding|letter of intent|loi|strategic (review|alternatives)|ai[- ]powered|blockchain|crypto treasury)\b",
    re.I)


def classify(item: dict, recent_8k_days: Optional[float] = None) -> str:
    """يرجع: RISK / MATERIAL / PROMO / OTHER"""
    tags = item.get("tags") or I.classify_news(item)
    if set(tags) & I.RISK_TAGS:
        return "RISK"
    text = f"{item.get('headline', '')} {item.get('summary', '')}"
    m, p = bool(MATERIAL.search(text)), bool(PROMO.search(text))
    if m and not p:
        return "MATERIAL"
    if m and p:   # أرقام مع لغة تسويقية: جوهري فقط إذا صاحبه 8-K حديث
        return "MATERIAL" if recent_8k_days is not None and recent_8k_days <= 2 else "PROMO"
    if p:
        return "PROMO"
    if set(tags) & {"FDA", "MNA", "EARNINGS", "CONTRACT"} and recent_8k_days is not None and recent_8k_days <= 2:
        return "MATERIAL"
    return "OTHER"


LABEL = {"RISK": "⚠️ خبر مخاطرة", "MATERIAL": "✅ خبر جوهري", "PROMO": "📣 خبر ترويجي", "OTHER": "📰 خبر عادي"}


def summarize(news: Optional[List[dict]], recent_8k_days: Optional[float]) -> dict:
    if news is None:
        return {"available": False, "best": None, "items": [], "kinds": []}
    items = []
    for n in news[:8]:
        k = classify(n, recent_8k_days)
        items.append({"kind": k, "headline": n.get("headline", ""), "time": n["time_utc"].isoformat() if n.get("time_utc") else "", "source": n.get("source", "")})
    kinds = [x["kind"] for x in items]
    best = "RISK" if "RISK" in kinds else "MATERIAL" if "MATERIAL" in kinds else "PROMO" if "PROMO" in kinds else ("OTHER" if kinds else None)
    return {"available": True, "best": best, "items": items, "kinds": kinds}
