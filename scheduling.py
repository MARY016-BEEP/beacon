"""
scheduling.py
-------------
Two responsibilities:

1. COMORBIDITY CONSOLIDATION - same idea as before: each active
   disease has its own "next due" date (last pickup + refill
   interval). If a patient has more than one, we pull due dates
   that fall within GRACE_WINDOW_DAYS of the earliest one onto a
   single visit, so a patient with e.g. HIV + TB collects both in
   one trip instead of two.

2. DELIVERY DAY RESTRICTION - Beacon of Hope only delivers on
   Monday, Wednesday and Saturday. Any computed date that doesn't
   fall on one of those days is rolled FORWARD to the next one that
   does.

create_or_update_pending_order() is called automatically whenever a
clinician issues/updates a prescription - it either creates a new
pharmacy order for the patient's next consolidated, day-restricted
delivery date, or updates the existing not-yet-delivered order if
one is already pending (so we don't spawn duplicate orders).
"""

from datetime import datetime, timedelta
from database import get_connection

DATE_FMT = "%Y-%m-%d"
GRACE_WINDOW_DAYS = 14

# Monday=0 ... Sunday=6  ->  allowed delivery weekdays: Mon(0), Wed(2), Sat(5)
ALLOWED_DELIVERY_WEEKDAYS = {0, 2, 5}


def _parse(d):
    return datetime.strptime(d, DATE_FMT)


def _fmt(dt):
    return dt.strftime(DATE_FMT)


def next_allowed_delivery_date(target_date: datetime) -> datetime:
    """Roll target_date forward (never backward) to the next Mon/Wed/Sat."""
    d = target_date
    for _ in range(8):  # at most a week of searching
        if d.weekday() in ALLOWED_DELIVERY_WEEKDAYS:
            return d
        d += timedelta(days=1)
    return d  # fallback, should never hit


def get_active_diseases_for_patient(patient_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT pd.id as pd_id, d.name as disease_name,
               pd.diagnosis_date, pd.last_pickup_date,
               COALESCE(pd.refill_interval_days, d.default_refill_days) as interval_days
        FROM patient_diseases pd
        JOIN diseases d ON d.id = pd.disease_id
        WHERE pd.patient_id = ? AND pd.active = 1
    """, (patient_id,)).fetchall()
    conn.close()

    result = []
    for r in rows:
        base_date = r["last_pickup_date"] or r["diagnosis_date"]
        due_date = _parse(base_date) + timedelta(days=r["interval_days"])
        result.append({
            "pd_id": r["pd_id"],
            "disease_name": r["disease_name"],
            "due_date": due_date,
        })
    return result


def compute_consolidated_date(patient_id):
    """Returns (final_date_str, diseases_covered_list) already rolled
    forward onto an allowed delivery weekday."""
    diseases = get_active_diseases_for_patient(patient_id)
    if not diseases:
        return None, []

    if len(diseases) == 1:
        d = diseases[0]
        final = next_allowed_delivery_date(d["due_date"])
        return _fmt(final), [d["disease_name"]]

    diseases.sort(key=lambda x: x["due_date"])
    anchor = diseases[0]["due_date"]

    covered = []
    for d in diseases:
        gap = (d["due_date"] - anchor).days
        if gap <= GRACE_WINDOW_DAYS:
            covered.append(d["disease_name"])

    final = next_allowed_delivery_date(anchor)
    return _fmt(final), covered


def create_or_update_pending_order(patient_id):
    """
    Called after a prescription is created/updated. Creates a new
    'Received' pharmacy order at the next consolidated, day-restricted
    delivery date - or updates the existing pending order for this
    patient if one hasn't been delivered/cancelled yet.
    """
    final_date, covered = compute_consolidated_date(patient_id)
    if not final_date:
        return None

    conn = get_connection()
    cur = conn.cursor()

    existing = cur.execute("""
        SELECT id FROM orders
        WHERE patient_id = ? AND status NOT IN ('Delivered','Cancelled')
        ORDER BY id DESC LIMIT 1
    """, (patient_id,)).fetchone()

    diseases_str = ", ".join(covered)

    if existing:
        cur.execute("""
            UPDATE orders SET delivery_date = ?, diseases_covered = ? WHERE id = ?
        """, (final_date, diseases_str, existing["id"]))
        order_id = existing["id"]
    else:
        cur.execute("""
            INSERT INTO orders (patient_id, diseases_covered, delivery_date, status)
            VALUES (?, ?, ?, 'Received')
        """, (patient_id, diseases_str, final_date))
        order_id = cur.lastrowid

    conn.commit()
    conn.close()
    return order_id


def mark_order_delivered_and_reschedule(order_id):
    """
    Called when a rider confirms delivery. Updates last_pickup_date
    for every disease covered in the order, then generates the
    patient's NEXT consolidated order automatically.
    """
    conn = get_connection()
    cur = conn.cursor()
    order = cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return False

    today = _fmt(datetime.now())
    covered_names = [n.strip() for n in (order["diseases_covered"] or "").split(",") if n.strip()]
    for name in covered_names:
        cur.execute("""
            UPDATE patient_diseases
            SET last_pickup_date = ?
            WHERE patient_id = ? AND disease_id = (SELECT id FROM diseases WHERE name = ?)
        """, (today, order["patient_id"], name))

    conn.commit()
    conn.close()

    create_or_update_pending_order(order["patient_id"])
    return True
