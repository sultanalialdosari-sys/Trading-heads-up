# -*- coding: utf-8 -*-
"""سجل الفرضيات (Pre-registration) — ملف JSONL يُضاف إليه فقط: research/hypotheses.jsonl

القواعد:
  - كل فرضية تُسجَّل قبل لمس بيانات الاختبار (2023+): اسم، وصف، المعاملات، بصمة الإعدادات والكود.
  - نفس الاسم بمعاملات مختلفة = مرفوض: أي تعديل فرضية جديدة باسم جديد (فتُحسب في تصحيح تعدد المحاولات).
  - بيانات الاختبار تُلمس مرة واحدة لكل فرضية. اللمسة الثانية مرفوضة؛ النتيجة الأولى هي النتيجة.
  - الفاشلة لا تُحذف: عددها يدخل في Benjamini–Hochberg و Deflated Sharpe.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional


class RegistryError(RuntimeError):
    pass


class TestSetLocked(RegistryError):
    """لمسة ثانية لبيانات الاختبار لنفس الفرضية."""
    __test__ = False          # ليس اختبار pytest رغم أن اسمه يبدأ بـ Test


def params_hash(params: dict, config_hash: Optional[str] = None) -> str:
    blob = json.dumps({"p": params, "c": config_hash}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def research_config_hash(app) -> str:
    """بصمة الأقسام التي تؤثر على نتيجة البحث فقط (التكاليف، الخروج، التنفيذ، التحقق)، بلا معاملات
    الفرضية نفسها — فتغيير قواعد التنبيه الحي لا يكسر الفرضيات المسجلة، وتغيير التكاليف يكسرها."""
    d = app.model_dump(mode="json")
    sect = {k: d[k] for k in ("costs", "exits", "execution", "validation")}
    for k in ("stop", "target_r"):
        sect["exits"].pop(k, None)
    return hashlib.sha256(json.dumps(sect, sort_keys=True).encode()).hexdigest()[:16]


class Registry:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def events(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _append(self, ev: dict) -> dict:
        ev = {"at": datetime.now(timezone.utc).isoformat(), **ev}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        return ev

    def hypotheses(self) -> Dict[str, dict]:
        return {e["hid"]: e for e in self.events() if e["event"] == "register"}

    def register(self, hid: str, description: str, params: dict, config_hash: Optional[str] = None,
                 code_commit: Optional[str] = None) -> dict:
        h = params_hash(params, config_hash)
        old = self.hypotheses().get(hid)
        if old:
            if old["hash"] != h:
                raise RegistryError(f"الفرضية {hid} مسجلة بمعاملات أخرى. أي تعديل = فرضية جديدة باسم جديد.")
            return old
        return self._append({"event": "register", "hid": hid, "description": description, "params": params,
                             "hash": h, "config_hash": config_hash, "code_commit": code_commit})

    def test_result(self, hid: str) -> Optional[dict]:
        return next((e for e in self.events() if e["event"] == "test" and e["hid"] == hid), None)

    def touch_test(self, hid: str, params: dict, config_hash: Optional[str], result: dict) -> dict:
        """يسجل نتيجة الاختبار الوحيدة. يُستدعى بعد الحساب مباشرة، والنتيجة لا تُستبدل."""
        reg = self.hypotheses().get(hid)
        if not reg:
            raise RegistryError(f"الفرضية {hid} غير مسجلة: سجّلها قبل لمس بيانات الاختبار.")
        if reg["hash"] != params_hash(params, config_hash):
            raise RegistryError(f"معاملات {hid} تغيّرت بعد التسجيل.")
        if self.test_result(hid):
            raise TestSetLocked(f"بيانات الاختبار لُمست سابقاً للفرضية {hid}. النتيجة الأولى هي النتيجة.")
        return self._append({"event": "test", "hid": hid, "hash": reg["hash"], "result": result})

    def test_pvalues(self) -> Dict[str, Optional[float]]:
        """p لكل فرضية مسجلة (None إن لم تُختبر بعد) — لتصحيح تعدد المحاولات."""
        tests = {e["hid"]: e["result"].get("p_bootstrap") for e in self.events() if e["event"] == "test"}
        return {hid: tests.get(hid) for hid in self.hypotheses()}

    @property
    def n_trials(self) -> int:
        return len(self.hypotheses())
