from dataclasses import dataclass, field

# Use this to check explicitly for a company or companies you want to monitor.
# Firms to monitor: CIK (zero-padded to 10 digits)
# Find CIKs at https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
WATCHLIST: dict[str, str] = {
    "0000320193": "Apple Inc.",
    "0000789019": "Microsoft Corp.",
    "0001652044": "Alphabet Inc.",
    "0001018724": "Amazon.com Inc.",
    "0001326801": "Meta Platforms Inc.",
}

# Your contact info for the SEC User-Agent header (required by SEC policy).
CONTACT_EMAIL: str = "david.steimel02@gmail.com"
PROJECT_NAME: str  = "Edgar Signal Harvester"

# User-Agent for SEC API requests
USER_AGENT: str = f"{PROJECT_NAME} {CONTACT_EMAIL}"

POLL_INTERVAL_SECONDS: int = 3600        # every hour by default
MIN_POLL_INTERVAL_SECONDS: int = 300     # minimum 5 minutes

TRIGGER_FORMS: set[str] = {"10-K", "10-Q"}

DB_PATH: str = "data/edgar_signals.db"

# ---------------------------------------------------------------------------
# Internal constants – do not edit
# ---------------------------------------------------------------------------
 
EDGAR_BASE       = "https://data.sec.gov"
SUBMISSIONS_URL  = EDGAR_BASE + "/submissions/CIK{cik}.json"
FACTS_URL        = EDGAR_BASE + "/api/xbrl/companyfacts/CIK{cik}.json"
CONCEPT_URL      = EDGAR_BASE + "/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"

XBRL_TAGS: dict[str, list[str]] = {


    "at": ["Assets"],

    "act": ["AssetsCurrent"],

    "che": [
        "CashCashEquivalentsAndShortTermInvestments",
        "CashAndCashEquivalentsAtCarryingValue",
    ],

    "lct": ["LiabilitiesCurrent"],

    "lt": ["Liabilities"],

    "dltt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermNotesPayable",
    ],

    "dlc": [
        "DebtCurrent",
        "LongTermDebtCurrent",
        "CurrentPortionOfLongTermDebt",
        "NotesPayableCurrent",
        "ShortTermBorrowings",
    ],

    "ceq": [
        "StockholdersEquity",
        "StockholdersEquityAttributableToParent",
    ],
    "seq": [
        "StockholdersEquity",
        "StockholdersEquityAttributableToParent",
        "LiabilitiesAndStockholdersEquity", 
    ],

    "pstk": [
        "PreferredStockValue",
        "PreferredStockCarryingAmountNonredeemable",
    ],

    "txditc": [
        "DeferredTaxLiabilitiesNoncurrent",
        "DeferredIncomeTaxLiabilitiesNet",
    ],

    "invt": [
        "InventoryNet",
        "InventoryGross",
    ],

    "ppegt": ["PropertyPlantAndEquipmentGross"],

    "ppent": [
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ],

    "ivao": [
        "LongTermInvestments",
        "EquityMethodInvestments",
        "OtherAssetsNoncurrent", 
    ],

    "ivst": [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
    ],

    "mib": [
        "MinorityInterest",
        "NoncontrollingInterestInNetAssetsOfConsolidatedEntities",
        "RedeemableNoncontrollingInterestEquityCarryingAmount",
    ],

    "recta": [
        "AccountsReceivableNetCurrent",
        "ReceivablesNetCurrent",
        "AccountsAndOtherReceivablesNetCurrent",
    ],

    "csho": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ],

    "txndb": [
        "DeferredTaxAssetsNet",
        "DeferredIncomeTaxAssetsNet",
    ],

    "re": [
        "RetainedEarningsAccumulatedDeficit",
    ],

    "gdwl": [
        "Goodwill",
    ],

    "intan": [
        "IntangibleAssetsNetExcludingGoodwill",
        "FiniteLivedIntangibleAssetsNet",
    ],

    "ni": ["NetIncomeLoss"],

    "ib": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
    ],

    "sale": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueGoodsNet",
    ],

    "txt": ["IncomeTaxExpenseBenefit"],

    "txdi": [
        "DeferredIncomeTaxExpenseBenefit",
        "DeferredIncomeTaxesAndTaxCredits",
    ],

    "xint": [
        "InterestExpense",
        "InterestExpenseDebt",
        "InterestAndDebtExpense",
    ],

    "dp": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "Depreciation",
    ],

    "oibdp": [
        "OperatingIncomeLoss",
    ],

    "xrd": ["ResearchAndDevelopmentExpense"],

    "xad": [
        "AdvertisingExpense",
        "MarketingExpenseToRevenue",
        "SellingAndMarketingExpense",
    ],

    "cogs": [
        "CostOfGoodsSold",
        "CostOfRevenue",
        "CostOfGoodsAndServicesSold",
    ],

    "xsga": [
        "SellingGeneralAndAdministrativeExpense",
        "OperatingExpenses", 
        "GeneralAndAdministrativeExpense",
    ],

    "epspx": [
        "EarningsPerShareBasic",
    ],
    "epsfx": [
        "EarningsPerShareDiluted",
    ],


    "oancf": ["NetCashProvidedByUsedInOperatingActivities"],

    "ivncf": ["NetCashProvidedByUsedInInvestingActivities"],

    "fincf": ["NetCashProvidedByUsedInFinancingActivities"],

    "capx": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireOtherProductiveAssets",
    ],

    "dv": [
        "PaymentsOfDividends",
        "PaymentsOfDividendsCommonStock",
    ],

    "dvp": ["PaymentsOfDividendsPreferredStockAndPreferenceStock"],

    "sstk": [
        "ProceedsFromIssuanceOfCommonStock",
        "ProceedsFromIssuanceOfEquity",
    ],
    "prstkc": [
        "PaymentsForRepurchaseOfCommonStock",
        "PaymentsForRepurchaseOfEquity",
    ],

    "dltis": [
        "ProceedsFromIssuanceOfLongTermDebt",
        "ProceedsFromIssuanceOfDebt",
    ],
    "dltr": [
        "RepaymentsOfLongTermDebt",
        "RepaymentsOfDebt", 
    ],
    "dlcch": [
        "ProceedsFromRepaymentsOfShortTermDebt",
        "ProceedsFromShortTermDebt",
        "RepaymentsOfShortTermDebt",
    ],

    "pstkissp": [
        "ProceedsFromIssuanceOfPreferredStockAndPreferenceStock",
    ],

}

INSTANT_TAGS: set[str] = {
    "at", "act", "che", "lct", "lt", "dltt", "dlc",
    "ceq", "seq", "pstk", "txditc", "invt", "ppegt",
    "ppent", "ivao", "ivst", "mib", "recta", "csho",
    "txndb", "re", "gdwl", "intan",
}

@dataclass(frozen=True, slots=True)
class AppConfig:
    watchlist: dict[str, str]            = field(default_factory=lambda: WATCHLIST)
    poll_interval: int                   = POLL_INTERVAL_SECONDS
    trigger_forms: set[str]              = field(default_factory=lambda: TRIGGER_FORMS)
    db_path: str                         = DB_PATH
    user_agent: str                      = USER_AGENT
    xbrl_tags: dict[str, list[str]]      = field(default_factory=lambda: XBRL_TAGS)