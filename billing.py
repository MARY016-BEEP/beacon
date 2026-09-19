"""
billing.py
----------
Payment must be completed BEFORE an order moves to 'Assigned' for
delivery (enforced in app.py's route logic, not just here).

Three payment methods:
  1. SHA (Social Health Authority) - logged as a claim reference;
     actual SHA claims integration happens through SHA's own
     provider portal/API, which requires facility accreditation
     credentials. This function records the claim attempt so
     billing status can be tracked; plug in the real SHA API call
     once your facility's SHA integration credentials are issued.
  2. M-Pesa STK Push (Safaricom Daraja API).
  3. Mastercard, via a card payment gateway (Flutterwave / Pesapal /
     DPO Pay all support Mastercard - a hospital cannot integrate
     directly with Mastercard itself without a licensed processor).

SIMULATE = True by default so you can test the full billing ->
delivery flow before plugging in real credentials.
"""

import os
import base64
import requests
from datetime import datetime
from database import get_connection

MPESA_CONFIG = {
    "CONSUMER_KEY": os.environ.get("MPESA_CONSUMER_KEY", "YOUR_CONSUMER_KEY"),
    "CONSUMER_SECRET": os.environ.get("MPESA_CONSUMER_SECRET", "YOUR_CONSUMER_SECRET"),
    "SHORTCODE": os.environ.get("MPESA_SHORTCODE", "174379"),
    "PASSKEY": os.environ.get("MPESA_PASSKEY", "YOUR_LIPA_NA_MPESA_PASSKEY"),
    "CALLBACK_URL": os.environ.get("MPESA_CALLBACK_URL", "https://yourdomain.com/mpesa/callback"),
    "ENV": os.environ.get("MPESA_ENV", "sandbox"),
}

CARD_GATEWAY_CONFIG = {
    "PROVIDER": "flutterwave",
    "SECRET_KEY": os.environ.get("CARD_GATEWAY_SECRET_KEY", "YOUR_GATEWAY_SECRET_KEY"),
    "BASE_URL": os.environ.get("CARD_GATEWAY_BASE_URL", "https://api.flutterwave.com/v3"),
}

SHA_CONFIG = {
    "PROVIDER_CODE": os.environ.get("SHA_PROVIDER_CODE", "YOUR_SHA_FACILITY_CODE"),
    "API_KEY": os.environ.get("SHA_API_KEY", "YOUR_SHA_API_KEY"),
    "BASE_URL": os.environ.get("SHA_BASE_URL", "https://api.sha.go.ke"),  # placeholder - confirm real endpoint with SHA
}

SIMULATE = True


def _mpesa_base_url():
    return "https://sandbox.safaricom.co.ke" if MPESA_CONFIG["ENV"] == "sandbox" else "https://api.safaricom.co.ke"


def get_mpesa_access_token():
    url = f"{_mpesa_base_url()}/oauth/v1/generate?grant_type=client_credentials"
    resp = requests.get(url, auth=(MPESA_CONFIG["CONSUMER_KEY"], MPESA_CONFIG["CONSUMER_SECRET"]))
    resp.raise_for_status()
    return resp.json()["access_token"]


def _log_bill_attempt(order_id, patient_id, amount, method, transaction_ref, status="Pending"):
    conn = get_connection()
    cur = conn.cursor()
    existing = cur.execute("SELECT id FROM bills WHERE order_id = ?", (order_id,)).fetchone()
    if existing:
        cur.execute("""
            UPDATE bills SET amount=?, payment_method=?, transaction_ref=?, status=? WHERE id=?
        """, (amount, method, transaction_ref, status, existing["id"]))
        bill_id = existing["id"]
    else:
        cur.execute("""
            INSERT INTO bills (order_id, patient_id, amount, payment_method, transaction_ref, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (order_id, patient_id, amount, method, transaction_ref, status))
        bill_id = cur.lastrowid
    conn.commit()
    conn.close()
    return bill_id


def initiate_mpesa_stk_push(order_id, patient_id, amount, phone_number):
    if SIMULATE:
        ref = f"SIM-MPESA-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        bill_id = _log_bill_attempt(order_id, patient_id, amount, "mpesa", ref, "Pending")
        return {"simulated": True, "bill_id": bill_id, "transaction_ref": ref,
                "message": "STK push simulated - fill in real Daraja credentials and set SIMULATE=False to go live."}

    token = get_mpesa_access_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(
        (MPESA_CONFIG["SHORTCODE"] + MPESA_CONFIG["PASSKEY"] + timestamp).encode()
    ).decode()
    payload = {
        "BusinessShortCode": MPESA_CONFIG["SHORTCODE"], "Password": password, "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline", "Amount": int(amount),
        "PartyA": phone_number, "PartyB": MPESA_CONFIG["SHORTCODE"], "PhoneNumber": phone_number,
        "CallBackURL": MPESA_CONFIG["CALLBACK_URL"],
        "AccountReference": f"BeaconOfHope-Order{order_id}", "TransactionDesc": "Chronic disease medication delivery",
    }
    resp = requests.post(f"{_mpesa_base_url()}/mpesa/stkpush/v1/processrequest",
                          json=payload, headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    ref = data.get("CheckoutRequestID", "UNKNOWN")
    _log_bill_attempt(order_id, patient_id, amount, "mpesa", ref, "Pending")
    return {"simulated": False, "raw_response": data, "transaction_ref": ref}


def initiate_card_payment(order_id, patient_id, amount, card_last4, email):
    if SIMULATE:
        ref = f"SIM-CARD-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        bill_id = _log_bill_attempt(order_id, patient_id, amount, "mastercard", ref, "Pending")
        return {"simulated": True, "bill_id": bill_id, "transaction_ref": ref,
                "message": "Card payment simulated - plug in a real gateway (Flutterwave/Pesapal/DPO) and set SIMULATE=False to go live."}

    headers = {"Authorization": f"Bearer {CARD_GATEWAY_CONFIG['SECRET_KEY']}"}
    tx_ref = f"BOH-{order_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    payload = {"tx_ref": tx_ref, "amount": amount, "currency": "KES",
               "redirect_url": "https://yourdomain.com/payment/callback",
               "customer": {"email": email}, "payment_options": "card"}
    resp = requests.post(f"{CARD_GATEWAY_CONFIG['BASE_URL']}/payments", json=payload, headers=headers)
    data = resp.json()
    _log_bill_attempt(order_id, patient_id, amount, "mastercard", tx_ref, "Pending")
    return {"simulated": False, "raw_response": data, "transaction_ref": tx_ref}


def initiate_sha_claim(order_id, patient_id, amount, sha_member_number):
    """
    Logs an SHA claim attempt. Replace the SIMULATE branch with a real
    call to SHA's provider API once your facility has SHA accreditation
    and API credentials - the exact endpoint/payload shape should be
    confirmed directly with SHA as their provider API details are not
    public in the way M-Pesa's Daraja docs are.
    """
    if SIMULATE:
        ref = f"SIM-SHA-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        bill_id = _log_bill_attempt(order_id, patient_id, amount, "sha", ref, "Pending")
        return {"simulated": True, "bill_id": bill_id, "transaction_ref": ref,
                "message": "SHA claim simulated - integrate your facility's real SHA provider API credentials to go live."}

    # Placeholder for the real SHA claims API call once credentials are available.
    ref = f"SHA-{order_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    _log_bill_attempt(order_id, patient_id, amount, "sha", ref, "Pending")
    return {"simulated": False, "transaction_ref": ref, "message": "SHA claim submitted (placeholder)."}


def mark_bill_paid(order_id):
    conn = get_connection()
    conn.execute("UPDATE bills SET status='Paid', paid_on=? WHERE order_id=?",
                 (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_id))
    conn.execute("UPDATE orders SET status='Paid' WHERE id=? AND status='Billed'", (order_id,))
    conn.commit()
    conn.close()


def get_bill_for_order(order_id):
    conn = get_connection()
    bill = conn.execute("SELECT * FROM bills WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()
    return dict(bill) if bill else None
