#!/usr/bin/env python3
"""Persistent operator state: idempotency + watermarks + user claims.

Tables:
  done   (kind, id)      — processed event ids; restarts never double-process.
  kv     (k, v)          — watermarks (eth from_block, btc last_seen, ...).
  claims (key, ...)      — user-submitted mappings:
      excal:<excal_txid> -> {"btc_address": ...}     (EXCAL -> ATEX leg)
      btc:<btc_txid>     -> {"eth_address": ...}     (ATEX -> SKYNT leg)
      btc:<btc_txid>     -> {"excal_lock": ...}      (ATEX -> EXCAL leg)
"""
import sqlite3
import time
import json


SCHEMA = """
CREATE TABLE IF NOT EXISTS done (
    kind TEXT NOT NULL,
    id   TEXT NOT NULL,
    ts   INTEGER NOT NULL,
    detail TEXT,
    PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts INTEGER NOT NULL);
"""


class State:
    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)
        self.con.commit()

    # ---- idempotency --------------------------------------------------
    def seen(self, kind, id_):
        row = self.con.execute(
            "SELECT 1 FROM done WHERE kind=? AND id=?", (kind, id_)).fetchone()
        return row is not None

    def mark(self, kind, id_, detail=""):
        self.con.execute(
            "INSERT OR IGNORE INTO done(kind,id,ts,detail) VALUES (?,?,?,?)",
            (kind, id_, int(time.time()), detail))
        self.con.commit()

    # ---- watermarks ---------------------------------------------------
    def get(self, k, default=None):
        row = self.con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def set(self, k, v):
        self.con.execute(
            "INSERT INTO kv(k,v) VALUES (?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        self.con.commit()

    # ---- claims -------------------------------------------------------
    def add_claim(self, key, value):
        self.con.execute(
            "INSERT INTO claims(key,value,ts) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key, json.dumps(value), int(time.time())))
        self.con.commit()

    def get_claim(self, key):
        row = self.con.execute(
            "SELECT value FROM claims WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.con.close()
