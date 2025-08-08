from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- HELPER: Format currency with commas ---
def fmt_currency(val: Decimal) -> str:
    return f"${val:,.2f}"

# --- FHA QUOTE ENDPOINT ---
@app.get("/fha-quote")
def fha_quote(
    purchase_price: Decimal = Query(..., description="Purchase price of the home"),
    down_payment_value: Decimal = Query(..., description="Down payment value (percent or dollar)"),
    down_payment_is_percent: bool = Query(..., description="True if down_payment_value is a %"),
    fico: int = Query(..., description="Credit score"),
    monthly_taxes: Decimal = Query(..., description="Monthly property taxes"),
    monthly_insurance: Decimal = Query(..., description="Monthly homeowner's insurance"),
    hoa: Decimal = Query(Decimal("0.00"), description="Monthly HOA fee"),
    property_type: str = Query(..., description="Property type: SFR or 1–4 unit"),
    lender_credit_percent: Optional[Decimal] = Query(Decimal("0.00"), description="Lender credit % of loan amount"),
):
    # --- GUARDRAILS ---
    if fico < 640:
        return {"error": "minimum credit score for FHA is 640."}

    # Down payment calc
    if down_payment_is_percent:
        down_payment_amount = (purchase_price * (down_payment_value / Decimal("100"))).quantize(Decimal("0.01"))
    else:
        down_payment_amount = down_payment_value

    base_loan = purchase_price - down_payment_amount

    if base_loan < 150000:
        return {"error": "minimum base loan amount for FHA is $150,000."}

    if property_type.lower() not in ["sfr", "single-family", "single family", "1-4 unit", "1–4 unit"]:
        return {"error": "property must be single-family or 1–4 unit. condos & manufactured homes not allowed."}

    # --- Constants ---
    rate = Decimal("6.125")  # Note rate
    ufmip = (base_loan * Decimal("0.0175")).quantize(Decimal("0.01"))
    final_loan = base_loan + ufmip

    # Monthly P&I
    monthly_pi = (final_loan * (rate / Decimal("100")) / Decimal("12")).quantize(Decimal("0.01"))

    # Monthly MIP
    mip = (final_loan * Decimal("0.0055") / Decimal("12")).quantize(Decimal("0.01"))

    # Monthly total payment (PITIA)
    pitia = monthly_pi + mip + monthly_taxes + monthly_insurance + hoa

    # --- Interim interest (15 days) ---
    daily_interest = (final_loan * (rate / Decimal("100")) / Decimal("365")).quantize(Decimal("0.01"))
    interim_interest = (daily_interest * Decimal("15")).quantize(Decimal("0.01"))

    # --- Closing costs breakdown ---
    box_a = Decimal("0.00")

    box_b_items = {
        "appraisal": Decimal("650.00"),
        "credit report": Decimal("100.00"),
        "flood certification": Decimal("30.00"),
        "ufmip": ufmip
    }
    box_b_total = sum(box_b_items.values())

    box_c_items = {
        "title settlement": Decimal("500.00"),
        "lender’s title insurance (0.55%)": (final_loan * Decimal("0.0055")).quantize(Decimal("0.01")),
        "survey": Decimal("300.00")
    }
    box_c_total = sum(box_c_items.values())

    box_e_items = {
        "recording": Decimal("299.00"),
        "transfer tax (matches lender’s title)": box_c_items["lender’s title insurance (0.55%)"]
    }
    box_e_total = sum(box_e_items.values())

    box_f_items = {
        "insurance (12 months)": monthly_insurance * Decimal("12"),
        "interim interest (15 days)": interim_interest
    }
    box_f_total = sum(box_f_items.values())

    box_g_items = {
        "taxes × 3": monthly_taxes * Decimal("3"),
        "insurance × 3": monthly_insurance * Decimal("3")
    }
    box_g_total = sum(box_g_items.values())

    total_closing_costs = box_a + box_b_total + box_c_total + box_e_total + box_f_total + box_g_total

    # Lender credit (if any)
    lender_credit_amount = (final_loan * (lender_credit_percent / Decimal("100"))).quantize(Decimal("0.01"))
    total_closing_after_credit = total_closing_costs - lender_credit_amount

    # Cash to close
    cash_to_close = down_payment_amount + total_closing_after_credit

    # --- APR calculation ---
    apr_est = (rate + Decimal("1.06")).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

    # --- Build markdown output ---
    output_md = f"""
🧾 **FHA Loan Estimate — govies.com**

your actual rate, payment, and costs could be higher. get an official loan estimate before choosing a loan.

🏠 **purchase price:** {fmt_currency(purchase_price)}
💵 **final loan amount (incl. ufmip):** {fmt_currency(final_loan)}
📉 **interest rate:** {rate}%
📈 **est. apr (incl. mip & costs):** {apr_est}%
📆 **monthly payment (pitia):** {fmt_currency(pitia)}
💰 **estimated cash to close:** {fmt_currency(cash_to_close)}

📦 **itemized closing costs**
**box a – origination charges (subtotal: {fmt_currency(box_a)}):**
""" + "\n".join([f"- {k}: {fmt_currency(v)}" for k, v in {}.items()]) + f"""

**box b – cannot shop for (subtotal: {fmt_currency(box_b_total)}):**
""" + "\n".join([f"- {k}: {fmt_currency(v)}" for k, v in box_b_items.items()]) + f"""

**box c – can shop for (subtotal: {fmt_currency(box_c_total)}):**
""" + "\n".join([f"- {k}: {fmt_currency(v)}" for k, v in box_c_items.items()]) + f"""

**box e – government fees (subtotal: {fmt_currency(box_e_total)}):**
""" + "\n".join([f"- {k}: {fmt_currency(v)}" for k, v in box_e_items.items()]) + f"""

**box f – prepaids (subtotal: {fmt_currency(box_f_total)}):**
""" + "\n".join([f"- {k}: {fmt_currency(v)}" for k, v in box_f_items.items()]) + f"""

**box g – initial escrow (subtotal: {fmt_currency(box_g_total)}):**
""" + "\n".join([f"- {k}: {fmt_currency(v)}" for k, v in box_g_items.items()]) + f"""

**total closing costs:** {fmt_currency(total_closing_costs)}

💵 **calculating cash to close**
- down payment: {fmt_currency(down_payment_amount)}
- closing costs: {fmt_currency(total_closing_after_credit)}
- estimated cash to close: {fmt_currency(cash_to_close)}
"""

    return {"output_markdown": output_md}
