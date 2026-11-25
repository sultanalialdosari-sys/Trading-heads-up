# -*- coding: utf-8 -*-
"""عرض الوقت بنظام 12 ساعة (ص/م) في كل الواجهات والرسائل."""
from datetime import datetime


def h12(d: datetime, seconds: bool = False) -> str:
    h = d.hour
    s = f"{h % 12 or 12}:{d.minute:02d}" + (f":{d.second:02d}" if seconds else "")
    return s + (" ص" if h < 12 else " م")


def dh12(d: datetime, seconds: bool = False, year: bool = False) -> str:
    return d.strftime("%Y-%m-%d " if year else "%m-%d ") + h12(d, seconds)
