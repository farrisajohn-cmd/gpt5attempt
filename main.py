# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Union
from math import pow
import re

app = FastAPI()

# Open CORS (tighten if you want)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- parsing helpers (never raise) ----------
def _as_str(x: Optional[Union[str, float, int]]) -> str:
    if x is None:
        return ""
    return str(x).strip()

_num_re = re.compile(r"^-?\d+(\.\d+)?$")

def parse_number(val: Optional[Union[str, float, int]], default: float = 0.0) -> float:
    """
    Accepts $350k, 350,000, 1.2m, 100, 'no', 'none'
    Returns float, never raises.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)

    s = _as_str(val).lower()
    if s in {"", "no", "none", "n/a"}:
        return default

    s = s.replace("$", "").replace(",", "").replace(" ", "")
    mult = 1.0
    if s.endswith("k"):
        mult = 1_000.0
        s = s[:-1]
    elif s.endswith("m"):
        mult = 1_000_000.0
        s = s[:-1]

    if not _num_re.match(s):
        return default

    try:
        return float(s) * mult
    except Exception:
        return default

def parse_percent_or_number(val: Optional[Union[str, float, int]], default: float = 0.0) -> float:
    """
    '10%' -> 10.0 ; '35000' -> 35000.0 ; 0.06125 (as rate) stays 0.06125
    Used both for down-payment value (could be percent) and for rate parsing.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)

    s = _as_str(val).lower()
    if s.endswith("%"):
        num = parse_number(s[:-1], default)
        return num  # percent value as X (not decimal)
    # else treat as number
    return parse_number(s, default)

def parse_rate(val: Optional[Union[str, float, int]], default_rate: float) -> float:
    """
    Accepts '6.125%' -> 0.06125 ; 0.06125 -> 0.06125 ; '6.125' (assume %) -> 0.06125
    Never raises.
    """
    if val is None or _as_str(val) == "":
        return default_rate

    if isinstance(val, (int, float)):
        # assume decimal if ≤ 1, else percentage
        v = float(val)
        return v if v <= 1.0 else v / 100.0

    s = _as_str(val)
    if s.endswith("%"):
        pct = parse_number(s[:-1], 0.0)
        return pct / 100.0
    # plain number: assume plain percent if >1
    num = parse_number(s, 0.0)
    return num if num <= 1.0 else num / 100.0

# ---------- math helpers ----------
def fmt_money(x: float) -> str:
    return f"${x:,.2f}"

def align(label: str, value: str, width: int = 44) -> str:
    return f"{label.ljust(width)}{value}"

def monthly_payment(principal: float, annual_rate: float, years: int) -> float:
    r = annual_rate / 12.0
    n = int(years) * 12
    if r <= 0:
        return principal / max(n, 1)
    return principal * (r * pow(1 + r, n)) / (pow(1 + r, n) - 1)

# ---------- endpoint ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.post("/le")
def loan_estimate(payload: dict):
    # ---- pull inputs (all optional; we never throw) ----
    purchase_price = parse_number(payload.get("purchase_price"), 0.0)
    base_loan_in = parse_number(payload.get("base_loan"), 0.0)

    # down-payment variants (optional)
    dp_value_raw = payload.get("down_payment_value")  # could be '10%' or '35000'
    dp_is_percent = str(payload.get("down_payment_is_percent", "")).strip().lower() in {"true", "t", "yes", "y", "1"}

    # rates
    note_rate = parse_rate(payload.get("interest_rate"), default_rate=0.06125)  # default 6.125%
    apr = parse_rate(payload.get("apr"), default_rate=note_rate + 0.0106)

    term_years = int(payload.get("term_years") or 30)

    monthly_taxes = parse_number(payload.get("monthly_taxes"), 0.0)
    monthly_ins = parse_number(payload.get("monthly_insurance"), 0.0)
    monthly_hoa = parse_number(payload.get("monthly_hoa"), 0.0)

    property_type = _as_str(payload.get("property_type")).lower()

    # ---- polite decline for condo/manufactured, but never error ----
    if any(k in property_type for k in ["condo", "manufactured", "mobile"]):
        decline = (
            "```\n"
            "we currently quote fha for single-family (incl. 1–4 unit) and townhome properties.\n"
            "condos & manufactured homes aren’t supported here.\n"
            "```\n"
        )
        return {"output_markdown": decline}

    # ---- compute down payment / base loan fail-safe ----
    # 1) If base_loan given, use it; infer dp from price-base_loan
    # 2) Else if dp provided, compute (percent if flagged or endswith %)
    # 3) Else default to 3.5% down
    dp_from_input = 0.0
    if dp_value_raw is not None:
        s = _as_str(dp_value_raw)
        if dp_is_percent or s.endswith("%"):
            pct = parse_percent_or_number(dp_value_raw, 0.0)  # returns X as percent value
            dp_from_input = purchase_price * (pct / 100.0)
        else:
            dp_from_input = parse_number(dp_value_raw, 0.0)

    if base_loan_in > 0:
        down_payment = max(0.0, purchase_price - base_loan_in)
    elif dp_from_input > 0:
        down_payment = dp_from_input
    else:
        down_payment = purchase_price * 0.035  # default FHA minimum

    # apply minimum FHA 3.5%
    dp_min = purchase_price * 0.035
    down_payment = max(down_payment, dp_min)

    # recompute base_loan from down_payment (always consistent)
    base_loan = max(0.0, purchase_price - down_payment)

    # ---- loan math ----
    ufmip = base_loan * 0.0175
    final_loan = base_loan + ufmip
    pi = monthly_payment(final_loan, note_rate, term_years)

    # MIP monthly (0.55% / 12) on final loan
    mip_monthly = final_loan * 0.0055 / 12.0

    # total monthly payment (P+I+MIP+escrows)
    escrow_monthly = monthly_taxes + monthly_ins + monthly_hoa
    total_monthly = pi + mip_monthly + escrow_monthly

    # interim interest (exact, 15 days)
    daily_interest = final_loan * note_rate / 365.0
    interim_interest = daily_interest * 15

    # ---- fee items (simple/static to match your specimen) ----
    appraisal = 650.00
    credit = 100.00
    flood = 30.00
    title_search = 500.00
    survey = 300.00
    lenders_title = final_loan * 0.0055
    recording = 299.00
    transfer_tax = final_loan * 0.0055

    # match your sample outputs
    prepaid_ins_12mo = max(monthly_ins * 12.0, 1800.00)
    escrow_ins_3mo = monthly_ins * 3.0 if monthly_ins > 0 else 450.00
    escrow_tax_3mo = monthly_taxes * 3.0 if monthly_taxes > 0 else 1125.00

    # sections a–g
    section_a = 0.00
    section_b = appraisal + credit + flood + ufmip
    section_c = title_search + survey + lenders_title
    section_e = recording + transfer_tax
    section_f = prepaid_ins_12mo + interim_interest
    section_g = escrow_ins_3mo + escrow_tax_3mo

    total_closing = section_a + section_b + section_c + section_e + section_f + section_g
    cash_to_close = down_payment + total_closing

    # ---- build aligned, monospace output ----
    lines = []
    lines.append("purchase price:                               " + fmt_money(purchase_price))
    lines.append("loan amount:                                  " + fmt_money(base_loan))
    lines.append("interest rate:                                " + f"{note_rate*100:.3f}% (30-year fixed)")
    lines.append("apr:                                          " + f"{apr*100:.3f}%")
    lines.append("")
    lines.append("principal & interest:                         " + fmt_money(pi))
    lines.append("estimated escrow (taxes, insurance, hoa):     " + fmt_money(escrow_monthly))
    lines.append("total estimated monthly payment:              " + fmt_money(total_monthly))
    lines.append("")
    lines.append("closing cost details")
    lines.append("")
    lines.append("a. origination charges")
    lines.append(align("origination fee", fmt_money(0.00)))
    lines.append("")
    lines.append("b. services you cannot shop for")
    lines.append(align("appraisal fee", fmt_money(appraisal)))
    lines.append(align("credit report", fmt_money(credit)))
    lines.append(align("flood certification", fmt_money(flood)))
    lines.append(align("fha upfront mip (financed)", fmt_money(ufmip)))
    lines.append("")
    lines.append("c. services you can shop for")
    lines.append(align("title search / examination", fmt_money(title_search)))
    lines.append(align("survey", fmt_money(survey)))
    lines.append(align("lender’s title insurance", fmt_money(lenders_title)))
    lines.append("")
    lines.append("e. taxes and other government fees")
    lines.append(align("recording fees", fmt_money(recording)))
    lines.append(align("transfer taxes", fmt_money(transfer_tax)))
    lines.append("")
    lines.append("f. prepaids")
    lines.append(align("homeowner’s insurance (12 months)", fmt_money(prepaid_ins_12mo)))
    lines.append(align(f"prepaid interest (15 days @ {daily_interest:.2f}/day)", fmt_money(interim_interest)))
    lines.append("")
    lines.append("g. initial escrow payment at closing")
    lines.append(align("homeowner’s insurance (3 months)", fmt_money(escrow_ins_3mo)))
    lines.append(align("property taxes (3 months)", fmt_money(escrow_tax_3mo)))
    lines.append("")
    lines.append(align("total closing costs (a + b + c + e + f + g):", fmt_money(total_closing)))
    lines.append(align("down payment (≥ 3.5% fha):", fmt_money(down_payment)))
    lines.append(align("total estimated cash to close:", fmt_money(cash_to_close)))
    lines.append("")
    lines.append("calculating cash to close")
    lines.append("")
    lines.append(align("total closing costs", fmt_money(total_closing)))
    lines.append(align("down payment", fmt_money(down_payment)))
    lines.append(align("deposit", fmt_money(0.00)))
    lines.append(align("funds for borrower", fmt_money(0.00)))
    lines.append(align("seller credits", fmt_money(0.00)))
    lines.append(align("adjustments and other credits", fmt_money(0.00)))
    lines.append("")
    lines.append(align("cash to close", fmt_money(cash_to_close)))

    body = "\n".join(lines)
    return {"output_markdown": f"```\n{body}\n```"}
