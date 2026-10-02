"""Script to generate synthetic corpus for the demo:
- 40 bank policy documents (hardship, card disputes, fees, fraud)
- 200 customer dispute letters
"""

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
POLICIES_DIR = DATA_DIR / "policies"
LETTERS_DIR = DATA_DIR / "letters"


def generate_corpus():
    POLICIES_DIR.mkdir(parents=True, exist_ok=True)
    LETTERS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Generate 40 Policies
    categories = {
        "hardship": [
            ("MORTGAGE_RELIEF", "Mortgage Hardship Assistance and Payment Deferrals"),
            ("UNEMPLOYMENT_SUPPORT", "Short-term Income Disruption and Repayment Holiday Policy"),
            ("MEDICAL_HARDSHIP", "Severe Medical Illness and Compassionate Relief Guidelines"),
            ("NATURAL_DISASTER_ASSIST", "Emergency Disaster Assistance and Fee Waivers"),
            ("DEBT_CONSOLIDATION_HARDSHIP", "Hardship Debt Restructure and Loan Consolidation Rules"),
            ("INTEREST_FREE_PERIOD", "Temporary Interest Capitalisation and Relief Policy"),
            ("SMALL_BUSINESS_HARDSHIP", "Micro-Business Emergency Cash Flow Relief and Forbearance"),
            ("VULNERABLE_CUSTOMER_PROTECTION", "Customer Vulnerability and Financial Wellbeing Framework"),
            ("DEFAULT_NOTICE_PAUSE", "Moratorium on Enforcement Actions During Assessment"),
            ("RECOVERY_ESCROW_RELEASE", "Compassionate Escrow Release for Distressed Mortgagors"),
        ],
        "card_disputes": [
            ("UNAUTHORISED_TXN", "Unauthorised Electronic and Card Transactions Protocol"),
            ("MERCHANT_NOT_RECEIVED", "Goods or Services Not Received Dispute Resolution"),
            ("DEFECTIVE_GOODS", "Defective Goods, Counterfeit Items, and Chargeback Evidence"),
            ("RECURRING_BILLING_CANCELLED", "Subscription and Recurring Billing Cancellation Policy"),
            ("DUPLICATE_PROCESSING", "Duplicate Processing and Incorrect Chargeback Management"),
            ("CURRENCY_CONVERSION_ERRORS", "Foreign Exchange Fee and Dynamic Currency Conversion Disputes"),
            ("ATM_DISCREPANCY", "ATM Cash Dispensation Error and Shortfall Handling"),
            ("TRAVEL_CANCELLATION_REFUND", "Airline, Hotel, and Booking Cancellation Chargeback Framework"),
            ("FRAUD_CHIP_PIN_LIABILITY", "Cardholder Liability in Chip-and-PIN and Contactless Transactions"),
            ("MERCHANT_SURCHARGE_DISPUTES", "Credit and Debit Card Excessive Surcharging Enforcement"),
        ],
        "fees": [
            ("OVERDRAFT_FEES_POLICY", "Unarranged Overdraft and Incurred Fee Calculation Rules"),
            ("ACCOUNT_KEEPING_WAIVERS", "Monthly Account Keeping Fee Exemption Guidelines"),
            ("LATE_PAYMENT_GRACE", "Credit Card Late Payment Fee Grace Periods and Assessment"),
            ("INTERNATIONAL_TRANSFER_TARIFF", "Wire Transfer and Cross-Border Intermediary Fee Policy"),
            ("DORMANT_ACCOUNT_RECOVERY", "Dormant Account Inactivity Fees and Funds Reclamation"),
            ("EARLY_TERMINATION_PENALTY", "Fixed Term Deposit Early Exit Fee Break Costs"),
            ("PAPER_STATEMENT_LEVY", "Paper Statement Fees and Concession Exemptions"),
            ("REPLACEMENT_CARD_TARIFF", "Emergency Replacement Card Courier and Issuance Fees"),
            ("HIGH_VOLUME_TRANSACTION_FEES", "Commercial Tiered Transaction Limit and Excess Charges"),
            ("DIRECT_DEBIT_DISHONOUR", "Direct Debit Insufficient Funds Dishonour Handling"),
        ],
        "fraud": [
            ("ACCOUNT_TAKEOVER_RESPONSE", "Account Takeover Detection and Immediate Mitigation Policy"),
            ("IDENTITY_THEFT_INVESTIGATION", "Identity Theft Reporting and Account Restoration Protocol"),
            ("AUTHORIZED_PUSH_PAYMENT", "Authorised Push Payment (APP) Scam Reimbursement Code"),
            ("SIM_SWAP_PROTECTION", "SIM-Swap Fraud and Step-Up Authentication Controls"),
            ("PHISHING_COMPROMISE_REMEDY", "Credential Harvesting and Remote Access Scam Remediation"),
            ("CARD_SKIMMING_PROTOCOLS", "Physical ATM / POS Skimming Detection and Remediation"),
            ("MULE_ACCOUNT_INTERDICTION", "Money Mule and Layering Ring Account Freeze Guidelines"),
            ("HIGH_RISK_CRYPTO_BLOCK", "Prohibited and High-Risk Cryptocurrency Exchange Outflows"),
            ("SENIOR_FINANCIAL_EXPLOITATION", "Elder Abuse and Coerced Transaction Intervention"),
            ("INTERNAL_FRAUD_WHISTLEBLOWING", "Staff Malfeasance and Whistleblower Protection Mandate"),
        ]
    }

    policy_count = 0
    all_policies_index = []

    for category, items in categories.items():
        for code, title in items:
            policy_count += 1
            filename = f"policy_{policy_count:02d}_{code.lower()}.txt"
            content = f"""POLICY REFERENCE: POL-{policy_count:03d}
TITLE: {title}
CATEGORY: {category.upper()}
EFFECTIVE DATE: 2026-01-01
STATUS: ACTIVE
AUTHORITY: Retail Banking Operations & Regulatory Compliance Board

1. EXECUTIVE SUMMARY
This policy document establishes standard operating procedures for {title.lower()} within the Retail Banking and Operations divisions. It defines eligibility, assessment thresholds, statutory response timeframes, and customer relief entitlements in compliance with federal banking codes.

2. SCOPE AND ELIGIBILITY
This framework applies to all individual personal accounts, small business banking relationships, and associated card/credit products. 
- Primary qualification criteria require a formal application or flagged incident registered within 60 calendar days of occurrence.
- Threshold limits: Up to $25,000 for standard branch/tier-1 determinations; incidents exceeding $25,000 require Tier-2 Risk Committee sign-off.
- Customers must provide standard documentation (e.g., bank statements, police reports, proof of cancellation, medical certificates) within 14 business days.

3. DETAILED PROVISIONS & OPERATIONAL GUIDELINES
3.1. Immediate Actions:
Upon receipt of a claim or hardship request under {code}, the case officer must:
(a) Pause automated collections and late fees for a mandatory 30-day moratorium.
(b) Review account history over the prior 12-month period for prior concessions or suspicious activity.
(c) Issue a formal acknowledgement notice with unique reference code POL-{policy_count:03d}-CASE within 48 hours.

3.2. Evaluation Standards:
- Relief Options: Repayment restructuring, term extension up to 24 months, complete fee waiver, or temporary zero-interest hold.
- Fraud and Dispute Liability: Customers are fully reimbursed where reasonable care was taken, no PIN disclosure occurred, and notification was provided within 48 hours of discovery.
- Dispute Timeframes: Standard card dispute investigations conclude within 21 business days; complex cross-border chargebacks within 45 calendar days.

4. ESCALATIONS AND APPEALS
If the customer disputes the outcome or requires compassionate grounds consideration:
- The case is referred to the Internal Dispute Resolution (IDR) team.
- IDR final determination must be communicated in writing within 15 calendar days.
- External Ombudsman escalation pathways (e.g., AFCA / CFPB) must be clearly provided in all rejection correspondence.

5. RECORD KEEPING AND AUDIT
All records, voice recordings, signed declarations, and financial evidence must be retained in the central immutable document repository for 7 years.
"""
            filepath = POLICIES_DIR / filename
            filepath.write_text(content.strip(), encoding="utf-8")
            all_policies_index.append({
                "id": f"POL-{policy_count:03d}",
                "code": code,
                "title": title,
                "category": category,
                "filename": filename
            })

    (DATA_DIR / "policies_index.json").write_text(json.dumps(all_policies_index, indent=2), encoding="utf-8")

    # 2. Generate 200 Dispute Letters
    merchants = [
        "TechGalaxy Online", "FlyHigh Airways", "Grand Imperial Hotel", "Global Stream Plus",
        "Apex Gym & Fitness", "Urban Supermarket", "Metro Ride Taxi", "QuickDine Delivery",
        "ElectroMart Supplies", "Fashion Boutique Direct", "AutoCare Fuel Station", "CloudStorage Solutions"
    ]
    reasons = [
        "unauthorised transaction", "goods never delivered", "charged twice for single order",
        "subscription cancelled but billing continued", "defective item seller refused refund",
        "charged incorrect foreign currency rate", "ATM cash failed to dispense full amount"
    ]
    amounts = ["$49.99", "$120.00", "$349.50", "$890.00", "$1,450.00", "$2,100.00", "$75.25", "$620.15", "$15.99", "$2,890.00"]
    dates = ["2026-08-12", "2026-08-25", "2026-09-02", "2026-09-15", "2026-09-21", "2026-09-28", "2026-09-30"]

    all_letters_index = []
    for i in range(1, 201):
        merchant = merchants[i % len(merchants)]
        reason = reasons[i % len(reasons)]
        amount = amounts[i % len(amounts)]
        date = dates[i % len(dates)]
        letter_id = f"LET-{i:04d}"
        filename = f"letter_{i:04d}.txt"

        letter_content = f"""Date: {date}
To: Dispute Resolution & Card Operations Department
From: Customer Account ID: ACC-9821{i:03d}
Subject: Formal Dispute regarding {reason.upper()} - Ref #{letter_id}

Dear Customer Service Team,

I am writing to formally dispute a charge appearing on my latest credit/debit card account statement. I have been a loyal customer of your bank for over five years, but recent handling of this matter has caused significant distress.

Incident Details:
- Transaction Date: {date}
- Merchant Name: {merchant}
- Disputed Amount: {amount}
- Card Ending In: {1000 + i % 9000}

Description of Issue:
On the date mentioned above, a transaction in the amount of {amount} was posted to my account from {merchant}. The issue is that this represents an {reason}. 

I immediately contacted the merchant directly via their customer support hotline and email on {date} to request an immediate correction and refund. However, they either refused to cooperate, claimed it was a processing error on the bank's end, or failed to respond after multiple follow-up inquiries. I have kept copies of all email correspondence, transaction receipts, and chat logs as proof.

Because the merchant has not resolved this issue, I am exercising my rights under the banking code of practice and cardholder agreement. I request that your team initiate a formal chargeback / dispute investigation immediately, issue a temporary credit for {amount} to my account, and ensure no interest or late fees accrue as a result of this incorrect charge.

Please confirm receipt of this dispute letter and provide a case tracking number at your earliest convenience.

Sincerely,
Customer Account ACC-9821{i:03d}
Contact Phone: +1 (555) 019-{i:04d}
"""
        filepath = LETTERS_DIR / filename
        filepath.write_text(letter_content.strip(), encoding="utf-8")
        all_letters_index.append({
            "id": letter_id,
            "filename": filename,
            "merchant": merchant,
            "amount": amount,
            "date": date,
            "reason": reason
        })

    (DATA_DIR / "letters_index.json").write_text(json.dumps(all_letters_index, indent=2), encoding="utf-8")
    print(f"Generated {policy_count} policies and {len(all_letters_index)} dispute letters successfully.")


if __name__ == "__main__":
    generate_corpus()
