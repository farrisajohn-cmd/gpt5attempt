from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from decimal import Decimal, ROUND_HALF_UP, getcontext

app = FastAPI()
getcontext().prec = 28  # high precision for exact math

# --- CORS: allow frontend origins ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://govies.com",
        "https://www.govies.com",
        "https://*.vercel.app",
        "http://localhost:3000",
        "https://www.chatbase.co",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Decimal helpers ----------
TWOPL = Decimal("0.01")
def d(x): return x if isinstance(x, Decimal) else Decimal(str(x))
def q2(x): return d(x).quantize(TWOPL, rounding=ROUND_HALF_UP)

# ---------- Mortgage calculations ----------
def monthly_p_and_i(principal: Decimal, annual_rate_pct: Decimal) -> Decimal:
    r = (annual_rate_pct / Decimal(100)) / Decimal(12)
    n = Decimal(360)
    if r == 0:
        return principal / n
    factor = (Decimal(1) + r) ** n
    return principal * r * factor / (factor - Decimal(1))

def choose_rate(fico: int) -> Decimal:
    # Current table per your spec: all buckets 6.125%
    return Decimal("6.125")

# ---------- Models ----------
class QuoteIn(BaseModel):
    purchase_price: Decimal
    down_payment_value: Decimal
    down_payment_is_percent: bool
    fico: int
    monthly_taxes: Decimal
    monthly_insurance: Decimal
    hoa: Decimal | None = Decimal("0")
    # NEW: optional lender credit percent (e.g., 1, 2, 3)
    lender_credit_percent: Decimal | None = None

class QuoteOut(BaseModel):
    output_markdown: str

# ---------- Health route ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}

# ---------- FHA Quote route ----------
@app.post("/fha-quote", response_model=QuoteOut)
def fha_quote(payload: QuoteIn):
    price = d(payload.purchase_price)
    dp_val = d(payload.down_payment_value)
    dp = price * (dp_val / Decimal(100)) if payload.down_payment_is_percent else dp_val
    base_loan = price - dp
    ufmip = base_loan * Decimal("0.0175")
    final_loan = base_loan + ufmip

    rate = choose_rate(payload.fico)

    # Step 3 – interim interest: daily × 15 (no pre-round)
    daily_interest = final_loan * (rate / Decimal(100)) / Decimal(365)
    interim_interest = daily_interest * Decimal(15)

    # Step 4 – monthly MIP
    mip = final_loan * Decimal("0.0055") / Decimal(12)

    # P&I (30-yr)
    p_i = monthly_p_and_i(final_loan, rate)

    hoa_amt = d(payload.hoa or 0)
    pitia = p_i + mip + d(payload.monthly_taxes) + d(payload.monthly_insurance) + hoa_amt

    # Step 6 – itemized boxes
    box_a = Decimal("0.00")

    b_appraisal, b_credit, b_flood = Decimal("650"), Decimal("100"), Decimal("30")
    box_b = b_appraisal + b_credit + b_flood + ufmip

    c_title, c_survey = Decimal("500"), Decimal("300")
    c_lender_title = final_loan * Decimal("0.0055")
    box_c = c_title + c_lender_title + c_survey

    e_recording, e_transfer = Decimal("299"), c_lender_title
    box_e = e_recording + e_transfer

    box_f = (d(payload.monthly_insurance) * Decimal(12)) + interim_interest
    box_g = (d(payload.monthly_taxes) * Decimal(3)) + (d(payload.monthly_insurance) * Decimal(3))

    total_closing = box_b + box_c + box_e + box_f + box_g

    # --- NEW: lender credit application (placeholder logic) ---
    credit_amt = Decimal("0")
    if payload.lender_credit_percent is not None:
        # credit = % of final loan, capped at total closing costs
        credit_amt = (final_loan * (d(payload.lender_credit_percent) / Decimal(100)))
        if credit_amt > total_closing:
            credit_amt = total_closing

    net_closing = total_closing - credit_amt

    # Step 7 – cash to close (never below required down payment)
    cash_to_close = dp + net_closing
    if cash_to_close < dp:
        cash_to_close = dp

    # Build markdown output (keep order/format per your spec)
    # NOTE: we keep the same $xx.xx format (no commas) to match your previous output exactly.
    credit_line = (
        f"**lender credit ({q2(payload.lender_credit_percent)}%):** -${q2(credit_amt)}\n"
        if payload.lender_credit_percent is not None else ""
    )

    md = f"""🧾 **FHA Loan Estimate — govies.com**

🏠 **purchase price:** ${q2(price)}
💵 **final loan amount (incl. ufmip):** ${q2(final_loan)}
📉 **interest rate:** {rate}%
📆 **monthly payment (pitia):** ${q2(pitia)}
💰 **estimated cash to close:** ${q2(cash_to_close)}

**📦 itemized closing costs**
**box a – origination:** ${q2(box_a)}
**box b – cannot shop:** ${q2(box_b)}
**box c – can shop:** ${q2(box_c)}
**box e – gov’t fees:** ${q2(box_e)}
**box f – prepaids:** ${q2(box_f)}
**box g – escrow:** ${q2(box_g)}
**total closing costs:** ${q2(total_closing)}
{credit_line}**💵 calculating cash to close**
**down payment:** ${q2(dp)}
**closing costs:** ${q2(net_closing)}
**estimated cash to close:** ${q2(cash_to_close)}

please review this estimate and consult with us if you'd like to move forward.
- 🔗 https://govies.com/apply
- 📅 https://govies.com/consult
- 📞 1-800-YES-GOVIES
- ✉️ team@govies.com
"""
    return QuoteOut(output_markdown=md)
