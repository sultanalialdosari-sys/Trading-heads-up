# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)

import intraday as I  # noqa: E402
import report  # noqa: E402
from fake_market import FakeWorld, et, TODAY  # noqa: E402
from hu import news as N, site, trades as TR  # noqa: E402
from hu.engine import Engine, load_cfg  # noqa: E402
from hu.sec import SecClient, SecInfo, evaluate_filings, parse_shares  # noqa: E402
from hu.store import Store  # noqa: E402

UTC = timezone.utc
CFG = load_cfg(os.path.join(os.path.dirname(HERE), "config.json"))
EXT = {**CFG, "extended_trades": True}      # صفقات قوائم ما قبل/بعد الجلسة (موقوفة افتراضياً)


def run_at(w, st, hm, kind="scan", sec_ua="Test t@example.com", cfg=None):  # noqa
    w.now = et(TODAY, *hm)
    w.tg.clear()
    c = I.AlpacaClient("KEY_SHOULD_NOT_LEAK", "SECRET_SHOULD_NOT_LEAK", I.Config(), session=w)
    c.rl = I.RateLimiter(100000)
    e = Engine(c, st, cfg or CFG, SecClient(sec_ua, session=w, max_rps=1000), I.TelegramNotifier("TGTOKEN_SECRET", "1", session=w))
    return e.run(kind), list(w.tg)


@pytest.fixture(scope="module")
def day():
    tmp = tempfile.mkdtemp()
    st = Store(os.path.join(tmp, "h.db"))
    w = FakeWorld()
    log = {}
    for hm in [(12, 17), (13, 17), (14, 17), (15, 17)]:
        log[hm] = run_at(w, st, hm)
    log["final"] = run_at(w, st, (16, 40), "final")
    return st, w, log, tmp


def test_only_early_setup_alerts_once(day):
    st, w, log, _ = day
    al = st.q("SELECT * FROM alerts")
    assert [a["symbol"] for a in al] == ["EARL"]
    first = log[(12, 17)][1]
    assert len(first) == 1 and first[0].startswith("🔔 إشارة مبكرة — EARL")
    for k in ("لم يحدث اختراق بعد", "شرط الدخول", "إبطال الفكرة", "Float: غير متوفر", "جودة البيانات",
              "متأخرة 15 دقيقة", "IEX", "ليست توصية شراء", "41.5M"):
        assert k in first[0], k
    # لا تكرار في الفحوص اللاحقة
    later = [m for hm in [(13, 17), (14, 17), (15, 17)] for m in log[hm][1] if m.startswith("🔔")]
    assert later == []


def test_dilution_excluded_with_reason(day):
    st, *_ = day
    c = st.q("SELECT data FROM candidates WHERE symbol='DILU' ORDER BY run_id LIMIT 1")
    d = json.loads(c[0]["data"])
    assert d["excluded"] and "424B5" in d["excluded"][0]
    assert any("ترويجي" in r for r in d["risks"])


def test_followup_activation_once_and_lead(day):
    st, w, log, _ = day
    a = st.one("SELECT * FROM alerts WHERE symbol='EARL'")
    assert a["state"] == "ACTIVATED" and 20 <= a["lead_min"] <= 30
    acts = [m for hm in log for m in log[hm][1] if m.startswith("✅ تفعّلت")]
    assert len(acts) == 1


def test_virtual_trades_and_baseline(day):
    st, *_ = day
    tr = st.q("SELECT * FROM trades")
    A = [t for t in tr if t["kind"] == "A"][0]
    B = [t for t in tr if t["kind"] == "B"][0]
    assert A["shares"] == 500 and A["status"] == "TARGET" and A["entry"] > A["stop"]
    assert B["status"] in ("TARGET", "TIME") and B["pnl"] > 0 and B["entry"] >= 2.0 and B["entry_utc"] > A["entry_utc"]
    assert any(t["kind"] == "BASE" for t in tr)
    assert all(t["status"] not in ("OPEN", "PENDING") for t in tr)   # كلها أُغلقت بنهاية اليوم


def test_missed_reports_late_scan(day):
    st, *_ = day
    m = st.one("SELECT * FROM missed WHERE symbol='RUNR'")
    assert m and m["max_gain_pct"] >= 30 and "بين فحصين" in m["reason"]


def test_daily_summary_and_site_and_report(day):
    st, w, log, tmp = day
    fin = log["final"][1]
    assert any(m.startswith("📋 ملخص") for m in fin)
    out = os.path.join(tmp, "docs", "index.html")
    site.build(st, out, CFG)
    html = open(out, encoding="utf-8").read()
    for k in ("EARL", "فاتت", "RUNR", "المقارنة", "التشغيلات", "متأخر 15 دقيقة"):
        assert k in html
    rep = report.build(st, TODAY, TODAY)
    assert "تفعّلت" in rep and "**1**" in rep


def test_no_secrets_no_orders(day):
    st, w, log, tmp = day
    assert not any("/orders" in u for u, _ in w.calls)
    blob = json.dumps([m for hm in log for m in log[hm][1]], ensure_ascii=False)
    blob += open(os.path.join(tmp, "docs", "index.html"), encoding="utf-8").read() if os.path.exists(os.path.join(tmp, "docs", "index.html")) else ""
    for s in ("KEY_SHOULD_NOT_LEAK", "SECRET_SHOULD_NOT_LEAK", "TGTOKEN_SECRET"):
        assert s not in blob
    import sqlite3
    raw = open(st.path, "rb").read()
    for s in (b"SECRET_SHOULD_NOT_LEAK", b"TGTOKEN_SECRET"):
        assert s not in raw


def test_market_closed_does_nothing():
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld()
    info, tg = run_at(w, st, (8, 17))
    assert info["market_open"] == 0 and info["swept"] == 0 and tg == []


def test_sip_not_allowed_falls_back_and_says_so():
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld(sip_allowed=False)
    info, tg = run_at(w, st, (12, 17))
    assert "IEX فقط" in (info.get("feed_note") or "")


def test_sec_missing_user_agent_is_reported():
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld()
    info, tg = run_at(w, st, (12, 17), sec_ua="")
    if tg:
        assert "إفصاحات SEC" in tg[0]   # تُذكر كبيانة غير متوفرة
    c = json.loads(st.q("SELECT data FROM candidates WHERE symbol='EARL'")[0]["data"])
    assert "إفصاحات SEC" in c["missing"]


# ---------------------------------------------------------------- وحدات
def test_sec_no_lookahead_and_rules():
    now = datetime(2026, 11, 20, 17, 0, tzinfo=UTC)
    rec = {"form": ["424B5", "424B5", "S-1", "8-K", "25-NSE"],
           "acceptanceDateTime": [(now + timedelta(days=1)).isoformat(), (now - timedelta(days=30)).isoformat(),
                                  (now - timedelta(days=10)).isoformat(), (now - timedelta(days=5)).isoformat(),
                                  (now - timedelta(days=400)).isoformat()],
           "filingDate": [""] * 5, "items": ["", "", "", "3.01", ""]}
    si = SecInfo("X")
    evaluate_filings(si, {"filings": {"recent": rec}}, now)
    assert si.exclude == []                                   # 424B5 المستقبلي متجاهَل، و25-NSE قديم
    texts = " ".join(m for _, m in si.penalties)
    assert "424B5" in texts and "S-1" in texts and "3.01" in texts
    parse_shares(si, {"units": {"shares": [{"end": "2026-09-30", "val": 5, "filed": "2026-12-01"},
                                           {"end": "2026-06-30", "val": 3, "filed": "2026-08-01"}]}}, now)
    assert si.shares_out == 3


def test_news_material_vs_promo():
    assert N.classify({"headline": "ABC awarded $25 million contract", "tags": []}) == "MATERIAL"
    assert N.classify({"headline": "ABC explores revolutionary blockchain vision", "tags": []}) == "PROMO"
    assert N.classify({"headline": "ABC announces $5 million registered direct offering", "tags": ["OFFERING"]}) == "RISK"
    assert N.classify({"headline": "ABC to present at investor conference", "tags": []}) == "PROMO"


def _bars(rows, start):
    idx = [start + timedelta(minutes=i) for i in range(len(rows))]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(idx))


def _t(kind, **kw):
    base = {"kind": kind, "day": TODAY, "created_utc": et(TODAY, 11, 0).isoformat(), "status": "PENDING" if kind == "B" else "OPEN",
            "entry": None if kind == "B" else 2.0, "stop": 1.9, "target": None if kind == "B" else 2.2, "trigger": 2.1, "shares": 500}
    base.update(kw)
    return base


def test_sim_gap_stop_same_bar_and_b_rules():
    s = et(TODAY, 11, 1)
    cut = et(TODAY, 15, 0)
    gap = TR.simulate(_t("A"), _bars([[1.99, 2.0, 1.98, 1.99], [1.85, 1.86, 1.80, 1.82]], s), cut, 0.5, 2.0)
    assert gap["status"] == "STOP" and gap["exit"] == 1.85 and gap["pnl"] < -50
    both = TR.simulate(_t("A"), _bars([[2.0, 2.25, 1.89, 2.1]], s), cut, 0.5, 2.0)
    assert both["status"] == "STOP" and both["exit"] == 1.9
    canc = TR.simulate(_t("B"), _bars([[2.0, 2.05, 1.88, 1.9]], s), cut, 0.5, 2.0)
    assert canc["status"] == "CANCELLED" and canc.get("pnl") is None
    ok = TR.simulate(_t("B"), _bars([[2.05, 2.12, 2.04, 2.11], [2.11, 2.6, 2.1, 2.5]], s), cut, 0.5, 2.0)
    assert ok["status"] == "TARGET" and ok["entry"] == 2.12   # 2.1 × 1.005 = 2.1105 ← يُقرّب للسنت الأعلى
    exp = TR.simulate(_t("B"), _bars([[2.0, 2.05, 1.95, 2.0]], s), et(TODAY, 16, 0), 0.5, 2.0)
    assert exp["status"] == "EXPIRED"
    tm = TR.simulate(_t("A"), _bars([[2.0, 2.05, 1.95, 2.03]], s), et(TODAY, 16, 0), 0.5, 2.0)
    assert tm["status"] == "TIME" and tm["exit"] == 2.03
    # لا نظر للمستقبل: شموع قبل لحظة الإنشاء لا تُستخدم
    early = TR.simulate(_t("A"), _bars([[1.5, 1.5, 1.5, 1.5]], et(TODAY, 10, 0)), cut, 0.5, 2.0)
    assert early["status"] == "OPEN"


def test_ticks():
    from hu.rules import tick_up, tick_down
    assert tick_up(1.9134) == 1.92 and tick_down(1.8889) == 1.88 and tick_up(0.45671) == 0.4568 and tick_up(2.0) == 2.0


def test_session_start_no_alert_and_schedule():
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld()
    info, tg = run_at(w, st, (9, 47))
    assert info["alerts"] == 0 and not any(m.startswith("🔔") for m in tg)
    import run as R
    now = datetime(2026, 11, 20, 15, 40, tzinfo=UTC)
    assert R.scheduled_time(now, [2, 17, 32, 47]) == datetime(2026, 11, 20, 15, 32, tzinfo=UTC)
    assert R.scheduled_time(datetime(2026, 11, 20, 15, 1, tzinfo=UTC), [2, 17, 32, 47]) == datetime(2026, 11, 20, 14, 47, tzinfo=UTC)


# ---------------------------------------------------------------- ما قبل الافتتاح / بعد الإغلاق
def test_premarket_watchlist_alert_and_session_outcome():
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld()
    info, tg = run_at(w, st, (9, 2), kind="pre", cfg=EXT)
    pre = st.q("SELECT * FROM alerts WHERE kind='PRE'")
    assert [a["symbol"] for a in pre] == ["PREM"], (info, pre)
    a = pre[0]
    assert a["eval_from"].startswith("2026-11-20T14:30") and a["trigger"] > 2.6 and a["invalidation"] < a["trigger"]
    assert tg and tg[0].startswith("🌅 قائمة مراقبة قبل الافتتاح — PREM") and "الرياض" in tg[0] and "نيويورك" not in tg[0]
    # تكرار الفحص قبل الافتتاح لا يكرر التنبيه
    run_at(w, st, (9, 17), kind="pre")
    assert len(st.q("SELECT * FROM alerts WHERE kind='PRE'")) == 1
    # الجلسة: يخترق قمة ما قبل الافتتاح — تُحسب من 9:30 فقط
    run_at(w, st, (11, 2))
    a = st.one("SELECT * FROM alerts WHERE kind='PRE'")
    t = st.one("SELECT * FROM trades WHERE kind='PRE'")
    assert a["state"] == "ACTIVATED" and a["activated_utc"] >= et(TODAY, 9, 30).isoformat()
    assert t["status"] in ("OPEN", "TARGET") and t["entry_utc"] >= et(TODAY, 9, 30).isoformat()


def test_afterhours_watchlist_targets_next_session():
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld()
    info, tg = run_at(w, st, (17, 2), kind="post", cfg=EXT)
    post = st.q("SELECT * FROM alerts WHERE kind='POST'")
    assert [a["symbol"] for a in post] == ["AFTR"], info
    assert post[0]["day"] == "2026-11-23" and post[0]["eval_from"].startswith("2026-11-23T14:30")
    tr = st.one("SELECT * FROM trades WHERE kind='POST'")
    assert tr["status"] == "PENDING" and tr["day"] == "2026-11-23"
    assert tg[0].startswith("🌙 قائمة مراقبة بعد الإغلاق — AFTR")


def test_follow_skips_recent_sip_requests():
    """التنبيه الجديد في نفس الفحص لا يطلب بيانات السوق الكامل الحديثة (كانت تسبب خطأ)."""
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld()
    info, _ = run_at(w, st, (12, 17))
    assert "follow" not in (info.get("errors") or "")


def test_loop_schedule_helpers():
    import loop as L
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    t = datetime(2026, 11, 23, 10, 3, tzinfo=ny)
    assert L.phase_of(t, (9, 30), (16, 0)) == "REG" and L.next_slot(t, "REG").minute == 7
    t = datetime(2026, 11, 23, 8, 3, tzinfo=ny)
    assert L.phase_of(t, (9, 30), (16, 0)) == "PRE" and L.next_slot(t, "X").minute == 17
    assert L.phase_of(datetime(2026, 11, 27, 14, 0, tzinfo=ny), (9, 30), (13, 0)) == "POST"   # إغلاق مبكر
    assert L.phase_of(datetime(2026, 11, 23, 20, 5, tzinfo=ny), (9, 30), (16, 0)) == "DONE"


def test_site_shows_riyadh_time_and_ext_section(day):
    st, w, log, tmp = day
    out = os.path.join(tmp, "docs2", "index.html")
    site.build(st, out, CFG)
    html = open(out, encoding="utf-8").read()
    assert "قوائم ما قبل الافتتاح وبعد الإغلاق" in html and "(الرياض)" in html
    import re
    visible = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    assert "نيويورك" not in visible


def test_follow_records_last_price_and_calendar_shows_open():
    """التقويم يتحدث مع كل صفقة: المفتوحة تظهر بربحها غير المحقق من آخر سعر تابعه النظام."""
    tmp = tempfile.mkdtemp()
    st = Store(os.path.join(tmp, "h.db"))
    w = FakeWorld()
    run_at(w, st, (12, 17))
    run_at(w, st, (13, 17))
    opn = st.q("SELECT * FROM trades WHERE status='OPEN'")
    assert opn and all(t["last_px"] and t["last_utc"] for t in opn)
    assert all(t["last_utc"] < w.now.astimezone(UTC).isoformat() for t in opn)        # لا سعر من بعد لحظة القطع
    html = site.calendar_html(st.q("SELECT * FROM trades"))
    assert "مفتوحة" in html and "live" in html


def test_calendar_open_vs_closed_and_signature():
    import loop
    trades = [
        {"day": "2026-11-25", "kind": "A", "status": "OPEN", "entry": 2.0, "last_px": 2.1, "shares": 500, "pnl": None},
        {"day": "2026-11-25", "kind": "B", "status": "TARGET", "entry": 2.0, "shares": 500, "pnl": 60.0},
        {"day": "2026-11-25", "kind": "BASE", "status": "PENDING", "entry": None, "shares": 500, "pnl": None},
    ]
    html = site.calendar_html(trades)
    assert "+50$" in html and "1 مفتوحة" in html and "+60.00$" in html            # المفتوحة لا تدخل الصافي
    assert "مفتوحة الآن 1" in html
    tmp = tempfile.mkdtemp()
    st = Store(os.path.join(tmp, "h.db"))
    s0 = loop.trade_signature(st)
    tid = st.insert("trades", {"day": "2026-11-25", "kind": "A", "symbol": "X", "status": "OPEN", "entry": 1.0, "stop": 0.9})
    s1 = loop.trade_signature(st)
    st.update("trades", tid, status="STOP", pnl=-50.0)
    s2 = loop.trade_signature(st)
    assert s0 != s1 != s2


def test_halts_parse_real_shape():
    from hu import halts as HL
    xml = ('<?xml version="1.0"?><rss xmlns:ndaq="http://www.nasdaqtrader.com/"><channel><item><title>ABCD</title>'
           '<ndaq:HaltDate>11/25/2026</ndaq:HaltDate><ndaq:HaltTime>10:31:05.120</ndaq:HaltTime><ndaq:IssueSymbol>ABCD</ndaq:IssueSymbol>'
           '<ndaq:IssueName>Abcd Inc</ndaq:IssueName><ndaq:Market>NASDAQ</ndaq:Market><ndaq:ReasonCode>LUDP</ndaq:ReasonCode>'
           '<ndaq:PauseThresholdPrice>1.2345</ndaq:PauseThresholdPrice><ndaq:ResumptionDate>11/25/2026</ndaq:ResumptionDate>'
           '<ndaq:ResumptionQuoteTime>10:31:05</ndaq:ResumptionQuoteTime><ndaq:ResumptionTradeTime>10:36:05</ndaq:ResumptionTradeTime>'
           '</item><item><ndaq:HaltDate>11/25/2026</ndaq:HaltDate><ndaq:HaltTime>11:00:00</ndaq:HaltTime>'
           '<ndaq:IssueSymbol>NEWS</ndaq:IssueSymbol><ndaq:ReasonCode>T1</ndaq:ReasonCode><ndaq:ResumptionTradeTime></ndaq:ResumptionTradeTime>'
           '</item></channel></rss>')
    h = HL.parse(xml)
    assert [x.symbol for x in h] == ["ABCD", "NEWS"]
    a, n = h
    assert (a.reason, a.halt_date, a.halt_time, a.pause_price, a.resume_trade) == ("LUDP", "2026-11-25", "10:31:05", 1.2345, "10:36:05")
    assert a.volatility and "LULD" in a.reason_ar and n.reason_ar == "بانتظار خبر"
    ny = HL.NY
    assert a.active(datetime(2026, 11, 25, 10, 33, tzinfo=ny)) and not a.active(datetime(2026, 11, 25, 10, 37, tzinfo=ny))
    assert not a.active(datetime(2026, 11, 25, 10, 30, tzinfo=ny))                   # قبل التوقف
    assert n.active(datetime(2026, 11, 25, 15, 0, tzinfo=ny))                          # بلا وقت استئناف = ما زال موقوفاً
    assert HL.parse("not xml") == []


def test_halted_stock_gets_no_alert_and_followed_stock_gets_halt_message():
    tmp = tempfile.mkdtemp()
    st = Store(os.path.join(tmp, "h.db"))
    w = FakeWorld()
    w.halts = [("EARL", "LUDP", "12:10:00", "")]                       # موقوف وقت فحص 12:17
    run_at(w, st, (12, 17))
    assert st.one("SELECT * FROM alerts WHERE symbol='EARL'") is None
    c = [json.loads(r["data"]) for r in st.q("SELECT data FROM candidates WHERE symbol='EARL'")]
    assert c and any("موقوف الآن" in g for g in c[-1].get("gates_failed", []))
    assert st.one("SELECT * FROM halts WHERE symbol='EARL'")["reason"] == "LUDP"
    # سهم عليه تنبيه ثم أُوقف: رسالة واحدة فقط
    st2 = Store(os.path.join(tmp, "h2.db"))
    w2 = FakeWorld()
    run_at(w2, st2, (12, 17))
    assert st2.one("SELECT * FROM alerts WHERE symbol='EARL'")
    w2.halts = [("EARL", "T1", "13:00:00", "")]
    w2._hu_halts = None       # الحارس «طلب في الدقيقة» يعمل بالوقت الحقيقي؛ الاختبار يقفز ساعة في جزء من الثانية
    _, msgs = run_at(w2, st2, (13, 17))
    w2._hu_halts = None
    _, msgs2 = run_at(w2, st2, (13, 22))
    assert sum(m.startswith("⏸️ توقف تداول — EARL") for m in msgs) == 1, [x[:60] for x in msgs]
    assert not any(m.startswith("⏸️") for m in msgs2)
    assert any("بانتظار خبر" in m and "Nasdaq" in m for m in msgs)


def test_halts_parse_live_feed_sample_with_undeclared_prefix():
    """العنصر كما جاء فعلاً من الخلاصة (25 نوفمبر 2026): البادئة ndaq غير معرّفة، ووقت فيه مسافات قبل الأجزاء."""
    from hu import halts as HL
    item = ('<item>\n      <title>FHIL</title>\n      <pubDate>Tue, 24 Nov 2026 05:00:00 GMT</pubDate>\n'
            '      <ndaq:IssueSymbol>FHIL</ndaq:IssueSymbol>\n      <ndaq:IssueName>Federated Hermes International Leaders ETF</ndaq:IssueName>\n'
            '      <ndaq:Mkt>Z</ndaq:Mkt>\n      <ndaq:ReasonCode>M</ndaq:ReasonCode>\n      <ndaq:PauseThresholdPrice />\n'
            '      <ndaq:HaltDate>11/24/2026</ndaq:HaltDate>\n      <ndaq:HaltTime>09:31:02                      .553</ndaq:HaltTime>\n'
            '      <ndaq:ResumptionDate>11/24/2026</ndaq:ResumptionDate>\n      <ndaq:ResumptionQuoteTime>09:36:02</ndaq:ResumptionQuoteTime>\n'
            '      <ndaq:ResumptionTradeTime>09:36:02</ndaq:ResumptionTradeTime>\n'
            '      <description><![CDATA[<table><tr><td>FHIL</td></tr></table>]]></description>\n    </item>')
    xml = f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>x</title>{item}{item.replace("FHIL", "ABCD")}</channel></rss>'
    h = HL.parse(xml)
    assert [x.symbol for x in h] == ["FHIL", "ABCD"]
    a = h[0]
    assert (a.market, a.reason, a.halt_date, a.halt_time, a.pause_price, a.resume_trade) == \
        ("Z", "M", "2026-11-24", "09:31:02", None, "09:36:02")
    assert a.name.startswith("Federated") and a.reason_ar == "إيقاف تقلب"


def test_trades_filled_by_ticks_with_fees_and_refill(day):
    st = day[0]
    tr = [t for t in st.q("SELECT * FROM trades") if t["pnl"] is not None]
    assert tr and all(t["fill_src"] == "tick" for t in tr)
    ab = st.q("SELECT calm FROM trades WHERE kind IN ('A','B')")
    assert ab and all(r["calm"] in (0, 1) for r in ab)          # وسم H5 محفوظ مع كل صفقة تنبيه
    assert all((t["fees"] or 0) > 0 for t in tr)
    # إعادة الحساب: نعيد صفقة لحالة «الشموع» ثم نعيد حسابها
    t = tr[0]
    st.update("trades", t["id"], fill_src="bar", exit=None)
    w = FakeWorld()
    w.now = et(TODAY, 16, 40) + timedelta(days=3)
    c = I.AlpacaClient("K", "S", I.Config(), session=w)
    c.rl = I.RateLimiter(100000)
    e = Engine(c, st, CFG, SecClient("T t@example.com", session=w, max_rps=1000), None)
    res = e.refill(w.now, "2026-11-23")
    after = st.one("SELECT * FROM trades WHERE id=?", (t["id"],))
    assert res["refilled"] >= 1 and after["fill_src"] == "tick" and after["exit"] == t["exit"]



def test_extended_trades_off_by_default_and_calm_tag():
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    w = FakeWorld()
    run_at(w, st, (17, 2), kind="post")
    assert st.q("SELECT * FROM alerts WHERE kind='POST'") and not st.q("SELECT * FROM trades WHERE kind='POST'")
    from hu.rules import calm_h5
    assert calm_h5({"vol_vs_avg": 1.0, "spike": 5, "day_change_pct": 3}) == 1
    assert calm_h5({"vol_vs_avg": 2.0, "spike": 5, "day_change_pct": 3}) == 0
    assert calm_h5({"vol_vs_avg": 1.0, "spike": None, "day_change_pct": 3}) is None
