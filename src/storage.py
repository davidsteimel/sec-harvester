import sqlite3
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

import pandas as pd

from normalizer import FactRecord

logger = logging.getLogger(__name__)

def upsert_fact(con: sqlite3.Connection, record: FactRecord) -> None:
    """
    Write a single FactRecord into the facts table using INSERT OR REPLACE.
    This is not optimized for bulk inserts; prefer upsert_facts_bulk for that.
    """
    con.execute(
        """
        INSERT OR REPLACE INTO facts (cik, tag, period_end, period_type, value, source_form)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            record.cik,
            record.tag,
            record.period_end.isoformat(),
            record.period_type,
            record.value,
            record.source_form,
        ),
    )


def upsert_facts_bulk(con: sqlite3.Connection, records: list[FactRecord]) -> int:
    """
    Write a list of FactRecords into the facts table efficiently with executemany.
    Returns the number of records written.

    INSERT OR REPLACE respects UNIQUE(cik, tag, period_end, period_type):
    an existing entry with the same key will be replaced.
    """
    rows = [
        (
            r.cik,
            r.tag,
            r.period_end.isoformat(),
            r.period_type,
            r.value,
            r.source_form,
        )
        for r in records
    ]
    con.executemany(
        """
        INSERT OR REPLACE INTO facts (cik, tag, period_end, period_type, value, source_form)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    count = len(rows)
    logger.debug("Upserted %d facts.", count)
    return count


def upsert_signals_bulk(con: sqlite3.Connection, cik: str, signal_rows: list[tuple]) -> int:
    """
    Write computed signal values into the signals table.

    signal_rows: list of (signal_name, period_end_str, value)
    """
    rows = [(cik, name, period_end, value) for name, period_end, value in signal_rows]
    con.executemany(
        """
        INSERT OR REPLACE INTO signals (cik, signal_name, period_end, value)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )
    count = len(rows)
    logger.debug("Upserted %d signal values for CIK %s.", count, cik)
    return count


def log_fetch(con: sqlite3.Connection, cik: str, status: str, detail: str = "") -> None:
    """
    Write a log entry to fetch_log.
    Called after each successful or failed EDGAR fetch.
    """
    con.execute(
        "INSERT INTO fetch_log (cik, fetched_at, status, detail) VALUES (?, ?, ?, ?)",
        (cik, datetime.utcnow().isoformat(), status, detail),
    )

def get_time_series(
    con: sqlite3.Connection,
    cik: str,
    tag: str,
    period_type: str = "Q",
) -> list[tuple[date, float]]:
    """
    Returns the complete time series for (Company, Variable, period_type).
    Return: [(period_end, value), ...] sorted by date in ascending order.
    """
    rows = con.execute(
        """
        SELECT period_end, value
        FROM   facts
        WHERE  cik = ? AND tag = ? AND period_type = ?
        ORDER  BY period_end ASC
        """,
        (cik, tag, period_type),
    ).fetchall()
    return [(date.fromisoformat(r["period_end"]), r["value"]) for r in rows]


def load_fact_data(con: sqlite3.Connection, cik: str) -> dict[str, pd.DataFrame]:
    """
    Loads all facts for a given CIK from the database and returns them as a FactData dict.

    FactData is the format expected by signals.py:
        {
            "at":  pd.DataFrame(columns=["period_end", "value", "period_type"]),
            "ni":  pd.DataFrame(columns=["period_end", "value", "period_type"]),
            ...
        }
    
    """
    rows = con.execute(
        """
        SELECT tag, period_end, value, period_type
        FROM   facts
        WHERE  cik = ?
        ORDER  BY tag, period_end ASC
        """,
        (cik,),
    ).fetchall()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["tag"]].append({
            "period_end":  row["period_end"],
            "value":       row["value"],
            "period_type": row["period_type"],
        })

    return {
        tag: pd.DataFrame(records)
        for tag, records in grouped.items()
    }


def get_latest_value(
    con: sqlite3.Connection,
    cik: str,
    tag: str,
) -> Optional[float]:
    """Returns the latest value for (cik, tag), or None."""
    row = con.execute(
        """
        SELECT value FROM facts
        WHERE  cik = ? AND tag = ?
        ORDER  BY period_end DESC
        LIMIT  1
        """,
        (cik, tag),
    ).fetchone()
    return row["value"] if row else None


def get_last_fetch(con: sqlite3.Connection, cik: str) -> Optional[str]:
    """
    Returns the timestamp of the last successful fetch for a given CIK, or None if no successful fetch exists.
    """
    row = con.execute(
        """
        SELECT fetched_at FROM fetch_log
        WHERE  cik = ? AND status = 'ok'
        ORDER  BY fetched_at DESC
        LIMIT  1
        """,
        (cik,),
    ).fetchone()
    return row["fetched_at"] if row else None


def get_signal_series(
    con: sqlite3.Connection,
    cik: str,
    signal_name: str,
) -> list[tuple[str, float]]:
    """
    Returns the time series of a computed signal for a given company.
    Return: [(period_end_str, value), ...] sorted in ascending order by date.
    """
    rows = con.execute(
        """
        SELECT period_end, value
        FROM   signals
        WHERE  cik = ? AND signal_name = ?
        ORDER  BY period_end ASC
        """,
        (cik, signal_name),
    ).fetchall()
    return [(r["period_end"], r["value"]) for r in rows]