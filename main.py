from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import math

app = FastAPI()

# Allow CORS for local dev / Vercel frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class FHAQuoteRequest(BaseModel):
    purchase_price: float
    down_payment: float  # can be % or $
    credit_score: int
    monthly_taxes: float
    monthly_insurance: float
    monthly_hoa: float = 0
    property_type: str

@app.post("/fha-quote")
async def fha_quote(data: FHAQuoteRequest):
    # Normalize property type
    prop_type = data.property_type.strip().lower()
    if prop_type == "sfr":
        prop_type = "single-family"

    valid_types = ["single-family", "townhome", "duplex", "triplex", "quadplex"]
    if prop_type not in valid_types:
        raise HTTPException(status_code=400, detail="only single-family 1–4 unit properties and townhomes are allowed — condos and manufactured homes are ineligible.")

    # Block low credit scores
    if data.credit_score < 640:
        raise HTTPException(status_code=400, detail="minimum credit score for FHA is 640.")

    # Determine down payment in dollars
    if data.down_payment < 1:  # entered as decimal %
        dp_amount = data.purchase_price * data.down_payment
    elif data.down_payment < 100:  # entered as whole %
        dp_amount = data.purchase_price * (data.down_payment / 100)
    else:  # entered as dollars
        dp_amount = data.down_payment

    base_loan = data.purchase_price - dp_amount

    if base_loan < 150000:
        raise HTTPException(status_code=400, detail="minimum base loan amount is $150,000.")

    # FHA calc
    ufmip = base_loan * 0.0175
    final_loan = base_loan + ufmip
    rate = 0.06125
    apr_rate = rate + 0.0106

    # Monthly P&I
    monthly_rate = rate / 12
    months = 360
    monthly_pi = final_loan * (monthly_rate * math.pow(1 + monthly_rate, months)) / (math.pow(1 + monthly_rate, months) - 1)

    # MIP
    mip_monthly = (final_loan * 0.0055) / 12

    # PITIA
    total_monthly = monthly_pi + mip_monthly + data.monthly_taxes + data.monthly_insurance + data.monthly_hoa

    # Interim interest (15 days)
    daily_interest = final_loan * rate / 365
    interim_interest = daily_interest * 15

    # Closing costs
    box_a = 0.00
    box_b_appraisal = 650.00
    box_b_credit = 100.00
    box_b_flood = 30.00
    box_b_ufmip = ufmip
    box_b_total = box_b_appraisal + box_b_credit + box_b_flood + box_b_ufmip

    box_c_title = 500.00
    box_c_lender_title = final_loan * 0.0055
    box_c_survey = 300.00
    box_c_total = box_c_title + box_c_lender_title + box_c_survey

    box_e_recording = 299.00
    box_e_transfer = final_loan * 0.0055
    box_e_total = box_e_recording + box_e_transfer

    box_f_insurance_prepay = data.monthly_insurance * 12
    box_f_interest = interim_interest
    box_f_total = box_f_insurance_prepay + box_f_interest

    box_g_taxes_escrow = data.monthly_taxes * 3
    box_g_insurance_escrow = data.monthly_insurance * 3
    box_g_total = box_g_taxes_escrow + box_g_insurance_escrow

    total_closing = box_a + box_b_total + box_c_total + box_e_total + box_f_total + box_g_total
    cash_to_close = dp_amount + total_closing

    def fmt(val):
        return f"${val:,.2f}"

    # Build markdown response
    return {
        "quote": f"""🧾 **FHA Loan Estimate — govies.com**

your actual rate, payment, and costs could be higher. get an official loan estimate before choosing a loan.

🏠 **purchase price:** {fmt(data.purchase_price)}
💵 **final loan amount (incl. ufmip):** {fmt(final_loan)}
📉 **interest rate:** {rate * 100:.3f}%
📊 **apr:** {apr_rate * 100:.3f}%
📆 **monthly payment (pitia):** {fmt(total_monthly)}
💰 **estimated cash to close:** {fmt(cash_to_close)}

📦 **itemized closing costs**

**box a – origination charges** (subtotal: {fmt(box_a)}):
- lender origination fee: {fmt(0)}

**box b – cannot shop for** (subtotal: {fmt(box_b_total)}):
- appraisal: {fmt(box_b_appraisal)}
- credit report: {fmt(box_b_credit)}
- flood certification: {fmt(box_b_flood)}
- ufmip: {fmt(box_b_ufmip)}

**box c – can shop for** (subtotal: {fmt(box_c_total)}):
- title settlement: {fmt(box_c_title)}
- lender’s title insurance (0.55%): {fmt(box_c_lender_title)}
- survey: {fmt(box_c_survey)}

**box e – government fees** (subtotal: {fmt(box_e_total)}):
- recording: {fmt(box_e_recording)}
- transfer tax (matches lender’s title): {fmt(box_e_transfer)}

**box f – prepaids** (subtotal: {fmt(box_f_total)}):
- insurance (12 months): {fmt(box_f_insurance_prepay)}
- interim interest (15 days): {fmt(box_f_interest)}

**box g – initial escrow** (subtotal: {fmt(box_g_total)}):
- taxes × 3: {fmt(box_g_taxes_escrow)}
- insurance × 3: {fmt(box_g_insurance_escrow)}

**total closing costs:** {fmt(total_closing)}

💵 **calculating cash to close**
- down payment: {fmt(dp_amount)}
- closing costs: {fmt(total_closing)}
- estimated cash to close: {fmt(cash_to_close)}

please review this estimate and consult with us if you'd like to move forward.

🔗 https://govies.com/apply  
📅 https://govies.com/consult  
📞 1-800-YES-GOVIES  
✉️ team@govies.com
"""
    }
