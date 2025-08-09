from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP
import re

app = FastAPI()

# CORS (keep open; tighten later if you like)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Helpers -----------------
TWOPL = Decimal("0.01")
_NUM_PAT = re.compile(r"-?\d+(\.\d+)?")

def q2(x: Decimal) -> Decimal:
    return x.quantize(TWOPL, rounding=ROUND_HALF_UP)

def fmt_currency(x: Decimal) -> str:
    return f"${q2(x):,}"

def parse_number(x) -> Decimal:
    """
    Accepts '$350k', '350k', '1.2m', '350,000', '100', 0, False, None,
    'no', 'none', 'no hoa', 'null', arrays/objects (treated as 0).
    """
    if x in (None, "", 0, 0.0, False, [], {}, ()):
        return Decimal("0")
    if isinstance(x, (int, float, Decimal)):
        return Decimal(str(x))
    s = str(x).strip().lower()
    if s in {"no", "none", "no hoa", "n/a", "na", "nil", "null"}:
        return Decimal("0")
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    mult = Decimal("1")
    if s.endswith("k"): mult = Decimal("1000"); s = s[:-1]
    elif s.endswith("m"): mult = Decimal("1000000"); s = s[:-1]
    if not _NUM_PAT.fullmatch(s or "0"):
        return Decimal("0")  # soft-fail to zero
    return Decimal(s) * mult

def parse_percent_or_number(x) -> Decimal:
    if x is None:
        return Decimal("0")
    s = str(x).strip().lower()
    if s.endswith("%"):
        s = s[:-1]
    return parse_number(s)

def parse_bool_soft(x) -> Optional[bool]:
    if x is None:
        return None
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in {"true", "t", "yes", "y", "1"}: return True
    if s in {"false", "f", "no", "n", "0"}: return False
    return None

def monthly_p_and_i(principal: Decimal, annual_rate_pct: Decimal) -> Decimal:
    r = (annual_rate_pct / Decimal("100")) / Decimal("12")
    n = Decimal(360)
    if r == 0:
        return principal / n
    factor = (Decimal(1) + r) ** n
    return principal * r * factor / (factor - Decimal(1))

def choose_rate(fico: int) -> Decimal:
    return Decimal("6.125")  # your placeholder grid

# ----------------- “Lean AF” property check -----------------
# Only deny if it smells like condo or manufactured/mobile
DENY_TOKENS = {
    "condo", "condominium",
    "manufactured", "mobile", "mobilehome", "mh", "manuf", "manufactured-home"
}

def is_denied_property(raw: Optional[str]) -> bool:
    if not raw:
        return False
    s = re.sub(r"\s+", "", raw.strip().lower())
    return any(tok in s for tok in DENY_TOKENS)

def show_property(raw: Optional[str]) -> str:
    if not raw:
        return "unspecified"
    return raw.strip().title()

# ----------------- Models -----------------
class QuoteInRaw(BaseModel):
    purchase_price: Optional[str] = None
    down_payment_value: Optional[str] = None
    down_payment_is_percent: Optional[str] = None
    fico: Optional[str] = None   # accept as string; we coerce
    monthly_taxes: Optional[str] = None
    monthly_insurance: Optional[str] = None
    hoa: Optional[str] = "0"
    property_type: Optional[str] = None
    lender_credit_percent: Optional[str] = "0"

class QuoteOut(BaseModel):
    output_markdown: str

# ----------------- Endpoints -----------------
@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.post("/fha-quote", response_model=QuoteOut)
def fha_quote(payload: QuoteInRaw):
    # Parse/clean inputs (tolerant, never 500)
    try:
        fico_val = int(str(payload.fico).strip()) if payload.fico is not None else None
        price = parse_number(payload.purchase_price)
        dp_val = parse_percent_or_number(payload.down_payment_value)

        # infer percent if missing/ambiguous
        dp_is_pct = parse_bool_soft(payload.down_payment_is_percent)
        if dp_is_pct is None:
            raw_dp = (payload.down_payment_value or "").strip().lower()
            dp_is_pct = "%" in raw_dp

        taxes = parse_number(payload.monthly_taxes)
        ins = parse_number(payload.monthly_insurance)
        hoa_amt = parse_number(payload.hoa if payload.hoa is not None else "0")
        lcp = parse_percent_or_number(payload.lender_credit_percent or "0")
        denied = is_denied_property(payload.property_type)
    except Exception as e:
        return QuoteOut(output_markdown=f"there was a problem with your inputs: **{e}**")

    # Missing-field nudge (don’t crash)
    missing = []
    if fico_val is None: missing.append("credit score")
    if price == Decimal("0") and (payload.purchase_price or "") == "": missing.append("purchase price")
    if (payload.down_payment_value or "").strip() == "": missing.append("down payment")
    if taxes == Decimal("0") and (payload.monthly_taxes or "") == "": missing.append("monthly taxes")
    if ins == Decimal("0") and (payload.monthly_insurance or "") == "": missing.append("monthly insurance")
    if (payload.property_type or "").strip() == "": missing.append("property type")
    if missing:
        return QuoteOut(output_markdown=f"i need a bit more info: please provide **{', '.join(missing)}**.")

    # Simple guardrails
    if denied:
        return QuoteOut(output_markdown=(
            "🚫 **we don’t quote or originate condos or manufactured/mobile homes.**\n"
            "i can help right away with single-family, duplex, triplex, quadplex, or townhome."
        ))

    down_payment = q2(price * (dp_val / Decimal("100"))) if dp_is_pct else q2(dp_val)
    base_loan = price - down_payment

    if fico_val < 640:
        return QuoteOut(output_markdown=(
            "🚫 **minimum fico for this fha quote is 640.**\n"
            "if your score is 640+ i can re-run the numbers."
        ))

    if base_loan < Decimal("150000"):
        return QuoteOut(output_markdown=(
            f"🚫 **base loan must be at least {fmt_currency(Decimal('150000'))}.**\n"
            f"your current base loan is {fmt_currency(base_loan)}."
        ))

    # Pricing
    ufmip = q2(base_loan * Decimal("0.0175"))
    final_loan = base_loan + ufmip
    rate = choose_rate(fico_val)

    p_i = monthly_p_and_i(final_loan, rate)
    mip_monthly = final_loan * Decimal("0.0055") / Decimal("12")
    pitia = p_i + mip_monthly + taxes + ins + hoa_amt

    # 15-day interim interest (no pre-round)
    daily_interest = final_loan * (rate / Decimal("100")) / Decimal("365")
    interim_interest = daily_interest * Decimal(15)

    # Itemized boxes
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

    # Lender credit (0/1/2/3 % of final_loan)
    lender_credit_amt = q2(final_loan * (lcp / Decimal("100")))
    total_after_credit = total_closing_costs - lender_credit_amt
    if total_after_credit < 0:
        total_after_credit = Decimal("0")

    cash_to_close = down_payment + total_after_credit
    if cash_to_close < down_payment:
        cash_to_close = down_payment

    # APR: note rate + 1.06%
    apr_est = q2(rate + Decimal("1.06"))
    shown_ptype = show_property(payload.property_type)

    md = f"""
🧾 **FHA Loan Estimate — govies.com**

your actual rate, payment, and costs could be higher. get an official loan estimate before choosing a loan.

🏠 **purchase price:** {fmt_currency(price)}
🏡 **property type:** {shown_ptype}
💳 **fico:** {fico_val}
💵 **final loan amount (incl. ufmip):** {fmt_currency(final_loan)}
📉 **interest rate:** {rate}%
📈 **est. apr (incl. mip & costs):** {apr_est}%
📆 **monthly payment (pitia):** {fmt_currency(pitia)}
💰 **estimated cash to close:** {fmt_currency(cash_to_close)}

**📦 itemized closing costs**

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
**total closing costs (after credit):** {fmt_currency(total_after_credit)}

**💵 calculating cash to close**
- down payment: {fmt_currency(down_payment)}
- closing costs: {fmt_currency(total_after_credit)}
- estimated cash to close: {fmt_currency(cash_to_close)}

please review this estimate and consult with us if you'd like to move forward.
- 🔗 https://govies.com/apply
- 📅 https://govies.com/consult
- 📞 1-800-YES-GOVIES
- ✉️ team@govies.com
""".strip()

    return QuoteOut(output_markdown=md)
