from dataclasses import dataclass
from datetime import date
from config import INSTANT_TAGS


@dataclass
class FactRecord:
    cik: str
    tag: str           # Compustat-Name
    period_end: date   # Date of the fact's period end
    period_type: str   # "Q" (Quarterly) or "A" (Annual)
    value: float
    source_form: str   # "10-Q" or "10-K"


def is_duration(fact: dict) -> bool:
    return "start" in fact


def classify_period(start_str: str, end_str: str) -> str:
    """
    Take two ISO strings, calculate the difference in days, and return the period category.
    Allows for a small tolerance (days +/- 15) since quarters can vary.

    Return values:
    - "Q1"   (~90 days:  actual first quarter / 1-quarter YTD)
    - "H1"   (~180 days: half-year / 2-quarter YTD)
    - "9M"   (~270 days: 3-quarter YTD)
    - "A"    (~365 days: annual / full fiscal year)
    - "SKIP" (unknown / noise — e.g. stub periods)
    """
    start_d = date.fromisoformat(start_str)
    end_d   = date.fromisoformat(end_str)
    days    = (end_d - start_d).days

    if  75 <= days <= 105:  return "Q1"
    if 165 <= days <= 195:  return "H1"
    if 255 <= days <= 285:  return "9M"
    if 350 <= days <= 380:  return "A"
    return "SKIP"


def resolve_tag(facts_json: dict, tag_priorities: list[str]) -> list[dict]:
    """
    Search the facts JSON for the best matching SEC tag based on our priority list.
    Returns the list of fact entries for the first matching tag, or [] if none found.
    """
    us_gaap_data = facts_json.get("facts", {}).get("us-gaap", {})
    for sec_tag in tag_priorities:
        if sec_tag in us_gaap_data:
            units_dict = us_gaap_data[sec_tag].get("units", {})
            for unit_key, facts_list in units_dict.items():
                return facts_list
    return []


def ytd_to_quarterly(cik: str, compustat_tag: str, records: list) -> list[FactRecord]:
    """
    Convert YTD duration facts (GuV / Cash Flow Statement) into:
      - One "Q" FactRecord per actual quarter (via differencing)
      - One "A" FactRecord per fiscal year (the full cumulative annual value)

    Why two records for the year-end date?
      - "A" is needed by annual signals (asset_growth, accruals, etc.)
        which call _a(data, "at") and filter on period_type == "A"
      - "Q" for Q4 is needed by quarterly signals (roaq, ch_tax)
        which call _q(data, "at") and filter on period_type == "Q"
      Both can coexist because UNIQUE(cik, tag, period_end, period_type).
    """
    # Group all duration facts by fiscal year start date
    by_fy: dict[str, list[dict]] = {}
    for rec in records:
        if not is_duration(rec):
            continue
        by_fy.setdefault(rec["start"], []).append(rec)

    result: list[FactRecord] = []

    for fy_start, fy_records in by_fy.items():
        # Build lookup: period_type → (value, end_date, form)
        # When duplicates exist for the same period type, keep the most
        # recently *filed* entry (amended filings supersede originals).
        lookup: dict[str, tuple[float, str, str]] = {}
        for rec in fy_records:
            ptype = classify_period(rec["start"], rec["end"])
            if ptype == "SKIP":
                continue
            existing = lookup.get(ptype)
            if existing is None or rec["filed"] > existing[2]:
                lookup[ptype] = (float(rec["val"]), rec["end"], rec["form"])

        q1_data = lookup.get("Q1")
        h1_data = lookup.get("H1")
        nm_data = lookup.get("9M")
        a_data  = lookup.get("A")

        # Q1: directly the YTD value
        if q1_data:
            result.append(FactRecord(
                cik=cik, tag=compustat_tag,
                period_end=date.fromisoformat(q1_data[1]),
                period_type="Q", value=q1_data[0], source_form=q1_data[2],
            ))

        # Q2: H1-YTD minus Q1-YTD
        if h1_data and q1_data:
            result.append(FactRecord(
                cik=cik, tag=compustat_tag,
                period_end=date.fromisoformat(h1_data[1]),
                period_type="Q", value=h1_data[0] - q1_data[0], source_form=h1_data[2],
            ))

        # Q3: 9M-YTD minus H1-YTD
        if nm_data and h1_data:
            result.append(FactRecord(
                cik=cik, tag=compustat_tag,
                period_end=date.fromisoformat(nm_data[1]),
                period_type="Q", value=nm_data[0] - h1_data[0], source_form=nm_data[2],
            ))

        # Annual year-end: two records 
        if a_data:
            # 1. Full cumulative annual value → for annual signals (_a())
            result.append(FactRecord(
                cik=cik, tag=compustat_tag,
                period_end=date.fromisoformat(a_data[1]),
                period_type="A", value=a_data[0], source_form=a_data[2],
            ))
            # 2. Isolated Q4 value → for quarterly signals (_q())
            #    Only possible when 9M data exists for differencing.
            if nm_data:
                result.append(FactRecord(
                    cik=cik, tag=compustat_tag,
                    period_end=date.fromisoformat(a_data[1]),
                    period_type="Q", value=a_data[0] - nm_data[0], source_form=a_data[2],
                ))

    return sorted(result, key=lambda r: r.period_end)


def extract_instant(cik: str, compustat_tag: str, records: list) -> list[FactRecord]:
    """
    For Balance Sheet tags (Instant facts, no 'start' key):
      - 10-K entries → period_type = "A"  (year-end balance sheet value)
      - 10-Q entries → period_type = "Q"  (quarter-end balance sheet value)

    Deduplicates by period_end: most recently *filed* entry wins.
    """
    # lookup: period_end → (value, filed_date, form)
    lookup: dict[str, tuple[float, str, str]] = {}
    for rec in records:
        if is_duration(rec):
            continue
        if rec.get("form") not in {"10-K", "10-Q"}:
            continue
        end = rec["end"]
        if end not in lookup or rec["filed"] > lookup[end][1]:
            lookup[end] = (float(rec["val"]), rec["filed"], rec["form"])

    result = []
    for end, (val, filed, form) in lookup.items():
        # 10-K = fiscal year-end → "A";  10-Q = quarter-end → "Q"
        period_type = "A" if form == "10-K" else "Q"
        result.append(FactRecord(
            cik=cik, tag=compustat_tag,
            period_end=date.fromisoformat(end),
            period_type=period_type,
            value=val,
            source_form=form,
        ))

    return sorted(result, key=lambda r: r.period_end)


def normalize_tag(cik: str, compustat_tag: str, records: list) -> list[FactRecord]:
    """
    Dispatcher: routes to the correct normalization function.
    This is the only function pipeline.py needs to call.

    - INSTANT_TAGS (Balance Sheet) → extract_instant
    - Everything else (GuV, CFS)   → ytd_to_quarterly
    """
    if compustat_tag in INSTANT_TAGS:
        return extract_instant(cik, compustat_tag, records)
    return ytd_to_quarterly(cik, compustat_tag, records)