import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """
    Open and return a SQLite connection to the given path.
    Ensures that the parent directory exists and configures the connection
    for better performance and usability:
      - row_factory = sqlite3.Row  →  row["column_name"] instead of row[0]
      - WAL mode                   →  better concurrent read/write performance
      - foreign_keys = ON          →  enforce FK constraints (for future use)
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def initialize_schema(con: sqlite3.Connection) -> None:
    """
    Creates all tables and indices if they don't already exist.
    Idempotent: safe to call on every startup.

    Tables:
      facts      — normalized Compustat-style financial data (the raw material)
      signals    — computed signal values derived from facts
      fetch_log  — audit log of EDGAR fetch attempts per CIK
    """
    con.executescript("""
        -- ─────────────────────────────────────────────────────────────────
        -- facts: one row per (company, variable, period, period_type)
        --
        -- period_type distinguishes annual ("A") from quarterly ("Q") data
        -- for the same tag and period_end date.
        -- Example: Apple's "ni" at 2023-09-30 exists twice:
        --   period_type="A"  value=96995000000  (full FY from 10-K)
        --   period_type="Q"  value=22956000000  (isolated Q4 = FY - 9M)
        -- ─────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS facts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cik         TEXT    NOT NULL,
            tag         TEXT    NOT NULL,
            period_end  TEXT    NOT NULL,
            period_type TEXT    NOT NULL,
            value       REAL    NOT NULL,
            source_form TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(cik, tag, period_end, period_type)
        );

        -- ─────────────────────────────────────────────────────────────────
        -- signals: computed signal values
        -- Populated by pipeline.py after each successful fact harvest.
        -- ─────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cik         TEXT    NOT NULL,
            signal_name TEXT    NOT NULL,
            period_end  TEXT    NOT NULL,
            value       REAL    NOT NULL,
            updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(cik, signal_name, period_end)
        );

        -- ─────────────────────────────────────────────────────────────────
        -- fetch_log: one row per fetch attempt
        -- ─────────────────────────────────────────────────────────────────
        CREATE TABLE IF NOT EXISTS fetch_log (
            cik         TEXT    NOT NULL,
            fetched_at  TEXT    NOT NULL,
            status      TEXT    NOT NULL,
            detail      TEXT,
            PRIMARY KEY (cik, fetched_at)
        );

        -- ─────────────────────────────────────────────────────────────────
        -- Indices
        -- ─────────────────────────────────────────────────────────────────

        -- Most common query: all data for a given company + variable
        CREATE INDEX IF NOT EXISTS idx_facts_cik_tag
            ON facts(cik, tag);

        -- Time-range queries across all companies
        CREATE INDEX IF NOT EXISTS idx_facts_period_end
            ON facts(period_end);

        -- Full lookup key (used by get_time_series and load_fact_data)
        CREATE INDEX IF NOT EXISTS idx_facts_cik_tag_period
            ON facts(cik, tag, period_end, period_type);

        -- Signal lookup
        CREATE INDEX IF NOT EXISTS idx_signals_cik_name
            ON signals(cik, signal_name);
    """)
    con.commit()
    logger.info("Schema initialized.")