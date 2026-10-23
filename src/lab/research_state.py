"""Transactional exposure ledger. Attempts are committed BEFORE evaluated code sees data."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime


def connect(cfg):
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(cfg.results_dir / "research.sqlite3", timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS exposure (id INTEGER PRIMARY KEY, kind TEXT, start TEXT, end TEXT, family TEXT, fingerprint TEXT, reason TEXT, status TEXT, created TEXT)"
    )
    return db


def family(speed):
    return "daily" if speed == "daily" else "intraday"


def record_research(cfg, period, speed):
    db = connect(cfg)
    try:
        with db:
            db.execute(
                "INSERT INTO exposure(kind,start,end,family,status,created) VALUES(?,?,?,?,?,?)",
                (
                    "research",
                    str(period.start),
                    str(period.end or "9999-12-31"),
                    family(speed),
                    "exposed",
                    datetime.now(UTC).isoformat(),
                ),
            )
    finally:
        db.close()


def latest_research(cfg, speed):
    db = connect(cfg)
    try:
        return db.execute(
            "SELECT MAX(end) FROM exposure WHERE kind='research' AND family=?", (family(speed),)
        ).fetchone()[0]
    finally:
        db.close()


def holdouts(cfg):
    db = connect(cfg)
    try:
        return [
            {
                "period": [a, b],
                "speed": "daily" if fam == "daily" else "intraday",
                "fingerprint": fp,
                "status": status,
                "reason_for_repeat": reason,
                "at": at,
            }
            for a, b, fam, fp, status, reason, at in db.execute(
                "SELECT start,end,family,fingerprint,status,reason,created FROM exposure WHERE kind='holdout'"
            )
        ]
    finally:
        db.close()


@contextmanager
def holdout_attempt(cfg, specs, period, speed, reason):
    from lab.experiments import HoldoutUsed, spec_fingerprint
    from lab.persistence import file_lock

    # Hold the run lock for the complete experiment, including failures. The
    # SQLite reservation independently survives process death/restart.
    with file_lock(cfg.results_dir / "holdout.lock"):
        db = connect(cfg)
        try:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT id FROM exposure WHERE kind='holdout' AND family=? LIMIT 1", (family(speed),)
            ).fetchone()
            legacy = cfg.results_dir / "holdout_ledger.json"
            old = json.loads(legacy.read_text()) if legacy.exists() else []
            if not reason.strip() and (prior or any(family(e.get("speed", "daily")) == family(speed) for e in old)):
                raise HoldoutUsed("this holdout family has already been exposed; an audited repeat reason is required")
            fp = ",".join(spec_fingerprint(s) for s in specs)
            row = db.execute(
                "INSERT INTO exposure(kind,start,end,family,fingerprint,reason,status,created) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "holdout",
                    str(period.start),
                    str(period.end or "9999-12-31"),
                    family(speed),
                    fp,
                    reason,
                    "started",
                    datetime.now(UTC).isoformat(),
                ),
            )
            ident = row.lastrowid
            db.commit()
            try:
                yield ident
            except BaseException:
                with db:
                    db.execute("UPDATE exposure SET status=? WHERE id=?", ("failed", ident))
                raise
            else:
                with db:
                    db.execute("UPDATE exposure SET status=? WHERE id=?", ("completed", ident))
        finally:
            db.close()
