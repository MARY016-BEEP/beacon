"""
database.py
-----------
Beacon of Hope - full platform schema (v2).

Run once: python database.py
Creates beacon.db with every table needed for:
  - Patient self-signup/login + all staff roles (clinician, pharmacist,
    nurse, lab, rider, admin)
  - Telemedicine appointments (clinical, mental health, follow-up)
  - Prescriptions (clinician-owned)
  - Pharmacy orders / inventory / billing
  - Delivery (rider workflow, Mon/Wed/Sat only)
  - Lab requests (incl. home sample collection)
  - Nursing home-visit requests
  - Packages (pharma + service bundles)
  - FAQ / patient education content
Also seeds: the 4 chronic diseases, a default admin account, starter
packages, and a starter FAQ set.
"""

import sqlite3
import os
from werkzeug.security import generate_password_hash

DB_NAME = os.path.join(os.path.dirname(__file__), "beacon.db")


def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    # ============================================================
    # USERS - every human in the system (patients + all staff roles)
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN
            ('patient','clinician','pharmacist','nurse','lab','rider','admin')),
        full_name TEXT NOT NULL,
        phone TEXT,
        area TEXT,                      -- used for rider zone-matching & delivery routing
        specialty TEXT,                 -- for clinicians: 'Hypertension','Diabetes Mellitus','HIV','TB','Mental Health','General'
        bio TEXT,                       -- short clinician bio shown to patients when choosing a doctor
        created_on TEXT DEFAULT (datetime('now'))
    )
    """)

    # Patient-only extra profile fields (kept separate so the users
    # table stays generic across all roles)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS patient_profiles (
        user_id INTEGER PRIMARY KEY,
        national_id TEXT,
        date_of_birth TEXT,
        gender TEXT,
        address TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # ============================================================
    # Chronic disease reference + per-patient diagnoses
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS diseases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        default_refill_days INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS patient_diseases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        disease_id INTEGER NOT NULL,
        diagnosis_date TEXT NOT NULL,
        last_pickup_date TEXT,
        refill_interval_days INTEGER,
        active INTEGER DEFAULT 1,
        FOREIGN KEY (patient_id) REFERENCES users(id),
        FOREIGN KEY (disease_id) REFERENCES diseases(id),
        UNIQUE(patient_id, disease_id)
    )
    """)

    # ============================================================
    # TELEMEDICINE: doctor-set available time slots
    # A clinician opens slots; a patient books a specific open slot
    # with that specific doctor. This is what makes booking "with a
    # specific doctor" actually work, rather than just a date request.
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS availability_slots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clinician_id INTEGER NOT NULL,
        slot_date TEXT NOT NULL,
        slot_time TEXT NOT NULL,       -- e.g. '09:00'
        is_booked INTEGER DEFAULT 0,
        FOREIGN KEY (clinician_id) REFERENCES users(id),
        UNIQUE(clinician_id, slot_date, slot_time)
    )
    """)

    # ============================================================
    # TELEMEDICINE: appointments (clinical / mental health / follow-up)
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS appointments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        clinician_id INTEGER,                 -- the specific doctor patient chose (or NULL until Beacon assigns one)
        slot_id INTEGER,                      -- the availability_slots row this appointment used, if any
        appointment_type TEXT NOT NULL CHECK(appointment_type IN
            ('Clinical','MentalHealth','FollowUp')),
        mode TEXT DEFAULT 'Virtual' CHECK(mode IN ('Virtual','InPerson')),
        requested_date TEXT NOT NULL,
        reason TEXT,                          -- short patient-supplied reason for the visit
        booked_by TEXT DEFAULT 'patient' CHECK(booked_by IN ('patient','admin_on_behalf')),
        status TEXT DEFAULT 'Requested' CHECK(status IN
            ('Requested','Confirmed','Completed','Cancelled')),
        consult_link TEXT,                    -- plug in Zoom/Jitsi/Twilio room link here
        created_on TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (patient_id) REFERENCES users(id),
        FOREIGN KEY (clinician_id) REFERENCES users(id),
        FOREIGN KEY (slot_id) REFERENCES availability_slots(id)
    )
    """)

    # ============================================================
    # PRESCRIPTIONS - clinician-owned entirely
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS prescriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        clinician_id INTEGER NOT NULL,
        disease_id INTEGER,
        medication_name TEXT NOT NULL,
        dosage TEXT NOT NULL,
        instructions TEXT,
        date_issued TEXT DEFAULT (date('now')),
        status TEXT DEFAULT 'Active' CHECK(status IN ('Active','Adjusted','Stopped')),
        FOREIGN KEY (patient_id) REFERENCES users(id),
        FOREIGN KEY (clinician_id) REFERENCES users(id),
        FOREIGN KEY (disease_id) REFERENCES diseases(id)
    )
    """)

    # ============================================================
    # INVENTORY - medications stock & pricing (pharmacist/admin)
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS medications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        unit_price REAL NOT NULL,
        stock_qty INTEGER NOT NULL DEFAULT 0,
        low_stock_threshold INTEGER DEFAULT 10,
        expiry_date TEXT
    )
    """)

    # ============================================================
    # PHARMACY ORDERS - one order = one consolidated delivery trip
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        diseases_covered TEXT,             -- comma separated, for comorbidity visibility
        status TEXT DEFAULT 'Received' CHECK(status IN
            ('Received','Verifying','Available','Dispensed','Billed','Paid',
             'Assigned','Collected','Delivered','Cancelled')),
        delivery_date TEXT NOT NULL,       -- always forced to Mon/Wed/Sat
        created_on TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (patient_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        prescription_id INTEGER,
        medication_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL,
        unit_price REAL NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (prescription_id) REFERENCES prescriptions(id),
        FOREIGN KEY (medication_id) REFERENCES medications(id)
    )
    """)

    # ============================================================
    # BILLING - patient must pay before delivery is assigned
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        patient_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        payment_method TEXT CHECK(payment_method IN ('sha','mpesa','mastercard')),
        status TEXT DEFAULT 'Pending' CHECK(status IN ('Pending','Paid','Failed')),
        transaction_ref TEXT,
        created_on TEXT DEFAULT (datetime('now')),
        paid_on TEXT,
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (patient_id) REFERENCES users(id)
    )
    """)

    # ============================================================
    # DELIVERY - rider workflow (privacy-restricted view in app.py)
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        rider_id INTEGER,
        status TEXT DEFAULT 'Assigned' CHECK(status IN ('Assigned','Collected','Delivered')),
        collected_on TEXT,
        delivered_on TEXT,
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (rider_id) REFERENCES users(id)
    )
    """)

    # ============================================================
    # LABORATORY
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS lab_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        clinician_id INTEGER,
        test_name TEXT NOT NULL,
        home_collection INTEGER DEFAULT 0,
        scheduled_date TEXT,
        status TEXT DEFAULT 'Requested' CHECK(status IN
            ('Requested','Collected','Processing','ResultReady','Cancelled')),
        result_text TEXT,
        created_on TEXT DEFAULT (datetime('now')),
        result_uploaded_on TEXT,
        FOREIGN KEY (patient_id) REFERENCES users(id),
        FOREIGN KEY (clinician_id) REFERENCES users(id)
    )
    """)

    # ============================================================
    # NURSING - home-based services
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS nursing_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        nurse_id INTEGER,
        service_type TEXT NOT NULL,     -- e.g. Wound Dressing, Follow-up Care
        scheduled_date TEXT NOT NULL,
        status TEXT DEFAULT 'Requested' CHECK(status IN
            ('Requested','Confirmed','Completed','Cancelled')),
        notes TEXT,
        created_on TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (patient_id) REFERENCES users(id),
        FOREIGN KEY (nurse_id) REFERENCES users(id)
    )
    """)

    # ============================================================
    # CLINICAL NOTES - primarily clinician; lab/nurse can add
    # role-scoped notes tied to their own request only
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id INTEGER NOT NULL,
        staff_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        note_text TEXT NOT NULL,
        related_type TEXT DEFAULT 'General',  -- General / Prescription / Lab / Nursing
        created_on TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (patient_id) REFERENCES users(id),
        FOREIGN KEY (staff_id) REFERENCES users(id)
    )
    """)

    # ============================================================
    # PACKAGES - admin-managed pharma & service bundles
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS packages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        package_type TEXT NOT NULL CHECK(package_type IN ('Pharma','Service')),
        price REAL NOT NULL,
        description TEXT,
        active INTEGER DEFAULT 1
    )
    """)

    # ============================================================
    # FAQ / patient education
    # ============================================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS faqs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL
    )
    """)

    conn.commit()

    # ============================================================
    # SEED DATA
    # ============================================================
    for name, days in [("Hypertension", 30), ("Diabetes Mellitus", 30), ("HIV", 90), ("TB", 30)]:
        cur.execute("INSERT OR IGNORE INTO diseases (name, default_refill_days) VALUES (?, ?)", (name, days))

    # Default admin login (CHANGE THIS PASSWORD after first login)
    cur.execute("SELECT id FROM users WHERE username = 'admin'")
    if not cur.fetchone():
        cur.execute("""
            INSERT INTO users (username, password_hash, role, full_name, phone)
            VALUES (?, ?, 'admin', 'System Administrator', '0700000000')
        """, ("admin", generate_password_hash("ChangeMe123!")))

    demo_clinicians = [
        ("dr.mwangi", "Dr. James Mwangi", "Hypertension", "Physician focused on hypertension & cardiovascular risk management."),
        ("dr.achieng", "Dr. Faith Achieng", "Diabetes Mellitus", "Physician specialising in diabetes mellitus and metabolic care."),
        ("dr.otieno", "Dr. Brian Otieno", "HIV", "HIV care specialist, ART initiation and adherence support."),
        ("dr.wanjiru", "Dr. Grace Wanjiru", "TB", "TB treatment and multimorbidity (HIV/TB co-infection) specialist."),
        ("dr.kamau", "Dr. Susan Kamau", "Mental Health", "Psychiatrist offering virtual mental health consultations."),
        ("dr.njoroge", "Dr. Peter Njoroge", "Mental Health", "Clinical psychologist, counselling & follow-up mental health care."),
    ]
    for username, name, specialty, bio in demo_clinicians:
        cur.execute("SELECT id FROM users WHERE username = ?", (username,))
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO users (username, password_hash, role, full_name, phone, specialty, bio)
                VALUES (?, ?, 'clinician', ?, '0711000000', ?, ?)
            """, (username, generate_password_hash("Doctor123!"), name, specialty, bio))

    starter_meds = [
        ("Amlodipine 10mg", 50.0, 200, "2027-12-31"),
        ("Metformin 500mg", 40.0, 300, "2027-10-31"),
        ("Tenofovir/Lamivudine/Dolutegravir (TLD)", 0.0, 500, "2027-06-30"),
        ("Rifampicin/Isoniazid (RH)", 0.0, 300, "2027-08-31"),
    ]
    for name, price, qty, exp in starter_meds:
        cur.execute("""
            INSERT OR IGNORE INTO medications (name, unit_price, stock_qty, expiry_date)
            VALUES (?, ?, ?, ?)
        """, (name, price, qty, exp))

    starter_packages = [
        ("Hypertension Monthly Refill", "Pharma", 500.0, "One month of standard hypertension medication"),
        ("Diabetes Monthly Refill", "Pharma", 600.0, "One month of standard diabetes medication"),
        ("HIV Care Package (3-monthly)", "Pharma", 0.0, "Quarterly ARV refill - government supported"),
        ("TB Treatment Support Package", "Pharma", 0.0, "Monthly TB medication - government supported"),
        ("Home Nursing Visit", "Service", 800.0, "Single home nursing visit (wound care / follow-up)"),
        ("Home Lab Sample Collection", "Service", 500.0, "Lab technician visits home to collect samples"),
        ("Mental Health Consultation", "Service", 1000.0, "One virtual mental health session"),
    ]
    for name, ptype, price, desc in starter_packages:
        cur.execute("SELECT id FROM packages WHERE name = ?", (name,))
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO packages (name, package_type, price, description) VALUES (?, ?, ?, ?)
            """, (name, ptype, price, desc))

    starter_faqs = [
        ("Hypertension", "What is hypertension?", "Hypertension (high blood pressure) is a chronic condition where the force of blood against your artery walls is consistently too high. It usually has no symptoms but raises the risk of heart attack and stroke if untreated."),
        ("Diabetes", "What is Diabetes Mellitus?", "Diabetes Mellitus is a chronic condition where the body cannot properly regulate blood sugar, either due to insufficient insulin production (Type 1) or insulin resistance (Type 2)."),
        ("HIV", "What is HIV and how is it managed?", "HIV is a virus that attacks the immune system. With consistent antiretroviral therapy (ART), people living with HIV can lead long, healthy lives and maintain an undetectable viral load."),
        ("TB", "What is TB and how long is treatment?", "Tuberculosis (TB) is a bacterial infection, most commonly affecting the lungs. Standard treatment usually lasts 6 months and must be completed fully even if symptoms improve early."),
        ("Multimorbidity", "What does it mean to have more than one chronic condition?", "Multimorbidity means managing two or more chronic conditions at once, e.g. HIV and TB, or Diabetes and Hypertension. Beacon of Hope consolidates your medication pickups and appointments where clinically safe, so you don't need separate hospital trips for each condition."),
        ("Medication Adherence", "Why does taking medication on schedule matter?", "Skipping doses can allow conditions like HIV, TB and Hypertension to worsen or medications to become less effective (e.g. drug resistance in HIV/TB). Always contact your clinician before stopping any medication."),
        ("Side Effects", "What should I do if I experience medication side effects?", "Contact your clinician through the platform's telemedicine feature. Do not stop medication on your own without medical guidance unless a side effect is severe."),
        ("Urgent Care", "When should I seek urgent medical care instead of using this platform?", "Seek emergency in-person care immediately for chest pain, difficulty breathing, severe bleeding, loss of consciousness, or any life-threatening symptom. This platform is for routine chronic disease management, not emergencies."),
        ("Telemedicine", "How do virtual consultations work?", "You can book a clinician or mental health appointment from your dashboard. Once confirmed, you'll receive a virtual consultation link to join at your scheduled time."),
        ("Delivery", "Which days can I receive medication deliveries?", "Deliveries run on Monday, Wednesday and Saturday only. Your delivery date is automatically calculated and, where you have more than one condition, consolidated into a single visit where possible."),
        ("Payments", "What payment methods are supported?", "You can pay via SHA (Social Health Authority), M-Pesa STK Push, or Mastercard through our payment gateway. Payment must be completed before your order is dispatched for delivery."),
        ("Laboratory", "Can I get lab tests done without visiting the hospital?", "Yes - where available, you can request home sample collection and a lab professional will visit you. Results are uploaded to your dashboard once ready."),
        ("Nursing", "What home nursing services are available?", "Approved home-based nursing services include wound dressing and follow-up nursing care. You can request these directly from your dashboard."),
    ]
    for cat, q, a in starter_faqs:
        cur.execute("SELECT id FROM faqs WHERE question = ?", (q,))
        if not cur.fetchone():
            cur.execute("INSERT INTO faqs (category, question, answer) VALUES (?, ?, ?)", (cat, q, a))

    conn.commit()
    conn.close()
    print(f"Database ready at: {DB_NAME}")
    print("Default admin login -> username: admin | password: ChangeMe123!  (change this immediately)")


if __name__ == "__main__":
    init_db()
