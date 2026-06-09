"""
Usage examples:
    # Run once for your watchlist (defined in config.py):
    python main.py run

    # Run once for specific CIKs:
    python main.py run --cik 0000320193 0000789019

    # Pull ALL ~12,000 active companies from EDGAR (takes hours, GB-scale DB):
    python main.py pull-all

    # Pull all, but limit to N companies (useful for testing pull-all logic):
    python main.py pull-all --limit 50

    # Run on a schedule (poll every N seconds, default from config):
    python main.py run --loop
    python main.py run --loop --interval 3600

    # Check what's in the DB:
    python main.py status

    # List earliest available data per tag for a given CIK:
    python main.py earliest --cik 0000320193
"""

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import AppConfig, SUBMISSIONS_URL
from db import get_connection, initialize_schema
from pipeline import run_pipeline, process_cik
from storage import get_time_series

def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("harvester.log", encoding="utf-8"),
        ],
    )
    # Quieten noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


async def fetch_all_ciks(config: AppConfig, limit: int | None = None) -> dict[str, str]:
    """
    Fetch the full list of companies that have filed 10-K or 10-Q from EDGAR.

    EDGAR provides a complete company tickers JSON at:
        https://www.sec.gov/files/company_tickers.json

    Returns dict of {cik_padded: company_name} for all active filers.
    With limit set, returns only the first N companies (for testing).

    Note: "active" here means they appear in EDGAR's company list.
    ~12,000 companies have meaningful 10-K/10-Q history.
    """
    url = "https://www.sec.gov/files/company_tickers.json"
    headers = {"User-Agent": config.user_agent}

    logger.info("Fetching full company list from EDGAR ...")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        raw = resp.json()

    # Raw format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    companies: dict[str, str] = {}
    for entry in raw.values():
        cik_padded = str(entry["cik_str"]).zfill(10)
        name       = entry.get("title", "Unknown")
        companies[cik_padded] = name
        if limit and len(companies) >= limit:
            break

    logger.info("Found %d companies in EDGAR company list.", len(companies))
    return companies


async def cmd_run(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Run a single harvest for the watchlist (or specific CIKs if --cik is given).
    With --loop, runs repeatedly on the configured interval.
    """
    # Build the watchlist for this run
    if args.cik:
        # --cik passed on command line: override watchlist
        watchlist = {cik.zfill(10): cik for cik in args.cik}
        logger.info("Running for %d CIK(s) from command line.", len(watchlist))
    else:
        watchlist = config.watchlist
        logger.info("Running for %d CIK(s) from watchlist.", len(watchlist))

    interval = getattr(args, "interval", None) or config.poll_interval

    # Build a config with the right watchlist
    # AppConfig is frozen so we build a plain namespace for pipeline
    run_config = _config_with_watchlist(config, watchlist)

    if args.loop:
        logger.info("Loop mode: interval = %ds. Ctrl+C to stop.", interval)
        while True:
            t0 = time.monotonic()
            await run_pipeline(run_config)
            elapsed = time.monotonic() - t0
            sleep_for = max(0, interval - elapsed)
            logger.info("Next run in %.0fs.", sleep_for)
            await asyncio.sleep(sleep_for)
    else:
        results = await run_pipeline(run_config)
        _print_run_summary(results)


async def cmd_pull_all(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Pull ALL companies from EDGAR (~12,000).

    This is a long-running operation:
      - ~12,000 companies × ~1–3s per fetch = several hours
      - Resulting DB will be several GB
      - Use --limit N to test with a subset first

    The run is resumable: companies already in fetch_log with status='ok'
    within the last 24h are skipped automatically by the pipeline.
    """
    limit = getattr(args, "limit", None)

    if limit is None:
        logger.warning(
            "pull-all will attempt to fetch ~12,000 companies. "
            "This will take several hours and produce a multi-GB database. "
            "Use --limit N to test first. Starting in 5 seconds ... (Ctrl+C to abort)"
        )
        await asyncio.sleep(5)

    companies = await fetch_all_ciks(config, limit=limit)
    run_config = _config_with_watchlist(config, companies)

    logger.info("Starting pull-all for %d companies.", len(companies))
    results = await run_pipeline(run_config)
    _print_run_summary(results)


async def cmd_status(args: argparse.Namespace, config: AppConfig) -> None:
    """
    Print a summary of what's currently in the database.
    """
    con = get_connection(config.db_path)
    initialize_schema(con)

    # Facts summary
    total     = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    companies = con.execute("SELECT COUNT(DISTINCT cik) FROM facts").fetchone()[0]
    tags      = con.execute("SELECT COUNT(DISTINCT tag) FROM facts").fetchone()[0]
    earliest  = con.execute("SELECT MIN(period_end) FROM facts").fetchone()[0]
    latest    = con.execute("SELECT MAX(period_end) FROM facts").fetchone()[0]

    # Fetch log
    ok_fetches  = con.execute("SELECT COUNT(*) FROM fetch_log WHERE status='ok'").fetchone()[0]
    err_fetches = con.execute("SELECT COUNT(*) FROM fetch_log WHERE status='error'").fetchone()[0]
    last_fetch  = con.execute(
        "SELECT MAX(fetched_at) FROM fetch_log WHERE status='ok'"
    ).fetchone()[0]

    # Per-tag fact counts (top 20 by count)
    tag_counts = con.execute("""
        SELECT tag, period_type, COUNT(*) as n_periods
        FROM facts
        GROUP BY tag, period_type
        ORDER BY n_periods DESC
        LIMIT 20
    """).fetchall()

    # Signals summary
    total_signals    = con.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    distinct_signals = con.execute("SELECT COUNT(DISTINCT signal_name) FROM signals").fetchone()[0]
    signal_counts    = con.execute("""
        SELECT signal_name, COUNT(*) as n_values
        FROM signals
        GROUP BY signal_name
        ORDER BY n_values DESC
        LIMIT 15
    """).fetchall()

    con.close()

    print("\n" + "="*60)
    print("  SEC Signal Harvester — Database Status")
    print("="*60)
    print(f"  DB path:         {config.db_path}")
    print(f"  Total facts:     {total:,}")
    print(f"  Companies:       {companies:,}")
    print(f"  Tags:            {tags}")
    print(f"  Date range:      {earliest} → {latest}")
    print(f"  Fetches ok:      {ok_fetches}")
    print(f"  Fetches error:   {err_fetches}")
    print(f"  Last ok fetch:   {last_fetch}")
    print()
    print(f"  Total signals:   {total_signals:,}  ({distinct_signals} distinct signals)")
    print()
    print("  Facts per tag (top 20):")
    print(f"  {'tag':20s}  {'type':4s}  {'count':>8s}")
    print("  " + "-"*38)
    for row in tag_counts:
        print(f"  {row['tag']:20s}  {row['period_type']:4s}  {row['n_periods']:>8,}")
    print()
    print("  Signal values per signal (top 15):")
    print(f"  {'signal':30s}  {'values':>8s}")
    print("  " + "-"*42)
    for row in signal_counts:
        print(f"  {row['signal_name']:30s}  {row['n_values']:>8,}")
    print("="*60 + "\n")


async def cmd_earliest(args: argparse.Namespace, config: AppConfig) -> None:
    """
    For a given CIK, show the earliest available period_end per tag.
    Useful for understanding how far back XBRL data goes for a specific company.

    Example: python main.py earliest --cik 0000320193
    """
    cik = args.cik[0].zfill(10) if args.cik else None
    if not cik:
        print("ERROR: --cik required for earliest command.")
        sys.exit(1)

    con = get_connection(config.db_path)
    initialize_schema(con)

    # Check if we have any data for this CIK
    count = con.execute(
        "SELECT COUNT(*) FROM facts WHERE cik = ?", (cik,)
    ).fetchone()[0]

    if count == 0:
        print(f"\nNo data found for CIK {cik}. Run 'python main.py run --cik {cik}' first.\n")
        con.close()
        return

    rows = con.execute("""
        SELECT tag, period_type,
               MIN(period_end) as earliest,
               MAX(period_end) as latest,
               COUNT(*)        as n_periods
        FROM   facts
        WHERE  cik = ?
        GROUP  BY tag, period_type
        ORDER  BY tag, period_type
    """, (cik,)).fetchall()

    name = con.execute(
        "SELECT detail FROM fetch_log WHERE cik=? AND status='ok' LIMIT 1", (cik,)
    ).fetchone()
    con.close()

    print(f"\n{'='*70}")
    print(f"  CIK {cik} — Data availability")
    print(f"{'='*70}")
    print(f"  {'tag':20s}  {'type':4s}  {'earliest':12s}  {'latest':12s}  {'n':>6s}")
    print("  " + "-"*60)
    for row in rows:
        print(f"  {row['tag']:20s}  {row['period_type']:4s}  "
              f"{row['earliest']:12s}  {row['latest']:12s}  {row['n_periods']:>6,}")
    print(f"{'='*70}\n")


def _config_with_watchlist(config: AppConfig, watchlist: dict[str, str]) -> AppConfig:
    """
    Return a new AppConfig with a different watchlist.
    AppConfig is frozen so we reconstruct it.
    """
    return AppConfig(
        watchlist=watchlist,
        poll_interval=config.poll_interval,
        trigger_forms=config.trigger_forms,
        db_path=config.db_path,
        user_agent=config.user_agent,
        xbrl_tags=config.xbrl_tags,
    )


def _print_run_summary(results: list[dict]) -> None:
    ok     = [r for r in results if r.get("status") == "ok"]
    errors = [r for r in results if r.get("status") != "ok"]

    print(f"\n{'='*60}")
    print(f"  Run complete: {len(ok)} ok, {len(errors)} errors")
    print(f"{'='*60}")
    for r in ok:
        print(f"  ✓  {r.get('name', r.get('cik', '?')):35s}  "
              f"{r.get('facts_written', 0):>6,} facts  "
              f"({r.get('duration_s', 0):.1f}s)")
    for r in errors:
        print(f"  ✗  {r.get('name', r.get('cik', '?')):35s}  "
              f"ERROR: {r.get('error', '?')}")
    print(f"{'='*60}\n")



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sec-harvester",
        description="SEC EDGAR signal harvester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py run                          # watchlist, once
  python main.py run --cik 0000320193        # single company, once
  python main.py run --loop --interval 3600  # watchlist, every hour
  python main.py pull-all --limit 100        # first 100 EDGAR companies (test)
  python main.py pull-all                    # ALL ~12,000 companies (hours)
  python main.py status                      # show DB stats
  python main.py earliest --cik 0000320193   # data availability for Apple
        """,
    )
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable DEBUG logging")
    parser.add_argument("--db", default=None,
                        help="Override DB path from config")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── run
    run_p = subparsers.add_parser("run", help="Harvest watchlist (or specific CIKs)")
    run_p.add_argument("--cik", nargs="+", metavar="CIK",
                       help="One or more CIKs to fetch (overrides watchlist)")
    run_p.add_argument("--loop", action="store_true",
                       help="Run repeatedly on a schedule")
    run_p.add_argument("--interval", type=int, default=None,
                       help="Poll interval in seconds (default: from config)")

    # ── pull-all 
    pull_p = subparsers.add_parser(
        "pull-all",
        help="Fetch ALL ~12,000 EDGAR companies (long-running, GB-scale)"
    )
    pull_p.add_argument("--limit", type=int, default=None,
                        help="Stop after N companies (for testing pull-all logic)")

    # ── status 
    subparsers.add_parser("status", help="Show database statistics")

    # ── earliest 
    earliest_p = subparsers.add_parser(
        "earliest",
        help="Show earliest available data per tag for a CIK"
    )
    earliest_p.add_argument("--cik", nargs=1, required=True, metavar="CIK",
                             help="CIK to inspect")

    return parser

async def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    setup_logging(verbose=args.verbose)
    logger.info("SEC Signal Harvester starting up.")

    config = AppConfig()
    if args.db:
        config = _config_with_watchlist(config, config.watchlist)
        # Override db_path — reconstruct with new path
        config = AppConfig(
            watchlist=config.watchlist,
            poll_interval=config.poll_interval,
            trigger_forms=config.trigger_forms,
            db_path=args.db,
            user_agent=config.user_agent,
            xbrl_tags=config.xbrl_tags,
        )

    dispatch = {
        "run":       cmd_run,
        "pull-all":  cmd_pull_all,
        "status":    cmd_status,
        "earliest":  cmd_earliest,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    await handler(args, config)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Exiting.")
        sys.exit(0)