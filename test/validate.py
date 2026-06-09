import argparse
import csv
import math
import sys
import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlite3

TAG_TO_COMPUSTAT_COLUMN: dict[str, str] = {
    "at":     "CO_IFNDQ_ATQ",
    "act":    "CO_IFNDQ_ACTQ",
    "che":    "CO_IFNDQ_CHEQ",
    "lct":    "CO_IFNDQ_LCTQ",
    "lt":     "CO_IFNDQ_LTQ",
    "dltt":   "CO_IFNDQ_DLTTQ",
    "dlc":    "CO_IFNDQ_DLCQ",
    "ceq":    "CO_IFNDQ_CEQQ",
    "seq":    "CO_IFNDQ_SEQQ",
    "pstk":   "CO_IFNDQ_PSTKQ",
    "txditc": "CO_IFNDQ_TXDITCQ",
    "invt":   "CO_IFNDQ_INVTQ",
    "ppegt":  "CO_IFNDQ_PPEGTQ",
    "ppent":  "CO_IFNDQ_PPENTQ",
    "ivao":   "CO_IFNDQ_IVAOQ",
    "mib":    "CO_IFNDQ_MIBQ",
    "recta":  "CO_IFNDQ_RECTAQ",
    "csho":   "CO_IFNDQ_CSHOQ",
    "re":     "CO_IFNDQ_REQ",
    "gdwl":   "CO_IFNDQ_GDWLQ",
    "intan":  "CO_IFNDQ_INTANQ",
    "ni":     "CO_IFNDQ_NIQ",
    "ib":     "CO_IFNDQ_IBQ",
    "sale":   "CO_IFNDQ_SALEQ",
    "txt":    "CO_IFNDQ_TXTQ",
    "txdi":   "CO_IFNDQ_TXDIQ",
    "xint":   "CO_IFNDQ_XINTQ",
    "dp":     "CO_IFNDQ_DPQ",
    "oibdp":  "CO_IFNDQ_OIBDPQ",
    "xrd":    "CO_IFNDQ_XRDQ",
    "cogs":   "CO_IFNDQ_COGSQ",
    "xsga":   "CO_IFNDQ_XSGAQ",
    "epspx":  "CO_IFNDQ_EPSPXQ",
    "epsfx":  "CO_IFNDQ_EPSFXQ",
    "oancf":  "CO_IFNDQ_OANCFQ",   
    "capx":   "CO_IFNDQ_CAPXQ",    
    "dv":     "CO_IFNDQ_DVTQ",     
    "prstkc": "CO_IFNDQ_PRSTKQ",   
    "sstk":   "CO_IFNDQ_SSTKY",    
}

NO_SCALE_TAGS: set[str] = {"csho", "epspx", "epsfx"}
COMPUSTAT_SCALE = 1_000_000.0

REL_TOL = 0.01   
ABS_TOL = 1_000_000.0  

def normalize_cik(cik: str) -> str:
    return str(cik).strip().zfill(10)

def parse_date(value: str) -> str:
    return value[:10] if value else ""

def parse_float(value: str) -> float | None:
    if not value or value.strip() == "":
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except ValueError:
        return None

def is_match(sec_val: float, compustat_val: float) -> bool:
    diff = abs(sec_val - compustat_val)
    if diff <= ABS_TOL:
        return True
    denom = max(abs(sec_val), abs(compustat_val), 1.0)
    return (diff / denom) <= REL_TOL

def load_compustat(
    csv_path: Path,
    target_ciks_by_name: dict[str, str],   
    tags: list[str],
    min_year: str,
    max_year: str,
) -> dict[tuple[str, str, str], float]:
    
    LEGAL_SUFFIXES = {
        "inc", "corp", "corporation", "co", "company", "ltd", "limited",
        "llc", "lp", "plc", "nv", "sa", "ag", "asa", "com",
    }

    def normalize_name(name: str) -> str:
        tokens = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
        return " ".join(t for t in tokens if t not in LEGAL_SUFFIXES)

    needed_columns = {tag: TAG_TO_COMPUSTAT_COLUMN[tag] for tag in tags if tag in TAG_TO_COMPUSTAT_COLUMN}
    facts: dict[tuple[str, str, str], float] = {}
    rows_read = 0
    rows_matched = 0

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        # 1. AUTO-DELIMITER ERKENNUNG
        first_line = f.readline()
        delim = "\t" if "\t" in first_line else ","
        f.seek(0) # Zurück zum Anfang der Datei
        
        reader = csv.DictReader(f, delimiter=delim)

        for row_num, row in enumerate(reader):
            rows_read += 1
            if row_num % 50_000 == 0 and row_num > 0:
                print(f"\r  {row_num:,} Zeilen gelesen, {rows_matched} Treffer ...",
                      end="", file=sys.stderr, flush=True)

            consol = row.get("CO_IDESIND_CONSOL", "").strip()
            if consol and consol != "C":
                continue

            period_end = parse_date(row.get("CO_IDESIND_DATADATE", ""))
            if not period_end:
                continue

            year = period_end[:4]
            if year < min_year or year > max_year:
                continue

            compustat_name = row.get("COMPANY_CONM", "").strip()
            norm_name = normalize_name(compustat_name)
            
            cik = target_ciks_by_name.get(norm_name)
            if cik is None:
                continue

            rows_matched += 1

            for tag, column in needed_columns.items():
                raw = parse_float(row.get(column, ""))
                if raw is None:
                    continue

                key = (cik, period_end, tag)
                if key in facts:
                    continue  

                if tag in NO_SCALE_TAGS:
                    facts[key] = raw
                else:
                    facts[key] = raw * COMPUSTAT_SCALE

    print(f"\r  {rows_read:,} Zeilen gelesen, {rows_matched} Treffer.          ", file=sys.stderr)
    return facts

def load_edgar(
    db_path: Path,
    ciks: list[str],
    tags: list[str],
    min_year: str,
    max_year: str,
) -> dict[tuple[str, str, str], float]:
    
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    placeholders_cik = ",".join("?" for _ in ciks)
    placeholders_tag = ",".join("?" for _ in tags)

    rows = con.execute(
        f"""
        SELECT cik, tag, period_end, value
        FROM   facts
        WHERE  period_type = 'Q'
          AND  cik IN ({placeholders_cik})
          AND  tag IN ({placeholders_tag})
          AND  substr(period_end, 1, 4) >= ?
          AND  substr(period_end, 1, 4) <= ?
        ORDER  BY cik, tag, period_end
        """,
        [*ciks, *tags, min_year, max_year],
    ).fetchall()
    con.close()

    return {(row["cik"], row["period_end"], row["tag"]): row["value"] for row in rows}

def run_comparison(
    compustat_path: Path,
    db_path: Path,
    output_path: Path,
    ciks: list[str],
    tags: list[str],
    min_year: str,
    max_year: str,
    watchlist: dict[str, str],
) -> None:
    
    LEGAL_SUFFIXES = {
        "inc", "corp", "corporation", "co", "company", "ltd", "limited",
        "llc", "lp", "plc", "nv", "sa", "ag", "asa", "com",
    }
    def normalize_name(name: str) -> str:
        tokens = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
        return " ".join(t for t in tokens if t not in LEGAL_SUFFIXES)

    if not ciks:
        ciks = list(watchlist.keys())
    ciks = [normalize_cik(c) for c in ciks]

    if not tags:
        tags = [t for t in TAG_TO_COMPUSTAT_COLUMN if t in {tag for tag in TAG_TO_COMPUSTAT_COLUMN}]
    tags = [t for t in tags if t in TAG_TO_COMPUSTAT_COLUMN]

    name_to_cik: dict[str, str] = {}
    cik_to_name: dict[str, str] = {}
    for cik in ciks:
        name = watchlist.get(cik, cik)
        norm = normalize_name(name)
        name_to_cik[norm] = cik
        cik_to_name[cik] = name

    print(f"\nVergleiche {len(ciks)} Firmen, {len(tags)} Tags, {min_year}–{max_year}")
    print(f"Watchlist-Mapping:")
    for cik, name in cik_to_name.items():
        print(f"  {cik}  '{name}'  →  normalized: '{normalize_name(name)}'")

    print(f"\nLese Compustat: {compustat_path} ({compustat_path.stat().st_size / 1e6:.0f} MB) ...")
    compustat_facts = load_compustat(compustat_path, name_to_cik, tags, min_year, max_year)
    print(f"  {len(compustat_facts):,} Compustat-Fakten geladen.")

    print(f"\nLese EDGAR-DB: {db_path} ...")
    edgar_facts = load_edgar(db_path, ciks, tags, min_year, max_year)
    print(f"  {len(edgar_facts):,} EDGAR-Fakten geladen (period_type='Q').")

    all_keys = sorted(set(compustat_facts) | set(edgar_facts))
    counts = {"match": 0, "mismatch": 0, "only_edgar": 0, "only_compustat": 0}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status", "cik", "company", "tag", "period_end",
        "edgar_value", "compustat_value_usd", "compustat_value_millions",
        "abs_diff", "pct_diff", "compustat_column",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for key in all_keys:
            cik, period_end, tag = key
            edgar_val = edgar_facts.get(key)
            comp_val  = compustat_facts.get(key)
            company   = cik_to_name.get(cik, cik)
            comp_col  = TAG_TO_COMPUSTAT_COLUMN.get(tag, "")

            if edgar_val is not None and comp_val is not None:
                match = is_match(edgar_val, comp_val)
                status = "match" if match else "mismatch"
                counts[status] += 1
                abs_diff = edgar_val - comp_val
                pct_diff = abs_diff / abs(comp_val) if comp_val else ""
                comp_millions = comp_val / COMPUSTAT_SCALE if tag not in NO_SCALE_TAGS else comp_val
                writer.writerow({
                    "status": status, "cik": cik, "company": company, "tag": tag, "period_end": period_end,
                    "edgar_value": edgar_val, "compustat_value_usd": comp_val, "compustat_value_millions": comp_millions,
                    "abs_diff": abs_diff, "pct_diff": f"{pct_diff:.4%}" if isinstance(pct_diff, float) else "",
                    "compustat_column": comp_col,
                })
            elif edgar_val is not None:
                counts["only_edgar"] += 1
                writer.writerow({"status": "only_edgar", "cik": cik, "company": company, "tag": tag, "period_end": period_end, "edgar_value": edgar_val, "compustat_column": comp_col})
            else:
                counts["only_compustat"] += 1
                comp_millions = comp_val / COMPUSTAT_SCALE if tag not in NO_SCALE_TAGS else comp_val
                writer.writerow({"status": "only_compustat", "cik": cik, "company": company, "tag": tag, "period_end": period_end, "compustat_value_usd": comp_val, "compustat_value_millions": comp_millions, "compustat_column": comp_col})

    total_compared = counts["match"] + counts["mismatch"]
    match_rate = counts["match"] / total_compared if total_compared else 0.0

    print(f"\n{'='*60}")
    print(f"  Validation Report")
    print(f"{'='*60}")
    print(f"  Verglichen:          {total_compared:>6,}")
    print(f"  Match:               {counts['match']:>6,}  ({match_rate:.1%})")
    print(f"  Mismatch:            {counts['mismatch']:>6,}  ({1-match_rate:.1%})")
    print(f"  Nur in EDGAR:        {counts['only_edgar']:>6,}")
    print(f"  Nur in Compustat:    {counts['only_compustat']:>6,}")
    print(f"{'='*60}")
    print(f"  Report: {output_path}")
    print(f"{'='*60}\n")

    if counts["mismatch"] > 0:
        print("  Top 10 Mismatches (nach abs. Differenz):")
        print(f"  {'company':25s}  {'tag':8s}  {'period':12s}  {'edgar':>18s}  {'compustat':>18s}  {'diff%':>8s}")
        print("  " + "-"*100)
        mismatches = []
        with output_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["status"] == "mismatch":
                    try:
                        abs_d = abs(float(row["edgar_value"]) - float(row["compustat_value_usd"]))
                        mismatches.append((abs_d, row))
                    except (ValueError, KeyError):
                        pass
                        
        mismatches.sort(key=lambda item: item[0], reverse=True)
        
        for _, row in mismatches[:10]:
            try:
                edgar_v = float(row["edgar_value"])
                comp_v  = float(row["compustat_value_usd"])
                print(f"  {row['company']:25s}  {row['tag']:8s}  {row['period_end']:12s}  {edgar_v:>18,.0f}  {comp_v:>18,.0f}  {row['pct_diff']:>8s}")
            except (ValueError, KeyError):
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Vergleicht EDGAR-DB mit Compustat Quarterly CSV.")
    parser.add_argument("--compustat-csv", default="data/1-Part-Compustat_NA.csv")
    parser.add_argument("--edgar-db", default="data/edgar_signals.db")
    parser.add_argument("--output", default="data/validation_report.csv")
    parser.add_argument("--cik", nargs="+", default=[])
    parser.add_argument("--tag", nargs="+", default=[])
    parser.add_argument("--min-year", default="2010")
    parser.add_argument("--max-year", default="2024")
    args = parser.parse_args()

    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from src.config import AppConfig
    watchlist = AppConfig().watchlist

    run_comparison(
        compustat_path=Path(args.compustat_csv),
        db_path=Path(args.edgar_db),
        output_path=Path(args.output),
        ciks=args.cik,
        tags=args.tag,
        min_year=args.min_year,
        max_year=args.max_year,
        watchlist=watchlist,
    )

if __name__ == "__main__":
    main()