# -*- coding: utf-8 -*-
"""ملف الإعدادات المركزي (config.json v3) والتحقق منه.

ملف واحد يقرؤه النظام الحي والبحث معاً:
  live        — قواعد التنبيه الحالية كما هي (نفس القيم السابقة، فلا يتغير سلوك النظام).
  universe / filters / exits / costs / execution / validation — إعدادات خط البحث.
  paper       — حجم الصفقة الورقية، والأوامر الحقيقية ممنوعة دائماً.

الحراس (يتوقف التشغيل إذا خُرق أي منها):
  - paper.live_orders يجب أن تكون false.
  - بداية الاختبار بعد نهاية التدريب + فترة العزل.
  - لا مفاتيح أو أسرار داخل الملف (الأسرار مكانها GitHub Secrets فقط).
  - أي مفتاح غير معروف = خطأ إملائي محتمل = رفض.

القيم الافتراضية أمثلة بدائية للانطلاق، لا نتائج مثبتة.
"""
import hashlib
import json
import os
import re
from datetime import date
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hu import extended as X
from hu import rules as RL

VERSION = 3


class ConfigError(ValueError):
    """إعدادات مرفوضة. الرسالة تشرح السبب بالعربي."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- live (النظام الحالي)
class Live(_Strict):
    # قواعد الجلسة (hu/rules.py)
    max_price: float = RL.DEFAULTS["max_price"]
    min_price: float = RL.DEFAULTS["min_price"]
    min_dollar_volume: float = RL.DEFAULTS["min_dollar_volume"]
    max_day_change_pct: float = RL.DEFAULTS["max_day_change_pct"]
    min_rvol: float = RL.DEFAULTS["min_rvol"]
    spike_mult: float = RL.DEFAULTS["spike_mult"]
    window_bars: int = RL.DEFAULTS["window_bars"]
    max_range_atr: float = RL.DEFAULTS["max_range_atr"]
    max_range_pct: float = RL.DEFAULTS["max_range_pct"]
    near_res_min_pct: float = RL.DEFAULTS["near_res_min_pct"]
    near_res_max_pct: float = RL.DEFAULTS["near_res_max_pct"]
    max_spread_pct: float = RL.DEFAULTS["max_spread_pct"]
    min_score: int = RL.DEFAULTS["min_score"]
    target_r: float = RL.DEFAULTS["target_r"]
    slippage_pct: float = RL.DEFAULTS["slippage_pct"]
    # ما قبل الافتتاح وما بعد الإغلاق (hu/extended.py)
    ext_min_change_pct: float = X.EXT_DEFAULTS["ext_min_change_pct"]
    pre_min_dollar_volume: float = X.EXT_DEFAULTS["pre_min_dollar_volume"]
    post_min_dollar_volume: float = X.EXT_DEFAULTS["post_min_dollar_volume"]
    ext_max_from_high_pct: float = X.EXT_DEFAULTS["ext_max_from_high_pct"]
    ext_min_score: int = X.EXT_DEFAULTS["ext_min_score"]
    ext_shortlist: int = X.EXT_DEFAULTS["ext_shortlist"]

    @model_validator(mode="after")
    def _check(self):
        if not 0 < self.min_price < self.max_price:
            raise ValueError("live: يجب أن يكون 0 < min_price < max_price")
        if self.near_res_min_pct >= self.near_res_max_pct:
            raise ValueError("live: near_res_min_pct يجب أن يكون أصغر من near_res_max_pct")
        if self.target_r <= 0 or self.window_bars < 2:
            raise ValueError("live: target_r > 0 و window_bars ≥ 2")
        return self


# ---------------------------------------------------------------- البحث
class Range(_Strict):
    min: float
    max: float

    @model_validator(mode="after")
    def _ordered(self):
        if self.min >= self.max:
            raise ValueError(f"النطاق غير صحيح: min={self.min} ≥ max={self.max}")
        return self


class Universe(_Strict):
    exchanges: List[Literal["NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"]] = ["NASDAQ", "NYSE", "AMEX"]
    include_inactive: bool = True          # بدونها = انحياز للناجين
    price_raw: Range = Range(min=0.20, max=5.00)
    min_dollar_volume_20d: float = 250_000
    exclude_types: List[str] = ["ETF", "warrant", "unit", "right"]

    @field_validator("include_inactive")
    @classmethod
    def _survivorship(cls, v):
        if not v:
            raise ValueError("universe.include_inactive=false يُدخل انحياز الناجين؛ غير مسموح")
        return v


class Rvol(_Strict):
    min: float = 3.0
    lookback_days: int = Field(20, ge=5, le=60)
    time_of_day_matched: bool = True


class SharesOut(_Strict):
    max: Optional[float] = 50_000_000
    stale_days: int = Field(400, ge=30)
    allow_unknown: bool = True


class News(_Strict):
    required: bool = False
    max_age_min: int = Field(120, ge=1)


class Filters(_Strict):
    rvol: Rvol = Rvol()
    gap_pct: Range = Range(min=5.0, max=80.0)
    shares_out: SharesOut = SharesOut()
    sec_block_forms: List[str] = ["S-1", "S-3", "424B3", "424B4", "424B5"]
    sec_block_items: List[str] = ["1.03", "3.01"]
    sec_block_window_days: int = Field(5, ge=0)
    news: News = News()
    one_signal_per_symbol_day: bool = True
    entry_window_et: Tuple[str, str] = ("09:45", "15:00")

    @field_validator("entry_window_et")
    @classmethod
    def _window(cls, v):
        for t in v:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"وقت غير صالح: {t} (الصيغة HH:MM)")
        if not ("09:30" <= v[0] < v[1] <= "16:00"):
            raise ValueError("نافذة الدخول يجب أن تكون داخل الجلسة 09:30–16:00 وبترتيب صحيح")
        return v


class Stop(_Strict):
    type: Literal["atr", "pct"] = "atr"
    atr_mult: float = Field(1.5, gt=0)
    min_pct: float = Field(4.0, gt=0)


class Exits(_Strict):
    stop: Stop = Stop()
    target_r: List[float] = [1.5, 2.0, 3.0]
    time_exit_et: str = "15:55"
    same_bar_rule: Literal["stop_first"] = "stop_first"      # الافتراض المتحفظ الوحيد المسموح
    gap_rule: Literal["fill_at_open"] = "fill_at_open"

    @field_validator("target_r")
    @classmethod
    def _targets(cls, v):
        if not v or any(x <= 0 for x in v):
            raise ValueError("exits.target_r: قائمة قيم موجبة")
        return v


class PriceBuckets(_Strict):
    lt_1: float = Field(alias="lt_1")
    to_5: float = Field(alias="1_to_5")
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Ticks(_Strict):
    lt_1: float = 0.0001
    ge_1: float = 0.01


class LiquidityMult(_Strict):
    dv_lt_250k: float = Field(1.5, ge=1)
    rvol_lt_2: float = Field(1.3, ge=1)


class Fees(_Strict):
    """رسوم تنظيمية على البيع فقط (العمولة في Alpaca صفر). القيم الحالية تُطبق على كل التاريخ — تبسيط أثره
    نحو 0.01% من الصفقة. المصادر: SEC Fee Rate Advisory FY2026، جدول رسوم FINRA 2026."""
    sec_per_million: float = Field(20.60, ge=0)
    taf_per_share: float = Field(0.000195, ge=0)
    taf_max: float = Field(9.79, ge=0)


class Costs(_Strict):
    fees: Fees = Fees()
    scenarios: Dict[str, float] = {"optimistic": 0.003, "base": 0.005, "pessimistic": 0.010}
    judge_on: str = "base"
    spread_floor: PriceBuckets = PriceBuckets(lt_1=0.010, to_5=0.003)
    slip_cap_per_side: PriceBuckets = PriceBuckets(lt_1=0.010, to_5=0.005)
    liquidity_mult: LiquidityMult = LiquidityMult()
    tick: Ticks = Ticks()

    @model_validator(mode="after")
    def _check(self):
        s = self.scenarios
        need = {"optimistic", "base", "pessimistic"}
        if set(s) != need:
            raise ValueError(f"costs.scenarios يجب أن تحوي بالضبط: {sorted(need)}")
        if not (0 <= s["optimistic"] <= s["base"] <= s["pessimistic"] < 0.2):
            raise ValueError("costs.scenarios: متفائل ≤ أساسي ≤ متشائم (كنسب، مثل 0.005 = 0.5%)")
        if self.judge_on not in s:
            raise ValueError("costs.judge_on يجب أن يكون أحد السيناريوهات")
        if self.judge_on == "optimistic":
            raise ValueError("الحكم على السيناريو المتفائل ممنوع")
        return self


class Execution(_Strict):
    bar_tf: List[Literal["1Min", "5Min"]] = ["1Min", "5Min"]
    fill: Literal["next_bar_open"] = "next_bar_open"        # الإشارة على t تُنفذ على t+1
    halt_gap_min: int = Field(5, ge=1)
    no_entry_bars_after_halt: int = Field(2, ge=0)


class WalkForward(_Strict):
    train_months: int = Field(24, ge=6)
    test_months: int = Field(6, ge=1)


class Bootstrap(_Strict):
    n: int = Field(10_000, ge=1000)
    block: Literal["day"] = "day"
    seed: int = 7


class Validation(_Strict):
    train: Tuple[date, date] = (date(2016, 1, 1), date(2022, 12, 31))
    test_from: date = date(2023, 1, 1)
    embargo_days: int = Field(5, ge=0)
    walk_forward: WalkForward = WalkForward()
    bootstrap: Bootstrap = Bootstrap()
    alpha: float = Field(0.05, gt=0, le=0.1)
    multiple_testing: Literal["benjamini_hochberg", "bonferroni"] = "benjamini_hochberg"
    min_trades: int = Field(100, ge=30)
    min_days: int = Field(60, ge=20)
    max_free_params: int = Field(4, ge=1, le=6)

    @model_validator(mode="after")
    def _split(self):
        a, b = self.train
        if a >= b:
            raise ValueError("validation.train: البداية بعد النهاية")
        if self.test_from <= b:
            raise ValueError("validation.test_from يجب أن يأتي بعد نهاية التدريب — وإلا تسرّب")
        return self

    def train_cutoff(self, trading_days: List[date]) -> date:
        """آخر يوم تدريب فعلي بعد العزل: تُحذف آخر embargo_days أيام تداول قبل نهاية التدريب،
        حتى لا تمتد نوافذ الميزات أو الصفقات المفتوحة من التدريب إلى فترة الاختبار."""
        days = sorted(d for d in trading_days if self.train[0] <= d <= self.train[1])
        if len(days) <= self.embargo_days:
            raise ValueError("أيام التدريب أقل من فترة العزل")
        return days[-1 - self.embargo_days] if self.embargo_days else days[-1]


class Paper(_Strict):
    shares_per_trade: int = Field(RL.DEFAULTS["shares_per_trade"], ge=1)
    live_orders: Literal[False] = False


class AppConfig(_Strict):
    version: Literal[3] = 3
    timezone_display: str = "Asia/Riyadh"
    live: Live = Live()
    universe: Universe = Universe()
    filters: Filters = Filters()
    exits: Exits = Exits()
    costs: Costs = Costs()
    execution: Execution = Execution()
    validation: Validation = Validation()
    paper: Paper = Paper()

    def live_dict(self) -> dict:
        """القاموس المسطح الذي يستخدمه النظام الحي (نفس الشكل القديم)."""
        d = self.live.model_dump()
        d["shares_per_trade"] = self.paper.shares_per_trade
        d["fees"] = self.costs.fees.model_dump()        # رسوم البيع النظامية في الصفقات الورقية
        return d


# ---------------------------------------------------------------- الحراس
SECRET_NAME = re.compile(r"(secret|api_?key|token|password|passwd|private)", re.I)
SECRET_VALUE = re.compile(r"^(PK|AK|CK)[A-Z0-9]{14,}$|^[A-Za-z0-9/+]{32,}$")


def _scan_secrets(obj: Any, path: str = "") -> List[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if SECRET_NAME.search(str(k)):
                found.append(p)
            found += _scan_secrets(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _scan_secrets(v, f"{path}[{i}]")
    elif isinstance(obj, str) and SECRET_VALUE.match(obj):
        found.append(path)
    return found


def _upgrade(raw: dict) -> dict:
    """الملف القديم (v1 مسطح) ← v3: المفاتيح المسطحة تذهب إلى live، و shares_per_trade إلى paper."""
    if "version" in raw:
        return raw
    raw = dict(raw)
    paper = {}
    if "shares_per_trade" in raw:
        paper["shares_per_trade"] = raw.pop("shares_per_trade")
    return {"version": VERSION, "live": raw, **({"paper": paper} if paper else {})}


def _fmt_errors(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"])
        msg = err["msg"].replace("Value error, ", "")
        if err["type"] == "extra_forbidden":
            msg = "مفتاح غير معروف (خطأ إملائي؟)"
        elif err["type"] == "literal_error" and loc.endswith("live_orders"):
            msg = "الأوامر الحقيقية ممنوعة: live_orders يجب أن تكون false"
        lines.append(f"  - {loc or '(الملف)'}: {msg}")
    return "\n".join(lines)


def parse(raw: dict) -> AppConfig:
    leaks = _scan_secrets(raw)
    if leaks:
        raise ConfigError("config.json يحوي ما يبدو مفتاحاً أو سراً في: " + ", ".join(leaks)
                          + " — الأسرار مكانها GitHub Secrets فقط.")
    try:
        return AppConfig.model_validate(_upgrade(raw))
    except ValidationError as e:
        raise ConfigError("config.json مرفوض:\n" + _fmt_errors(e)) from None


def file_hash(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def load(path: str) -> AppConfig:
    if not os.path.exists(path):
        return AppConfig()
    with open(path, encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"config.json ليس JSON صالحاً (سطر {e.lineno}): {e.msg}") from None
    return parse(raw)
