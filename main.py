from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from decimal import Decimal, ROUND_HALF_UP, getcontext

app = FastAPI()
getcontext().prec = 28  # high precision for exact math

# --- CORS ---
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
def money(x): return f"${q2(x)}"  # keep two decimals, no commas to match prior output

# ---------- Mortgage calculations ----------
def monthly_p_and_i(principal: Decimal, annual_rate_pct: Decimal) -> Decimal:
    r = (annual_rate_pct / Decimal(100)) / Decimal(12)
    n = Decimal(360)
    if r == 0:
        return principal / n
    factor = (Decimal(1) + r) ** n
    return principal * r * factor / (factor - Decimal(1))

def choose_rate(fico: int) -> Decimal:
    # current table: all buckets 6.125%
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
    # optional lender credit (% of final loan), e.g. 1, 2, or 3
    lender_credit_percent: Decimal | None = None

class QuoteOut(BaseModel):
    output_markdown: str

# ---------- Health ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}

# ---------- FHA Quote ----------
@app.post("/fha-quote", response_model=QuoteOut)
def fha_quote(payload: QuoteIn):
    price = d(payload.purchase_price)
    dp_val = d(payload.down_payment_value)
    dp = price * (dp_val / Decimal(100)) if payload.down_payment_is_percent else dp_val
    base_loan = price - dp
    ufmip = base_loan * Decimal("0.0175")
    final_loan = base_loan + ufmip

    rate = choose_rate(payload.fico)

    # interim interest: daily × 15 (no pre-round)
    daily_interest = final_loan * (rate / Decimal(100)) / Decimal(365)
    interim_interest = daily_interest * Decimal(15)

    # MIP + P&I
    mip = final_loan * Decimal("0.0055") / Decimal(12)
    p_i = monthly_p_and_i(final_loan, rate)

    hoa_amt = d(payload.hoa or 0)
    pitia = p_i + mip + d(payload.monthly_taxes) + d(payload.monthly_insurance) + hoa_amt

    # ---------------- Itemized fees per Box ----------------
    # Box A – Origination
    a_origination = Decimal("0.00")
    box_a = a_origination

    # Box B – Cannot Shop For
    b_appraisal = Decimal("650.00")
    b_credit_report = Decimal("100.00")
    b_flood_cert = Decimal("30.00")
    b_ufmip = ufmip
    box_b = b_appraisal + b_credit_report + b_flood_cert + b_ufmip

    # Box C – Can Shop For
    c_title_settlement = Decimal("500.00")
    c_lenders_title = final_loan * Decimal("0.0055")
    c_survey = Decimal("300.00")
    box_c = c_title_settlement + c_lenders_title + c_survey

    # Box E – Government Fees
    e_recording = Decimal("299.00")
    e_transfer_tax = c_lenders_title  # per spec: same as lender’s title
    box_e = e_recording + e_transfer_tax

    # Box F – Prepaids
    f_insurance_12mo = d(payload.monthly_insurance) * Decimal(12)
    f_interim_interest_15d = interim_interest
    box_f = f_insurance_12mo + f_interim_interest_15d

    # Box G – Initial Escrow
    g_taxes_3mo = d(payload.monthly_taxes) * Decimal(3)
    g_insurance_3mo = d(payload.monthly_insurance) * Decimal(3)
    box_g = g_taxes_3mo + g_insurance_3mo

    total_closing = box_b + box_c + box_e + box_f + box_g + box_a  # include A explicitly

    # --- optional lender credit (placeholder logic) ---
    credit_amt = Decimal("0.00")
    if payload.lender_credit_percent is not None:
        credit_amt = final_loan * (d(payload.lender_credit_percent) / Decimal(100))
        if credit_amt > total_closing:
            credit_amt = total_closing

    net_closing = total_closing - credit_amt

    # cash to close (never below dp)
    cash_to_close = dp + net_closing
    if cash_to_close < dp:
        cash_to_close = dp

    # ---------------- Markdown ----------------
    disclaimer = "your actual rate, payment, and costs could be higher. get an official loan estimate before choosing a loan."

    # itemized lines with box subtotal in header
    md = f"""🧾 **FHA Loan Estimate — govies.com**

_{disclaimer}_

🏠 **purchase price:** {money(price)}
💵 **final loan amount (incl. ufmip):** {money(final_loan)}
📉 **interest rate:** {rate}%
📆 **monthly payment (pitia):** {money(pitia)}
💰 **estimated cash to close:** {money(cash_to_close)}

**📦 itemized closing costs**
**box a – origination charges (subtotal: {money(box_a)}):**
- lender origination fee: {money(a_origination)}

**box b – cannot shop for (subtotal: {money(box_b)}):**
- appraisal: {money(b_appraisal)}
- credit report: {money(b_credit_report)}
- flood certification: {money(b_flood_cert)}
- ufmip: {money(b_ufmip)}

**box c – can shop for (subtotal: {money(box_c)}):**
- title settlement: {money(c_title_settlement)}
- lender’s title insurance (0.55%): {money(c_lenders_title)}
- survey: {money(c_survey)}

**box e – government fees (subtotal: {money(box_e)}):**
- recording: {money(e_recording)}
- transfer tax (matches lender’s title): {money(e_transfer_tax)}

**box f – prepaids (subtotal: {money(box_f)}):**
- insurance (12 months): {money(f_insurance_12mo)}
- interim interest (15 days): {money(f_interim_interest_15d)}

**box g – initial escrow (subtotal: {money(box_g)}):**
- taxes × 3: {money(g_taxes_3mo)}
- insurance × 3: {money(g_insurance_3mo)}

**total closing costs:** {money(total_closing)}
"""

    if credit_amt > 0:
        md += f"""**lender credit ({q2(payload.lender_credit_percent)}%):** -{money(credit_amt)}
**net closing costs after credit:** {money(net_closing)}
"""

    md += f"""
**💵 calculating cash to close**
**down payment:** {money(dp)}
**closing costs:** {money(net_closing if credit_amt > 0 else total_closing)}
**estimated cash to close:** {money(cash_to_close)}

please review this estimate and consult with us if you'd like to move forward.
- 🔗 https://govies.com/apply
- 📅 https://govies.com/consult
- 📞 1-800-YES-GOVIES
- ✉️ team@govies.com
"""

    return QuoteOut(output_markdown=md)
