# -*- coding: utf-8 -*-
"""قاعدة SQLite محلية (data/headsup.db) — تُحفظ في المستودع بعد كل تشغيل لتراجعها لاحقاً."""
import json
import os
import sqlite3
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, started_utc TEXT, scheduled_utc TEXT, delay_min REAL,
  market_open INTEGER, universe INTEGER, swept INTEGER, shortlist INTEGER, qualified INTEGER, alerts INTEGER,
  api_calls INTEGER, sec_calls INTEGER, duration_sec REAL, feed_note TEXT, errors TEXT);
CREATE TABLE IF NOT EXISTS candidates (
  run_id INTEGER, day TEXT, symbol TEXT, score INTEGER, stage TEXT, qualifies INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS ix_cand_day ON candidates(day, symbol);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, symbol TEXT, run_id INTEGER, alert_utc TEXT, kind TEXT,
  score INTEGER, price REAL, trigger REAL, invalidation REAL, data_quality INTEGER, gain_from_open REAL,
  state TEXT DEFAULT 'OPEN', activated_utc TEXT, invalidated_utc TEXT, lead_min REAL, sent INTEGER, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS ix_alert_day ON alerts(day, symbol);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id INTEGER, kind TEXT, day TEXT, symbol TEXT, created_utc TEXT,
  ref_price REAL, trigger REAL, entry_utc TEXT, entry REAL, stop REAL, target REAL, shares INTEGER,
  status TEXT, exit_utc TEXT, exit REAL, pnl REAL, r_mult REAL, pct REAL, note TEXT);
CREATE INDEX IF NOT EXISTS ix_trade_day ON trades(day, status);
CREATE TABLE IF NOT EXISTS missed (
  day TEXT, symbol TEXT, prev_close REAL, high REAL, max_gain_pct REAL, close_gain_pct REAL,
  was_candidate INTEGER, best_score INTEGER, reason TEXT, PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS movers (
  day TEXT, symbol TEXT, first_utc TEXT, first_chg REAL, first_price REAL, PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, day TEXT, at_utc TEXT);
CREATE TABLE IF NOT EXISTS chart_bars (day TEXT, symbol TEXT, bars TEXT, source TEXT, updated_utc TEXT, PRIMARY KEY (day, symbol));
"""


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(alerts)")}
        for c, t in (("eval_from", "TEXT"), ("eval_until", "TEXT"), ("phase", "TEXT")):
            if c not in cols:
                self.db.execute(f"ALTER TABLE alerts ADD COLUMN {c} {t}")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(candidates)")}
        if "phase" not in cols:
            self.db.execute("ALTER TABLE candidates ADD COLUMN phase TEXT")
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(runs)")}
        if "timing" not in cols:
            self.db.execute("ALTER TABLE runs ADD COLUMN timing TEXT")
        if "config_hash" not in cols:
            self.db.execute("ALTER TABLE runs ADD COLUMN config_hash TEXT")
        tcols = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        for c, typ in (("last_px", "REAL"), ("last_utc", "TEXT")):   # آخر سعر تابعه النظام (للربح غير المحقق)
            if c not in tcols:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {c} {typ}")

    def q(self, sql: str, args=()) -> list:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> Optional[dict]:
        r = self.db.execute(sql, args).fetchone()
        return dict(r) if r else None

    def insert(self, table: str, row: dict) -> int:
        row = {k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v) for k, v in row.items()}
        cols = ",".join(row)
        cur = self.db.execute(f"INSERT INTO {table} ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        return cur.lastrowid

    def update(self, table: str, rid: int, **kw) -> None:
        if not kw:
            return
        sets = ",".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE {table} SET {sets} WHERE id=?", list(kw.values()) + [rid])

    def once(self, key: str, day: str, at: str) -> bool:
        """True أول مرة فقط لهذا المفتاح (منع تكرار الرسائل)."""
        try:
            self.db.execute("INSERT INTO sent (key, day, at_utc) VALUES (?,?,?)", (key, day, at))
            return True
        except sqlite3.IntegrityError:
            return False

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
