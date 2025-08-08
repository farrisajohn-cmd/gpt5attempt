from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- helpers ----------
def _q2(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def fmt_currency(val: Decimal) -> str:
    return f"${_q2(val):,}"

def parse_number(x) -> Decimal:
    """
    Accepts things like:
      '$350k', '350k', '350,000', '1.2m', '100', '100.50', 'no', 'none'
    Returns Decimal(…)
    """
    if x is None:
        return Decimal("0")
    if isinstance(x, (int, float, Decimal)):
        return Decimal(str(x))

    s = str(x).strip().lower()
    if s in {"no", "none", "no hoa", ""}:
        return Decimal("0")

    s = s.replace("$", "").replace(",", "").replace(" ", "")

    mult = Decimal("1")
    if s.endswith("k"):
        mult = Decimal("1000")
        s = s[:-1]
    elif s.endswith("m"):
        mult = Decimal("1000000")
        s = s[:-1]

    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        raise ValueError(f"invalid numeric value: {x}")

    return Decimal(s) * mult

def parse_percent_or_number(x) -> Decimal:
    if x is None:
        return Decimal("0")
    s = str(x).strip().lower()
    if s.endswith("%"):
        s = s[:-1]
    return parse_number(s)

def parse_bool(x) -> bool:
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in {"true", "t", "yes", "y", "1"}

# --- mortgage math ---
def monthly_p_and_i(principal: Decimal, annual_rate_pct: Decimal) -> Decimal:
    r = (annual_rate_pct / Decimal("100")) / Decimal("12")
    n = Decimal(360)
    if r == 0:
        return principal / n
    factor = (Decimal(1) + r) ** n
    return principal * r * factor / (factor - Decimal(1))

def choose_rate(fico: int) -> Decimal:
    return Decimal("6.125")  # update if you want rate table

# ---------- endpoint ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.get("/fha-quote")
def fha_quote(
    purchase_price: str = Query(..., description="e.g. 350000, $350k, 1.2m"),
    down_payment_value: str = Query(..., description="e.g. 10% or 35000"),
    down_payment_is_percent: str = Query(..., description="true/false, yes/no"),
    fico: int = Query(...),
    monthly_taxes: str = Query(..., description="e.g. 350 or $350"),
    monthly_insurance: str = Query(..., description="e.g. 100 or $100"),
    hoa: Optional[str] = Query("0", description="e.g. 0, no, or $50"),
    property_type: str = Query(..., description="single-family, townhome, 1–4 unit"),
    lender_credit_percent: Optional[str] = Query("0", description="0/1/2/3"),
):
    # ---- tolerant parsing ----
    try:
        price = parse_number(purchase_price)
        dp_val_raw = down_payment_value
        dp_is_pct = parse_bool(down_payment_is_percent)
        dp_val = parse_percent_or_number(dp_val_raw)
        taxes = parse_number(monthly_taxes)
        ins = parse_number(monthly_insurance)
        hoa_amt = parse_number(hoa)
        lcp = parse_percent_or_number(lender_credit_percent or "0")
    except Exception as e:
        return {"output_markdown": f"there was a problem with your inputs: {e}"}

    # compute down payment and base loan
    down_payment = _q2(price * (dp_val / Decimal("100"))) if dp_is_pct else _q2(dp_val)
    base_loan = price - down_payment

    # ---- GUARDRAILS ----
    allowed = {
        "single-family", "single family", "sfr",
        "townhome", "townhouse", "rowhome",
        "1 unit", "2 unit", "3 unit", "4 unit", "1-4 unit", "1–4 unit"
    }
    ptype = property_type.strip().lower()
    if ptype not in allowed:
        return {"output_markdown": (
            "🚫 **we only quote fha loans for single-family, townhome, or 1–4 unit properties.**\n"
            "condos & manufactured homes aren’t supported."
        )}

    if fico < 640:
        return {"output_markdown": (
            "🚫 **minimum fico for this fha quote is 640.**\n"
            "if your score is 640+ i can re-run the numbers."
        )}

    if base_loan < Decimal("150000"):
        return {"output_markdown": (
            f"🚫 **base loan must be at least {fmt_currency(Decimal('150000'))}.**\n"
            f"your current base loan is {fmt_currency(base_loan)}."
        )}

    # ---- pricing ----
    ufmip = _q2(base_loan * Decimal("0.0175"))
    final_loan = base_loan + ufmip
    rate = choose_rate(fico)

    # monthly pieces
    p_i = monthly_p_and_i(final_loan, rate)
    mip_monthly = final_loan * Decimal("0.0055") / Decimal("12")
    pitia = p_i + mip_monthly + taxes + ins + hoa_amt

    # interim interest (15 days exact)
    daily_interest = final_loan * (rate / Decimal("100")) / Decimal("365")
    interim_interest = daily_interest * Decimal(15)

    # boxes
    box_a = Decimal("0.00")
    b_appraisal, b_credit, b_flood = Decimal("650"), Decimal("100"), Decimal("30")
    box_b = b_appraisal + b_credit + b_flood + ufmip

    c_title = Decimal("500")
    c_lender_title = final_loan * Decimal("0.0055")
    c_survey = Decimal("300")
    box_c = c_title + c_lender_title + c_survey

    e_recording, e_transfer = Decimal("299"), c_lender_title
    box_e = e_recording + e_transfer

    box_f = (ins * Decimal("12")) + interim_interest
    box_g = (taxes * Decimal("3")) + (ins * Decimal("3"))

    total_closing_costs = box_a + box_b + box_c + box_e + box_f + box_g

    # lender credit
    lender_credit_amt = _q2(final_loan * (lcp / Decimal("100")))
    total_after_credit = total_closing_costs - lender_credit_amt
    if total_after_credit < Decimal("0"):
        total_after_credit = Decimal("0")

    cash_to_close = down_payment + total_after_credit
    if cash_to_close < down_payment:
        cash_to_close = down_payment

    # APR
    apr_est = (rate + Decimal("1.06")).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

    # output
    md = f"""
🧾 **FHA Loan Estimate — govies.com**

your actual rate, payment, and costs could be higher. get an official loan estimate before choosing a loan.

🏠 **purchase price:** {fmt_currency(price)}
🏡 **property type:** {property_type}
💵 **final loan amount (incl. ufmip):** {fmt_currency(final_loan)}
📉 **interest rate:** {rate}%
📈 **est. apr (incl. mip & costs):** {apr_est}%
📆 **monthly payment (pitia):** {fmt_currency(pitia)}
💰 **estimated cash to close:** {fmt_currency(cash_to_close)}

📦 **itemized closing costs**
**box a – origination charges (subtotal: {fmt_currency(box_a)}):**
- lender origination fee: {fmt_currency(Decimal('0'))}

**box b – cannot shop for (subtotal: {fmt_currency(box_b)}):**
- appraisal: {fmt_currency(b_appraisal)}
- credit report: {fmt_currency(b_credit)}
- flood certification: {fmt_currency(b_flood)}
- ufmip: {fmt_currency(ufmip)}

**box c – can shop for (subtotal: {fmt_currency(box_c)}):**
- title settlement: {fmt_currency(c_title)}
- lender’s title insurance (0.55%): {fmt_currency(c_lender_title)}
- survey: {fmt_currency(c_survey)}

**box e – government fees (subtotal: {fmt_currency(box_e)}):**
- recording: {fmt_currency(e_recording)}
- transfer tax (matches lender’s title): {fmt_currency(e_transfer)}

**box f – prepaids (subtotal: {fmt_currency(box_f)}):**
- insurance (12 months): {fmt_currency(ins * Decimal('12'))}
- interim interest (15 days): {fmt_currency(interim_interest)}

**box g – initial escrow (subtotal: {fmt_currency(box_g)}):**
- taxes × 3: {fmt_currency(taxes * Decimal('3'))}
- insurance × 3: {fmt_currency(ins * Decimal('3'))}

**lender credit applied:** {fmt_currency(lender_credit_amt)}
**total closing costs:** {fmt_currency(total_after_credit)}

💵 **calculating cash to close**
- down payment: {fmt_currency(down_payment)}
- closing costs: {fmt_currency(total_after_credit)}
- estimated cash to close: {fmt_currency(cash_to_close)}
""".strip()

    return {"output_markdown": md}
