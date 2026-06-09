from __future__ import annotations

import logging
import math
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

# What every signal function receives.
# Built by the pipeline from storage.get_time_series().
# Key = Compustat tag name ("at", "ni", ...),
# Value = DataFrame with columns: period_end (str), value (float)
FactData = dict[str, pd.DataFrame]

# Every signal function has this exact signature.
SignalFn = Callable[[FactData], dict[str, float | None]]

def _series(data: FactData, var: str, period_type: str) -> pd.Series:
    """
    Core loader: extract a Series for one variable from FactData.

    Filters by period_type ("A" for annual, "Q" for quarterly).
    Deduplicates on period_end (last entry wins — matches storage upsert logic).
    Sets period_end as index, sorts ascending (oldest → newest).
    Returns empty Series if variable not in data.
    """
    if var not in data:
        return pd.Series(dtype=float, name=var)

    df = data[var].copy()
    df = df[df["period_type"] == period_type]
    df = df.drop_duplicates(subset="period_end", keep="last")
    df = df.set_index("period_end").sort_index()
    return df["value"].astype(float)


def _a(data: FactData, var: str) -> pd.Series:
    """Annual series (period_type = 'A')."""
    return _series(data, var, "A")


def _q(data: FactData, var: str) -> pd.Series:
    """Quarterly series (period_type = 'Q')."""
    return _series(data, var, "Q")


def _fill(s: pd.Series, idx: pd.Index) -> pd.Series:
    """
    Reindex to idx, fill missing with 0.

    Use for optional variables in a formula: if the firm has no preferred stock,
    pstk is missing from FactData — treat as zero rather than propagating NaN.
    """
    return s.reindex(idx, fill_value=0.0)


def _avg_at(at: pd.Series) -> pd.Series:
    """(at_t + at_{t-1}) / 2  — standard denominator for accrual signals."""
    return (at + at.shift(1)) / 2


def _scale_avg_at(numerator: pd.Series, at: pd.Series) -> pd.Series:
    """Scale by average total assets. Replaces 0 denominator with NaN."""
    return numerator / _avg_at(at).replace(0, float("nan"))


def _out(s: pd.Series) -> dict[str, float | None]:
    """Drop NaN and return as dict keyed by period_end string."""
    return {k: v for k, v in s.dropna().items()}


# ─────────────────────────────────────────────────────────────────────────────
# Annual signals  (use period_type = "A")
# ─────────────────────────────────────────────────────────────────────────────

def asset_growth(data: FactData) -> dict[str, float | None]:
    """
    AssetGrowth — Cooper, Gulen and Schill (2008), JF

    Formula:  (at_t - at_{t-1}) / at_{t-1}

    Firms that grew assets aggressively tend to underperform.
    Reflects overinvestment and empire-building.

    T-stat: 8.45   Direction: -1
    """
    at = _a(data, "at")
    if at.empty:
        return {}
    return _out(at.pct_change())


def del_coa(data: FactData) -> dict[str, float | None]:
    """
    DelCOA — Richardson et al. (2005), JAE

    Formula:  Δ(act - che) / avg(at)

    Current operating assets = current assets minus cash.
    Increases without cash inflows = accruals → low returns.

    T-stat: 8.71   Direction: -1
    """
    act = _a(data, "act")
    che = _a(data, "che")
    at  = _a(data, "at")
    if act.empty or at.empty:
        return {}
    idx  = at.index
    coa  = act - _fill(che, idx)
    return _out(_scale_avg_at(coa - coa.shift(1), at))


def del_col(data: FactData) -> dict[str, float | None]:
    """
    DelCOL — Richardson et al. (2005), JAE

    Formula:  Δ(lct - dlc) / avg(at)

    Current operating liabilities = current liabilities minus debt.
    Increases = accruals (reverse sign to DelCOA).

    T-stat: 4.49   Direction: -1
    """
    lct = _a(data, "lct")
    dlc = _a(data, "dlc")
    at  = _a(data, "at")
    if lct.empty or at.empty:
        return {}
    idx = at.index
    col = lct - _fill(dlc, idx)
    return _out(_scale_avg_at(col - col.shift(1), at))


def del_finl(data: FactData) -> dict[str, float | None]:
    """
    DelFINL — Richardson et al. (2005), JAE

    Formula:  Δ(dltt + dlc + pstk) / avg(at)

    Change in financial liabilities = new debt issuance.
    High external financing predicts low returns.

    T-stat: 8.01   Direction: -1
    """
    dltt = _a(data, "dltt")
    at   = _a(data, "at")
    if dltt.empty or at.empty:
        return {}
    idx  = at.index
    finl = (_fill(dltt, idx)
            + _fill(_a(data, "dlc"),  idx)
            + _fill(_a(data, "pstk"), idx))
    return _out(_scale_avg_at(finl - finl.shift(1), at))


def del_equ(data: FactData) -> dict[str, float | None]:
    """
    DelEqu — Richardson et al. (2005), JAE

    Formula:  Δceq / avg(at)

    Change in book equity. Equity issuance predicts low returns.

    T-stat: 6.25   Direction: -1
    """
    ceq = _a(data, "ceq")
    at  = _a(data, "at")
    if ceq.empty or at.empty:
        return {}
    return _out(_scale_avg_at(ceq - ceq.shift(1), at))


def del_net_fin(data: FactData) -> dict[str, float | None]:
    """
    DelNetFin — Richardson et al. (2005), JAE

    Formula:  Δ(ivst + ivao - dltt - dlc - pstk) / avg(at)

    Change in net financial assets (financial assets minus financial liabilities).
    Decrease = external financing → low returns.

    T-stat: 5.85   Direction: +1
    """
    at = _a(data, "at")
    if at.empty:
        return {}
    idx = at.index
    nfa = (_fill(_a(data, "ivst"), idx) + _fill(_a(data, "ivao"), idx)
           - _fill(_a(data, "dltt"), idx) - _fill(_a(data, "dlc"),  idx)
           - _fill(_a(data, "pstk"), idx))
    return _out(_scale_avg_at(nfa - nfa.shift(1), at))


def composite_debt_issuance(data: FactData) -> dict[str, float | None]:
    """
    CompositeDebtIssuance — Lyandres, Sun and Zhang (2008), RFS

    Formula:  log(dltt + dlc)_t  -  log(dltt + dlc)_{t-5}

    5-year growth in total debt. Firms that raised a lot of debt underperform
    as the investment boom fades.

    T-stat: 8.59   Direction: -1
    """
    dltt = _a(data, "dltt")
    if dltt.empty:
        return {}
    idx   = dltt.index
    total = (_fill(dltt, idx) + _fill(_a(data, "dlc"), idx)).clip(lower=1e-6)
    log_t = total.apply(math.log)
    return _out(log_t - log_t.shift(5))


def noa(data: FactData) -> dict[str, float | None]:
    """
    NOA — Hirshleifer et al. (2004), JAE

    Formula:  (oa - ol) / at_{t-1}
      oa = at - che
      ol = at - dltt - mib - dc - ceq

    Net operating assets scaled by lagged total assets.
    High NOA = accruals accumulated over time → low returns.

    NOTE: requires 'dc' (convertible debt, added to config.XBRL_TAGS).
    If dc is missing, treated as 0.

    T-stat: 8.45   Direction: -1
    """
    at  = _a(data, "at")
    ceq = _a(data, "ceq")
    if at.empty or ceq.empty:
        return {}
    idx      = at.index
    oa       = at - _fill(_a(data, "che"),  idx)
    ol       = (at
                - _fill(_a(data, "dltt"), idx)
                - _fill(_a(data, "mib"),  idx)
                - _fill(_a(data, "dc"),   idx)   # 0 if not available
                - ceq)
    lagged   = at.shift(1).replace(0, float("nan"))
    return _out((oa - ol) / lagged)


def invest_ppe_inv(data: FactData) -> dict[str, float | None]:
    """
    InvestPPEInv — Lyandres, Sun and Zhang (2008), RFS

    Formula:  (Δppegt + Δinvt) / at_{t-1}

    Investment in PP&E plus inventory growth scaled by lagged assets.
    High investment predicts low returns (q-theory).

    T-stat: 7.13   Direction: -1
    """
    ppegt = _a(data, "ppegt")
    at    = _a(data, "at")
    if ppegt.empty or at.empty:
        return {}
    idx    = at.index
    d_ppe  = _fill(ppegt,            idx) - _fill(ppegt,            idx).shift(1)
    d_inv  = _fill(_a(data, "invt"), idx) - _fill(_a(data, "invt"), idx).shift(1)
    lagged = at.shift(1).replace(0, float("nan"))
    return _out((d_ppe + d_inv) / lagged)


def net_debt_finance(data: FactData) -> dict[str, float | None]:
    """
    NetDebtFinance — Bradshaw, Richardson, Sloan (2006), JAE

    Formula:  (dltis - dltr - dlcch) / avg(at)

    Net debt financing = new debt minus repayments.
    Firms that raise net debt underperform.

    T-stat: 6.91   Direction: -1
    """
    at = _a(data, "at")
    if at.empty:
        return {}
    idx = at.index
    ndf = (_fill(_a(data, "dltis"), idx)
           - _fill(_a(data, "dltr"),  idx)
           - _fill(_a(data, "dlcch"), idx))
    return _out(_scale_avg_at(ndf, at))


def xfin(data: FactData) -> dict[str, float | None]:
    """
    XFIN (Net External Financing) — Bradshaw, Richardson, Sloan (2006), JAE

    Formula:  (sstk - dv - prstkc + dltis - dltr) / at

    Most comprehensive external financing measure.

    T-stat: 5.70   Direction: -1
    """
    at = _a(data, "at")
    if at.empty:
        return {}
    idx = at.index
    net = (_fill(_a(data, "sstk"),   idx)
           - _fill(_a(data, "dv"),     idx)
           - _fill(_a(data, "prstkc"), idx)
           + _fill(_a(data, "dltis"),  idx)
           - _fill(_a(data, "dltr"),   idx))
    return _out(net / at.replace(0, float("nan")))


def accruals(data: FactData) -> dict[str, float | None]:
    """
    Accruals — Sloan (1996), AR

    Formula:  [Δ(act - che) - Δ(lct - dlc) - Δtxp] / avg(at)

    Working capital accruals: accounting income minus cash income.
    High accruals predict earnings reversals and low returns.
    One of the most replicated accounting anomalies.

    NOTE: 'txp' (taxes payable) is not in the standard config — treated as 0.
    Add "txp": ["TaxesPayableCurrent"] to XBRL_TAGS for full accuracy.

    T-stat: 4.71   Direction: -1
    """
    act = _a(data, "act")
    lct = _a(data, "lct")
    at  = _a(data, "at")
    if act.empty or at.empty:
        return {}
    idx = at.index
    wc  = (act - _fill(_a(data, "che"), idx)) - (lct - _fill(_a(data, "dlc"), idx))
    txp = _fill(_a(data, "txp"), idx)
    acc = (wc - wc.shift(1)) - (txp - txp.shift(1))
    return _out(_scale_avg_at(acc, at))


def total_accruals(data: FactData) -> dict[str, float | None]:
    """
    TotalAccruals — Richardson et al. (2005), JAE

    Formula (post-1988, CFS available):
      (ni - oancf - ivncf - fincf + sstk - prstkc - dv) / at_{t-1}

    Broader accruals including long-term (capex, acquisitions).

    T-stat: 6.38   Direction: -1
    """
    ni = _a(data, "ni")
    at = _a(data, "at")
    if ni.empty or at.empty:
        return {}
    idx    = at.index
    ta     = (ni
              - _fill(_a(data, "oancf"),  idx)
              - _fill(_a(data, "ivncf"),  idx)
              - _fill(_a(data, "fincf"),  idx)
              + _fill(_a(data, "sstk"),   idx)
              - _fill(_a(data, "prstkc"), idx)
              - _fill(_a(data, "dv"),     idx))
    lagged = at.shift(1).replace(0, float("nan"))
    return _out(ta / lagged)


def roe(data: FactData) -> dict[str, float | None]:
    """
    RoE — Haugen and Baker (1996), JFE

    Formula:  ni / ceq   (only where ceq > 0)

    Profitable firms earn higher returns.

    T-stat: 4.50   Direction: +1
    """
    ni  = _a(data, "ni")
    ceq = _a(data, "ceq")
    if ni.empty or ceq.empty:
        return {}
    valid = ceq[ceq > 0]
    return _out(ni.reindex(valid.index) / valid)


def book_leverage(data: FactData) -> dict[str, float | None]:
    """
    BookLeverage — Fama and French (1992), JF

    Formula:  at / (ceq + txditc + pstk)

    High leverage → debated direction; following FF1992 here.

    T-stat: 5.34   Direction: -1
    """
    at  = _a(data, "at")
    ceq = _a(data, "ceq")
    if at.empty or ceq.empty:
        return {}
    idx   = at.index
    denom = (ceq
             + _fill(_a(data, "txditc"), idx)
             + _fill(_a(data, "pstk"),   idx)).replace(0, float("nan"))
    return _out(at / denom)


def cheq(data: FactData) -> dict[str, float | None]:
    """
    ChEQ — Lockwood and Prombutr (2010), JFR

    Formula:  ceq_t / ceq_{t-1}   (both > 0 required)

    Equity growth as proxy for external financing and empire-building.

    T-stat: 5.38   Direction: -1
    """
    ceq = _a(data, "ceq")
    if ceq.empty:
        return {}
    ratio    = ceq / ceq.shift(1)
    both_pos = (ceq > 0) & (ceq.shift(1) > 0)
    return _out(ratio[both_pos])


def grcapx(data: FactData) -> dict[str, float | None]:
    """
    grcapx — Anderson and Garcia-Feijoo (2006), JF

    Formula:  (capx_t - capx_{t-2}) / |capx_{t-2}|

    Two-year capex growth. Rapid growth predicts low returns (overinvestment).

    T-stat: 5.05   Direction: -1
    """
    capx = _a(data, "capx")
    if capx.empty:
        return {}
    denom = capx.shift(2).abs().replace(0, float("nan"))
    return _out((capx - capx.shift(2)) / denom)


def grcapx3y(data: FactData) -> dict[str, float | None]:
    """
    grcapx3y — Anderson and Garcia-Feijoo (2006), JF

    Formula:  capx_t / (capx_{t-1} + capx_{t-2} + capx_{t-3})

    Three-year capex growth alternative.

    T-stat: 4.71   Direction: -1
    """
    capx = _a(data, "capx")
    if capx.empty:
        return {}
    denom = (capx.shift(1) + capx.shift(2) + capx.shift(3)).replace(0, float("nan"))
    return _out(capx / denom)


def inv_growth(data: FactData) -> dict[str, float | None]:
    """
    InvGrowth — Belo and Lin (2012), RFS

    Formula:  (invt_t - invt_{t-1}) / invt_{t-1}

    Inventory growth. High accumulation predicts weak earnings → low returns.

    T-stat: 6.64   Direction: -1
    """
    invt = _a(data, "invt")
    if invt.empty:
        return {}
    return _out(invt.pct_change())


def ch_asset_turnover(data: FactData) -> dict[str, float | None]:
    """
    ChAssetTurnover — Soliman (2008), AR

    Formula:  (sale/at)_t - (sale/at)_{t-1}

    Change in asset efficiency. Improving turnover signals better management.

    T-stat: 5.12   Direction: +1
    """
    sale = _a(data, "sale")
    at   = _a(data, "at")
    if sale.empty or at.empty:
        return {}
    ato = sale.reindex(at.index) / at.replace(0, float("nan"))
    return _out(ato - ato.shift(1))


def ch_nwc(data: FactData) -> dict[str, float | None]:
    """
    ChNWC — Soliman (2008), AR

    Formula:  Δ[(act - che) - (lct - dlc)] / at

    Change in net working capital. Buildup without revenue growth = poor performance.

    T-stat: 4.61   Direction: -1
    """
    act = _a(data, "act")
    lct = _a(data, "lct")
    at  = _a(data, "at")
    if act.empty or at.empty:
        return {}
    idx = at.index
    nwc = (act - _fill(_a(data, "che"), idx)) - (lct - _fill(_a(data, "dlc"), idx))
    return _out((nwc - nwc.shift(1)) / at.replace(0, float("nan")))


def ch_nncoa(data: FactData) -> dict[str, float | None]:
    """
    ChNNCOA — Soliman (2008), AR

    Formula:  Δ[(at - act - ivao) - (lt - dlc - dltt)] / at

    Change in net non-current operating assets.
    Captures long-term accruals missed by working capital measures.

    T-stat: 5.26   Direction: -1
    """
    at   = _a(data, "at")
    lt   = _a(data, "lt")
    if at.empty or lt.empty:
        return {}
    idx   = at.index
    nncoa = ((at - _fill(_a(data, "act"),  idx) - _fill(_a(data, "ivao"), idx))
             - (lt - _fill(_a(data, "dlc"),  idx) - _fill(_a(data, "dltt"), idx)))
    scaled = nncoa / at.replace(0, float("nan"))
    return _out(scaled - scaled.shift(1))


def conv_debt(data: FactData) -> dict[str, float | None]:
    """
    ConvDebt — Valta (2016), JFQA

    Formula:  1 if dc > 0, else 0

    Indicator for convertible debt outstanding.
    Requires 'dc' tag in config.XBRL_TAGS:
        "dc": ["ConvertibleDebtNoncurrent", "ConvertibleDebt"]

    T-stat: 4.50   Direction: -1
    """
    dc = _a(data, "dc")
    at = _a(data, "at")
    if dc.empty or at.empty:
        return {}
    flag = (_fill(dc, at.index) > 0).astype(float)
    return _out(flag)


def ps(data: FactData) -> dict[str, float | None]:
    """
    PS (Piotroski F-Score) — Piotroski (2000), JAR

    9-indicator composite:
      Profitability (4):       ROA>0, CFO>0, ΔROA>0, CFO>ROA
      Leverage/Liquidity (3):  Δlev<0, Δcurrent_ratio>0, no share issuance
      Efficiency (2):          Δmargin>0, Δturnover>0

    High score → strong fundamentals → better returns (among value stocks).
    We compute unconditionally; BM filtering is done at portfolio level.

    T-stat: 5.59   Direction: +1

    Implementation note:
    All intermediate series are explicitly reindexed to idx (= at.index)
    before any comparison. This is necessary because ib, sale, txt etc.
    may start later than at (e.g. ib from 2011, at from 2008), producing
    Series with different labels. Comparing two such Series with > raises
    "Can only compare identically-labeled Series objects".
    _fill() handles missing variables (→ 0); reindex() handles sparse ones (→ NaN).
    """
    ib   = _a(data, "ib")
    at   = _a(data, "at")
    sale = _a(data, "sale")
    if ib.empty or at.empty or sale.empty:
        return {}

    # idx is the master index — every intermediate series is aligned to it.
    # at has the broadest coverage among required variables, so we use its index.
    idx  = at.index
    nan  = float("nan")

    # All inputs aligned to idx:
    at_   = at.replace(0, nan)
    ib_   = ib.reindex(idx)                                   # NaN where missing
    oancf = _fill(_a(data, "oancf"), idx)
    dltt  = _fill(_a(data, "dltt"),  idx)
    act_  = _fill(_a(data, "act"),   idx)
    lct_  = _fill(_a(data, "lct"),   idx).replace(0, nan)
    txt_  = _fill(_a(data, "txt"),   idx)
    xint_ = _fill(_a(data, "xint"),  idx)
    csho_ = _fill(_a(data, "csho"),  idx)
    sale_ = sale.reindex(idx).replace(0, nan)

    # Derived ratios — all on idx, NaN propagates naturally:
    roa    = ib_   / at_
    cfo    = oancf / at_
    lev    = dltt  / at_
    cur    = act_  / lct_
    ebit   = ib_ + txt_ + xint_
    margin = ebit  / sale_
    ato    = sale_ / at_

    # F-score: each indicator is 0 or 1; sum → 0–9
    # .astype(float) converts bool to float so pandas can sum them cleanly.
    # Rows where both sides are NaN produce NaN (correct — no data, no score).
    f = (  (roa > 0).astype(float)              # F1  ROA positive
         + (cfo > 0).astype(float)              # F2  CFO positive
         + (roa > roa.shift(1)).astype(float)   # F3  ROA improving
         + (cfo > roa).astype(float)            # F4  CFO > ROA (accrual quality)
         + (lev < lev.shift(1)).astype(float)   # F5  leverage fell
         + (cur > cur.shift(1)).astype(float)   # F6  liquidity improved
         + (csho_ <= csho_.shift(1)).astype(float)  # F7  no dilution
         + (margin > margin.shift(1)).astype(float)  # F8  margin improved
         + (ato > ato.shift(1)).astype(float))  # F9  turnover improved

    return _out(f)


def ent_mult(data: FactData) -> dict[str, float | None]:
    """
    EntMult (Enterprise Multiple) — Loughran and Wellman (2011), JFQA

    Formula:  (ME + dltt + dlc + dc - che) / oibdp

    PLACEHOLDER — requires market cap (ME = price × shares).
    Market data is not available from SEC EDGAR alone.
    Returns {} until a price data source is integrated.

    T-stat: 6.54   Direction: -1
    """
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Quarterly signals  (use period_type = "Q")
# ─────────────────────────────────────────────────────────────────────────────

def ch_tax(data: FactData) -> dict[str, float | None]:
    """
    ChTax — Thomas and Zhang (2011), JAR

    Formula:  (txt_t - txt_{t-4}) / at_{t-1}   (quarterly, year-over-year)

    4-quarter change in tax expense scaled by lagged assets.
    Taxes are hard to manipulate → high ChTax signals real earnings growth.
    Highest t-stat in this signal set.

    T-stat: 11.26   Direction: +1

    Note: uses period_type="Q" for both txt and at.
    Shift(4) = same quarter one year ago (corrects for seasonality).
    """
    txt_q = _q(data, "txt")
    at_q  = _q(data, "at")
    if txt_q.empty or at_q.empty:
        return {}
    dtax   = txt_q - txt_q.shift(4)
    lagged = at_q.shift(1).replace(0, float("nan"))
    return _out(dtax.reindex(lagged.index) / lagged)


def roaq(data: FactData) -> dict[str, float | None]:
    """
    roaq — Balakrishnan, Bartov and Faurel (2010), JAE

    Formula:  ib_t / at_{t-1}   (quarterly)

    Quarterly return on assets. Updated 4× per year — more timely than annual ROA.

    T-stat: 6.45   Direction: +1
    """
    ib_q = _q(data, "ib")
    at_q = _q(data, "at")
    if ib_q.empty or at_q.empty:
        return {}
    lagged = at_q.shift(1).replace(0, float("nan"))
    return _out(ib_q.reindex(lagged.index) / lagged)


# ─────────────────────────────────────────────────────────────────────────────
# Signal registry
# ─────────────────────────────────────────────────────────────────────────────
# The pipeline imports this dict and runs every function in it.
# To disable a signal temporarily: comment out its line here.

SIGNAL_REGISTRY: dict[str, SignalFn] = {
    # ── Annual ──────────────────────────────────────────────────────────────
    "AssetGrowth":           asset_growth,
    "DelCOA":                del_coa,
    "DelCOL":                del_col,
    "DelFINL":               del_finl,
    "DelEqu":                del_equ,
    "DelNetFin":             del_net_fin,
    "CompositeDebtIssuance": composite_debt_issuance,
    "NOA":                   noa,
    "InvestPPEInv":          invest_ppe_inv,
    "NetDebtFinance":        net_debt_finance,
    "XFIN":                  xfin,
    "Accruals":              accruals,
    "TotalAccruals":         total_accruals,
    "RoE":                   roe,
    "BookLeverage":          book_leverage,
    "ChEQ":                  cheq,
    "grcapx":                grcapx,
    "grcapx3y":              grcapx3y,
    "InvGrowth":             inv_growth,
    "ChAssetTurnover":       ch_asset_turnover,
    "ChNWC":                 ch_nwc,
    "ChNNCOA":               ch_nncoa,
    "ConvDebt":              conv_debt,
    "PS":                    ps,
    "EntMult":               ent_mult,       # placeholder, always returns {}
    # ── Quarterly ───────────────────────────────────────────────────────────
    "ChTax":                 ch_tax,
    "roaq":                  roaq,
}