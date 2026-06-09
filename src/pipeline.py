"""
Orchestrates a complete harvest run:
  For each CIK: fetch → normalize → store facts → compute signals → store signals → commit.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import httpx

from config import AppConfig
from db import get_connection, initialize_schema
from edgar_client import EdgarClient
from normalizer import normalize_tag, resolve_tag
from signals import SIGNAL_REGISTRY
from storage import load_fact_data, upsert_facts_bulk, upsert_signals_bulk, log_fetch

logger = logging.getLogger(__name__)


def compute_and_store_signals(con: sqlite3.Connection, cik: str, name: str) -> int:
    """
    Load all facts for a CIK from the DB, run every signal function,
    and write results back to the signals table.

    Returns the total number of signal values written.

    Called after upsert_facts_bulk + commit so that signals always
    reflect the latest facts in the DB.
    """
    fact_data = load_fact_data(con, cik)

    if not fact_data:
        logger.debug("[%s] No fact data found for signal computation.", name)
        return 0

    signal_rows: list[tuple[str, str, float]] = []  # (signal_name, period_end, value)

    for signal_name, signal_fn in SIGNAL_REGISTRY.items():
        try:
            results = signal_fn(fact_data)
            for period_end, value in results.items():
                if value is not None and value == value:  # exclude NaN
                    signal_rows.append((signal_name, period_end, float(value)))
        except Exception as exc:
            # A broken signal must not abort the whole CIK run.
            logger.warning("[%s] Signal '%s' raised an error: %s", name, signal_name, exc)

    if signal_rows:
        upsert_signals_bulk(con, cik, signal_rows)

    logger.debug("[%s] Computed %d signal values across %d signals.",
                 name, len(signal_rows), len(SIGNAL_REGISTRY))
    return len(signal_rows)


async def process_cik(
    client: httpx.AsyncClient,
    con:    sqlite3.Connection,
    config: AppConfig,
    cik:    str,
    name:   str,
) -> dict:
    """
    Complete processing for a single CIK.

    Steps:
      1. Fetch raw EDGAR company facts JSON
      2. For each configured tag: resolve → normalize → collect FactRecords
      3. Bulk-upsert all FactRecords into facts table
      4. Commit (atomic: either all facts for this CIK land, or none)
      5. Compute all signals from the now-updated facts table
      6. Bulk-upsert signal values
      7. Commit + log success

    Returns a status dict:
        {
            "cik":             "0000320193",
            "name":            "Apple Inc.",
            "status":          "ok" | "error",
            "facts_written":   1247,
            "tags_empty":      3,
            "signals_written": 892,
            "duration_s":      4.2,
            "error":           None | "message",
        }
    """
    t_start = datetime.now(timezone.utc)
    edgar   = EdgarClient(config)

    try:
        # Step 1: Fetch
        logger.info("[%s] Fetching company facts ...", name)
        facts_json = await edgar.fetch_company_facts(client, cik)

        # Step 2 + 3: Normalize all tags and collect records 
        total_written = 0
        total_empty   = 0
        all_records   = []

        for compustat_tag, sec_tag_list in config.xbrl_tags.items():
            raw_facts = resolve_tag(facts_json, sec_tag_list)

            if not raw_facts:
                logger.debug("[%s] No EDGAR data for tag '%s'", name, compustat_tag)
                total_empty += 1
                continue

            records = normalize_tag(cik, compustat_tag, raw_facts)

            if not records:
                logger.debug("[%s] Tag '%s' produced 0 FactRecords after normalization",
                             name, compustat_tag)
                total_empty += 1
                continue

            all_records.extend(records)

        #  Step 4: Write facts + commit 
        if all_records:
            total_written = upsert_facts_bulk(con, all_records)
            con.commit()

        # Step 5 + 6: Compute signals and write them 
        signals_written = compute_and_store_signals(con, cik, name)

        # Step 7: Log success + final commit 
        log_fetch(con, cik, "ok")
        con.commit()

        duration = (datetime.now(timezone.utc) - t_start).total_seconds()
        logger.info(
            "[%s] Done. %d facts, %d signal values, %d tags empty. (%.1fs)",
            name, total_written, signals_written, total_empty, duration,
        )

        return {
            "cik":             cik,
            "name":            name,
            "status":          "ok",
            "facts_written":   total_written,
            "tags_empty":      total_empty,
            "signals_written": signals_written,
            "duration_s":      duration,
            "error":           None,
        }

    except Exception as exc:
        duration = (datetime.now(timezone.utc) - t_start).total_seconds()
        logger.error("[%s] Failed after %.1fs: %s", name, duration, exc, exc_info=True)

        try:
            log_fetch(con, cik, "error", detail=str(exc))
            con.commit()
        except Exception as log_exc:
            logger.warning("Could not write error to fetch_log: %s", log_exc)

        return {
            "cik":             cik,
            "name":            name,
            "status":          "error",
            "facts_written":   0,
            "tags_empty":      0,
            "signals_written": 0,
            "duration_s":      duration,
            "error":           str(exc),
        }


async def run_pipeline(config: AppConfig) -> list[dict]:
    """
    Main entry point for a full harvest run.

    Opens the DB, ensures schema exists, fetches all CIKs in the watchlist
    in parallel (bounded by the semaphore in EdgarClient), returns one
    status dict per CIK.
    """
    logger.info("Starting pipeline run for %d companies ...", len(config.watchlist))
    t_start = datetime.now(timezone.utc)

    con = get_connection(config.db_path)
    initialize_schema(con)

    async with httpx.AsyncClient(timeout=30.0) as client:
        tasks = [
            process_cik(client, con, config, cik, name)
            for cik, name in config.watchlist.items()
        ]
        # return_exceptions=True: a failure in one CIK does not cancel others
        results = await asyncio.gather(*tasks, return_exceptions=True)

    con.close()

    # Normalize: asyncio.gather can return raw Exception objects if something
    # unexpected escaped process_cik's own try/except
    normalized = []
    for r in results:
        if isinstance(r, Exception):
            logger.error("Unexpected gather-level error: %s", r)
            normalized.append({"status": "error", "error": str(r)})
        else:
            normalized.append(r)

    duration  = (datetime.now(timezone.utc) - t_start).total_seconds()
    ok_count  = sum(1 for r in normalized if r.get("status") == "ok")
    err_count = len(normalized) - ok_count

    logger.info(
        "Pipeline run complete. %d/%d ok, %d errors. Total time: %.1fs",
        ok_count, len(normalized), err_count, duration,
    )
    return normalized