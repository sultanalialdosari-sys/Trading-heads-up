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


def run_at(w, st, hm, kind="scan", sec_ua="Test t@example.com"):
    w.now = et(TODAY, *hm)
    w.tg.clear()
    c = I.AlpacaClient("KEY_SHOULD_NOT_LEAK", "SECRET_SHOULD_NOT_LEAK", I.Config(), session=w)
    c.rl = I.RateLimiter(100000)
    e = Engine(c, st, CFG, SecClient(sec_ua, session=w, max_rps=1000), I.TelegramNotifier("TGTOKEN_SECRET", "1", session=w))
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
