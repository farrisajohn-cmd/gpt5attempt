from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from decimal import Decimal, ROUND_HALF_UP
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- helpers ----------
TWOPL = Decimal("0.01")
_num_pat = re.compile(r"-?\d+(\.\d+)?")

def q2(x: Decimal) -> Decimal:
    return x.quantize(TWOPL, rounding=ROUND_HALF_UP)

def fmt_currency(x: Decimal) -> str:
    return f"${q2(x):,}"

def parse_number(x) -> Decimal:
    """
    Accepts '$350k', '350k', '1.2m', '350,000', '100', 'no', 'none', '', etc.
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

    if not _num_pat.fullmatch(s or "0"):
        raise ValueError(f"invalid numeric value: {x}")

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
    if s in {"true", "t", "yes", "y", "1"}:
        return True
    if s in {"false", "f", "no", "n", "0"}:
        return False
    return None  # unknown

def monthly_p_and_i(principal: Decimal, annual_rate_pct: Decimal) -> Decimal:
    r = (annual_rate_pct / Decimal("100")) / Decimal("12")
    n = Decimal(360)
    if r == 0:
        return principal / n
    factor = (Decimal(1) + r) ** n
    return principal * r * factor / (factor - Decimal(1))

def choose_rate(fico: int) -> Decimal:
    return Decimal("6.125")  # plug your grid later

# ---------- property type normalization ----------
PROPERTY_MAP = {
    # single-family
    "sfr": "single-family",
    "sfh": "single-family",
    "single family": "single-family",
    "single-family": "single-family",
    "single family home": "single-family",
    "single-family home": "single-family",
    "1 unit": "single-family",
    "1-unit": "single-family",
    # 1–4 unit umbrella
    "2 unit": "1-4 unit", "2-unit": "1-4 unit", "duplex": "1-4 unit",
    "3 unit": "1-4 unit", "3-unit": "1-4 unit", "triplex": "1-4 unit",
    "4 unit": "1-4 unit", "4-unit": "1-4 unit", "quadplex": "1-4 unit", "fourplex": "1-4 unit",
    "1–4 unit": "1-4 unit", "1-4 unit": "1-4 unit",
    # townhome
    "townhome": "townhome", "townhouse": "townhome", "town house": "townhome",
    "th": "townhome", "twnhm": "townhome",
}
ALLOWED_PROP_TYPES = {"single-family", "townhome", "1-4 unit"}
def normalize_property_type(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = raw.strip().lower()
    if s in PROPERTY_MAP:
        return PROPERTY_MAP[s]
    if "single" in s and "family" in s:
        return "single-family"
    if "town" in s:
        return "townhome"
    if any(tok in s for tok in ["1-4", "1–4", "duplex", "triplex", "quadplex", "fourplex", "2 unit", "3 unit", "4 unit"]):
        return "1-4 unit"
    return None

# ---------- models ----------
class QuoteInRaw(BaseModel):
    purchase_price: Optional[str] = None
    down_payment_value: Optional[str] = None
    down_payment_is_percent: Optional[str] = None  # may be missing
    fico: Optional[int] = None
    monthly_taxes: Optional[str] = None
    monthly_insurance: Optional[str] = None
    hoa: Optional[str] = "0"
    property_type: Optional[str] = None
    lender_credit_percent: Optional[str] = "0"

class QuoteOut(BaseModel):
    output_markdown: str

# ---------- endpoints ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.post("/fha-quote", response_model=QuoteOut)
def fha_quote(payload: QuoteInRaw):
    try:
        # tolerate strings or numbers
        fico_val = int(str(payload.fico).strip()) if payload.fico is not None else None

        price = parse_number(payload.purchase_price)
        dp_val = parse_percent_or_number(payload.down_payment_value)

        # infer percent if field is absent/empty: contains "%" OR obvious percent format
        dp_is_pct = parse_bool_soft(payload.down_payment_is_percent)
        if dp_is_pct is None:
            raw_dp = (payload.down_payment_value or "").strip().lower()
            dp_is_pct = "%" in raw_dp

        taxes = parse_number(payload.monthly_taxes)
        ins = parse_number(payload.monthly_insurance)
        hoa_amt = parse_number(payload.hoa or "0")
        lcp = parse_percent_or_number(payload.lender_credit_percent or "0")
        ptype = normalize_property_type(payload.property_type)
    except Exception as e:
        return QuoteOut(output_markdown=f"there was a problem with your inputs: **{e}**")

    # required checks first
    missing = []
    if fico_val is None: missing.append("credit score")
    if price == 0: missing.append("purchase price")
    if (payload.down_payment_value or "").strip() == "": missing.append("down payment")
    if taxes == 0 and (payload.monthly_taxes or "") == "": missing.append("monthly taxes")
    if ins == 0 and (payload.monthly_insurance or "") == "": missing.append("monthly insurance")
    if ptype is None: missing.append("property type")
    if missing:
        return QuoteOut(output_markdown=f"i need a bit more info: please provide **{', '.join(missing)}**.")

    # down payment & base loan
    down_payment = q2(price * (dp_val / Decimal("100"))) if dp_is_pct else q2(dp_val)
    base_loan = price - down_payment

    # guardrails
    if ptype not in ALLOWED_PROP_TYPES:
        return QuoteOut(
            output_markdown=(
                "🚫 **we only quote fha loans for single-family (incl. 1–4 unit) or townhome properties.**\n"
                "condos & manufactured homes aren’t supported."
            )
        )
    if fico_val < 640:
        return QuoteOut(
            output_markdown=(
                "🚫 **minimum fico for this fha quote is 640.**\n"
                "if your score is 640+ i can re-run the numbers."
            )
        )
    if base_loan < Decimal("150000"):
        return QuoteOut(
            output_markdown=(
                f"🚫 **base loan must be at least {fmt_currency(Decimal('150000'))}.**\n"
                f"your current base loan is {fmt_currency(base_loan)}."
            )
        )

    # pricing
    ufmip = q2(base_loan * Decimal("0.0175"))
    final_loan = base_loan + ufmip
    rate = choose_rate(fico_val)

    # monthly components
    p_i = monthly_p_and_i(final_loan, rate)
    mip_monthly = final_loan * Decimal("0.0055") / Decimal("12")
    pitia = p_i + mip_monthly + taxes + ins + hoa_amt

    # interim interest (15 days exact; no pre-round)
    daily_interest = final_loan * (rate / Decimal("100")) / Decimal("365")
    interim_interest = daily_interest * Decimal(15)

    # itemized boxes
    box_a = Decimal("0.00")
    b_appraisal = Decimal("650")
    b_credit = Decimal("100")
    b_flood = Decimal("30")
    box_b = b_appraisal + b_credit + b_flood + ufmip

    c_title = Decimal("500")
    c_lender_title = final_loan * Decimal("0.0055")
    c_survey = Decimal("300")
    box_c = c_title + c_lender_title + c_survey

    e_recording = Decimal("299")
    e_transfer = c_lender_title
    box_e = e_recording + e_transfer

    box_f = (ins * Decimal("12")) + interim_interest
    box_g = (taxes * Decimal("3")) + (ins * Decimal("3"))

    total_closing_costs = box_a + box_b + box_c + box_e + box_f + box_g

    # lender credit (0/1/2/3%)
    lender_credit_amt = q2(final_loan * (lcp / Decimal("100")))
    total_after_credit = total_closing_costs - lender_credit_amt
    if total_after_credit < Decimal("0"):
        total_after_credit = Decimal("0")

    cash_to_close = down_payment + total_after_credit
    if cash_to_close < down_payment:
        cash_to_close = down_payment

    # APR = note rate + 1.06%
    apr_est = q2(rate + Decimal("1.06"))

    shown_ptype = "Single-family" if ptype == "single-family" else ("Townhome" if ptype == "townhome" else "1–4 unit")

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
