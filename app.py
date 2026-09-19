"""
app.py
------
Beacon of Hope - unified telemedicine + pharmacy + delivery platform.

RUN:
  pip install -r requirements.txt
  python database.py      (once, creates beacon.db + default admin)
  python app.py
  Open http://127.0.0.1:5000

Default admin login: username 'admin', password 'ChangeMe123!'
Use the admin dashboard to create clinician/pharmacist/nurse/lab/rider
accounts. Patients self-register via /signup.
"""

from flask import Flask, render_template, request, redirect, url_for, flash, session
from datetime import datetime

from database import get_connection, init_db
from auth import create_user, verify_login, current_user, login_required, role_required
from scheduling import create_or_update_pending_order, mark_order_delivered_and_reschedule
import billing

app = Flask(__name__)
app.secret_key = "change-this-secret-key-before-deploying"


@app.context_processor
def inject_user():
    return {"logged_in_user": current_user()}


# ============================================================
# HOME / AUTH
# ============================================================
@app.route("/")
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    return redirect(url_for(f"{user['role']}_dashboard"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Patient self-signup only. Staff accounts are created by admin."""
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        full_name = request.form["full_name"]
        phone = request.form["phone"]
        area = request.form["area"]

        user_id, error = create_user(username, password, "patient", full_name, phone, area)
        if error:
            flash(error)
            return redirect(url_for("signup"))

        conn = get_connection()
        conn.execute("""
            INSERT INTO patient_profiles (user_id, national_id, date_of_birth, gender, address)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, request.form.get("national_id"), request.form.get("date_of_birth"),
              request.form.get("gender"), request.form.get("address")))
        conn.commit()
        conn.close()

        flash("Account created. Please log in.")
        return redirect(url_for("login"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = verify_login(request.form["username"].strip(), request.form["password"])
        if user:
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            session["full_name"] = user["full_name"]
            flash(f"Welcome, {user['full_name']}.")
            nxt = request.args.get("next")
            return redirect(nxt or url_for("home"))
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/faq")
def faq():
    conn = get_connection()
    faqs = conn.execute("SELECT * FROM faqs ORDER BY category").fetchall()
    conn.close()
    return render_template("faq.html", faqs=faqs)


# ============================================================
# PATIENT DASHBOARD
# ============================================================
@app.route("/patient/dashboard")
@role_required("patient")
def patient_dashboard():
    pid = session["user_id"]
    conn = get_connection()

    profile = conn.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (pid,)).fetchone()
    diseases = conn.execute("""
        SELECT d.name, pd.diagnosis_date, pd.last_pickup_date
        FROM patient_diseases pd JOIN diseases d ON d.id = pd.disease_id
        WHERE pd.patient_id = ? AND pd.active = 1
    """, (pid,)).fetchall()

    appointments = conn.execute("""
        SELECT a.*, u.full_name as clinician_name
        FROM appointments a LEFT JOIN users u ON u.id = a.clinician_id
        WHERE a.patient_id = ? ORDER BY a.requested_date DESC
    """, (pid,)).fetchall()

    prescriptions = conn.execute("""
        SELECT p.*, u.full_name as clinician_name, d.name as disease_name
        FROM prescriptions p JOIN users u ON u.id = p.clinician_id
        LEFT JOIN diseases d ON d.id = p.disease_id
        WHERE p.patient_id = ? ORDER BY p.date_issued DESC
    """, (pid,)).fetchall()

    orders = conn.execute("SELECT * FROM orders WHERE patient_id = ? ORDER BY id DESC", (pid,)).fetchall()

    bills = conn.execute("""
        SELECT b.*, o.delivery_date FROM bills b JOIN orders o ON o.id = b.order_id
        WHERE b.patient_id = ? ORDER BY b.id DESC
    """, (pid,)).fetchall()

    lab_requests = conn.execute("SELECT * FROM lab_requests WHERE patient_id = ? ORDER BY id DESC", (pid,)).fetchall()
    nursing_requests = conn.execute("SELECT * FROM nursing_requests WHERE patient_id = ? ORDER BY id DESC", (pid,)).fetchall()

    conn.close()
    return render_template(
        "patient_dashboard.html", profile=profile, diseases=diseases, appointments=appointments,
        prescriptions=prescriptions, orders=orders, bills=bills,
        lab_requests=lab_requests, nursing_requests=nursing_requests,
    )


@app.route("/patient/diseases/add", methods=["POST"])
@role_required("patient")
def patient_add_disease():
    """Patients declare a diagnosed condition (a clinician can also confirm/add via notes)."""
    pid = session["user_id"]
    disease_id = request.form["disease_id"]
    diagnosis_date = request.form["diagnosis_date"]
    conn = get_connection()
    conn.execute("""
        INSERT OR IGNORE INTO patient_diseases (patient_id, disease_id, diagnosis_date)
        VALUES (?, ?, ?)
    """, (pid, disease_id, diagnosis_date))
    conn.commit()
    conn.close()
    flash("Condition added to your profile.")
    return redirect(url_for("patient_dashboard"))


@app.route("/doctors")
@role_required("patient")
def browse_doctors():
    """
    Telemedicine doctor directory. Patients pick a specialty (an NCD -
    hypertension/diabetes/HIV/TB - or Mental Health) then see the
    specific doctors in that specialty and their open slots.
    """
    specialty_filter = request.args.get("specialty")
    conn = get_connection()

    specialties = [r["specialty"] for r in conn.execute(
        "SELECT DISTINCT specialty FROM users WHERE role='clinician' AND specialty IS NOT NULL ORDER BY specialty"
    ).fetchall()]

    query = "SELECT * FROM users WHERE role = 'clinician'"
    params = []
    if specialty_filter:
        query += " AND specialty = ?"
        params.append(specialty_filter)
    query += " ORDER BY specialty, full_name"
    doctors = conn.execute(query, params).fetchall()

    # open (unbooked), future-dated slot count per doctor, for a quick "X slots open" badge
    open_slots_count = {}
    for d in doctors:
        c = conn.execute("""
            SELECT COUNT(*) c FROM availability_slots
            WHERE clinician_id = ? AND is_booked = 0 AND slot_date >= date('now')
        """, (d["id"],)).fetchone()["c"]
        open_slots_count[d["id"]] = c

    conn.close()
    return render_template("doctors.html", doctors=doctors, specialties=specialties,
                            specialty_filter=specialty_filter, open_slots_count=open_slots_count)


@app.route("/doctors/<int:clinician_id>")
@role_required("patient")
def doctor_profile(clinician_id):
    conn = get_connection()
    doctor = conn.execute("SELECT * FROM users WHERE id = ? AND role = 'clinician'", (clinician_id,)).fetchone()
    slots = conn.execute("""
        SELECT * FROM availability_slots
        WHERE clinician_id = ? AND is_booked = 0 AND slot_date >= date('now')
        ORDER BY slot_date, slot_time
    """, (clinician_id,)).fetchall()
    conn.close()
    if not doctor:
        flash("Doctor not found.")
        return redirect(url_for("browse_doctors"))
    return render_template("doctor_profile.html", doctor=doctor, slots=slots)


@app.route("/patient/appointments/book-slot/<int:slot_id>", methods=["POST"])
@role_required("patient")
def patient_book_slot(slot_id):
    """Patient books a SPECIFIC open slot with a SPECIFIC doctor."""
    pid = session["user_id"]
    appt_type = request.form["appointment_type"]
    reason = request.form.get("reason", "")

    conn = get_connection()
    cur = conn.cursor()
    slot = cur.execute("SELECT * FROM availability_slots WHERE id = ? AND is_booked = 0", (slot_id,)).fetchone()
    if not slot:
        conn.close()
        flash("Sorry, that slot was just taken. Please pick another.")
        return redirect(url_for("browse_doctors"))

    cur.execute("""
        INSERT INTO appointments (patient_id, clinician_id, slot_id, appointment_type,
                                   requested_date, reason, booked_by, status)
        VALUES (?, ?, ?, ?, ?, ?, 'patient', 'Confirmed')
    """, (pid, slot["clinician_id"], slot_id, appt_type, slot["slot_date"], reason))
    cur.execute("UPDATE availability_slots SET is_booked = 1 WHERE id = ?", (slot_id,))
    conn.commit()
    doctor_name = cur.execute("SELECT full_name FROM users WHERE id = ?", (slot["clinician_id"],)).fetchone()["full_name"]
    conn.close()

    flash(f"Appointment confirmed with {doctor_name} on {slot['slot_date']} at {slot['slot_time']}.")
    return redirect(url_for("patient_dashboard"))


@app.route("/patient/appointments/new", methods=["POST"])
@role_required("patient")
def patient_book_appointment():
    """
    Fallback path: patient doesn't want to pick a doctor/slot themselves
    and instead asks Beacon of Hope staff to book on their behalf
    (admin will assign a doctor and slot from the admin dashboard).
    """
    pid = session["user_id"]
    appt_type = request.form["appointment_type"]
    requested_date = request.form["requested_date"]
    reason = request.form.get("reason", "")

    conn = get_connection()
    conn.execute("""
        INSERT INTO appointments (patient_id, appointment_type, requested_date, reason, booked_by, status)
        VALUES (?, ?, ?, ?, 'admin_on_behalf', 'Requested')
    """, (pid, appt_type, requested_date, reason))
    conn.commit()
    conn.close()

    flash("Your request has been sent to Beacon of Hope staff, who will book a suitable doctor for you.")
    return redirect(url_for("patient_dashboard"))


@app.route("/patient/lab/new", methods=["POST"])
@role_required("patient")
def patient_request_lab():
    pid = session["user_id"]
    test_name = request.form["test_name"]
    home_collection = 1 if request.form.get("home_collection") else 0
    scheduled_date = request.form.get("scheduled_date") or None

    conn = get_connection()
    conn.execute("""
        INSERT INTO lab_requests (patient_id, test_name, home_collection, scheduled_date)
        VALUES (?, ?, ?, ?)
    """, (pid, test_name, home_collection, scheduled_date))
    conn.commit()
    conn.close()
    flash("Lab request submitted.")
    return redirect(url_for("patient_dashboard"))


@app.route("/patient/nursing/new", methods=["POST"])
@role_required("patient")
def patient_request_nursing():
    pid = session["user_id"]
    service_type = request.form["service_type"]
    scheduled_date = request.form["scheduled_date"]

    conn = get_connection()
    conn.execute("""
        INSERT INTO nursing_requests (patient_id, service_type, scheduled_date)
        VALUES (?, ?, ?)
    """, (pid, service_type, scheduled_date))
    conn.commit()
    conn.close()
    flash("Home nursing service requested.")
    return redirect(url_for("patient_dashboard"))


@app.route("/patient/orders/<int:order_id>/pay", methods=["POST"])
@role_required("patient")
def patient_pay_order(order_id):
    pid = session["user_id"]
    conn = get_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND patient_id = ?", (order_id, pid)).fetchone()
    bill = conn.execute("SELECT * FROM bills WHERE order_id = ?", (order_id,)).fetchone()
    conn.close()

    if not order or not bill:
        flash("No bill found for this order yet - the pharmacist needs to generate it first.")
        return redirect(url_for("patient_dashboard"))

    method = request.form["method"]
    if method == "mpesa":
        result = billing.initiate_mpesa_stk_push(order_id, pid, bill["amount"], request.form["phone_number"])
    elif method == "mastercard":
        result = billing.initiate_card_payment(order_id, pid, bill["amount"], request.form["card_last4"], request.form["email"])
    elif method == "sha":
        result = billing.initiate_sha_claim(order_id, pid, bill["amount"], request.form["sha_member_number"])
    else:
        flash("Unknown payment method.")
        return redirect(url_for("patient_dashboard"))

    flash(result.get("message", "Payment initiated."))
    return redirect(url_for("patient_dashboard"))


# ============================================================
# CLINICIAN DASHBOARD - telemedicine: availability + appointments
# ============================================================
@app.route("/clinician/dashboard")
@role_required("clinician", "admin")
def clinician_dashboard():
    cid = session["user_id"]
    conn = get_connection()

    upcoming = conn.execute("""
        SELECT a.*, u.full_name as patient_name, u.phone as patient_phone
        FROM appointments a JOIN users u ON u.id = a.patient_id
        WHERE a.clinician_id = ? AND a.status IN ('Confirmed','Requested')
        ORDER BY a.requested_date
    """, (cid,)).fetchall()

    unassigned_requests = conn.execute("""
        SELECT a.*, u.full_name as patient_name
        FROM appointments a JOIN users u ON u.id = a.patient_id
        WHERE a.clinician_id IS NULL AND a.status = 'Requested'
        ORDER BY a.requested_date
    """).fetchall()

    my_slots = conn.execute("""
        SELECT * FROM availability_slots WHERE clinician_id = ? AND slot_date >= date('now')
        ORDER BY slot_date, slot_time
    """, (cid,)).fetchall()

    recent_patients = conn.execute("""
        SELECT DISTINCT u.id, u.full_name FROM appointments a
        JOIN users u ON u.id = a.patient_id WHERE a.clinician_id = ?
        ORDER BY a.created_on DESC LIMIT 15
    """, (cid,)).fetchall()

    conn.close()
    return render_template("clinician_dashboard.html", upcoming=upcoming,
                            unassigned_requests=unassigned_requests,
                            my_slots=my_slots, recent_patients=recent_patients)


@app.route("/clinician/availability/add", methods=["POST"])
@role_required("clinician", "admin")
def clinician_add_slot():
    cid = session["user_id"]
    slot_date = request.form["slot_date"]
    slot_time = request.form["slot_time"]
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO availability_slots (clinician_id, slot_date, slot_time) VALUES (?, ?, ?)
        """, (cid, slot_date, slot_time))
        conn.commit()
        flash(f"Slot added: {slot_date} at {slot_time}.")
    except Exception:
        flash("That slot already exists.")
    conn.close()
    return redirect(url_for("clinician_dashboard"))


@app.route("/clinician/appointments/<int:appt_id>/claim", methods=["POST"])
@role_required("clinician", "admin")
def clinician_claim_appointment(appt_id):
    """Clinician picks up an unassigned 'ask Beacon to book for me' request."""
    cid = session["user_id"]
    conn = get_connection()
    conn.execute("""
        UPDATE appointments SET clinician_id = ?, status = 'Confirmed' WHERE id = ? AND clinician_id IS NULL
    """, (cid, appt_id))
    conn.commit()
    conn.close()
    flash("Appointment assigned to you and confirmed.")
    return redirect(url_for("clinician_dashboard"))


@app.route("/clinician/appointments/<int:appt_id>/link", methods=["POST"])
@role_required("clinician", "admin")
def clinician_set_consult_link(appt_id):
    consult_link = request.form["consult_link"]
    conn = get_connection()
    conn.execute("UPDATE appointments SET consult_link = ? WHERE id = ?", (consult_link, appt_id))
    conn.commit()
    conn.close()
    flash("Consultation link saved. The patient will see it on their dashboard.")
    return redirect(url_for("clinician_dashboard"))


@app.route("/clinician/appointments/<int:appt_id>/complete", methods=["POST"])
@role_required("clinician", "admin")
def clinician_complete_appointment(appt_id):
    conn = get_connection()
    conn.execute("UPDATE appointments SET status = 'Completed' WHERE id = ?", (appt_id,))
    conn.commit()
    conn.close()
    flash("Appointment marked completed.")
    return redirect(url_for("clinician_dashboard"))


@app.route("/clinician/patients/<int:patient_id>")
@role_required("clinician", "admin")
def clinician_view_patient(patient_id):
    conn = get_connection()
    patient = conn.execute("SELECT * FROM users WHERE id = ?", (patient_id,)).fetchone()
    profile = conn.execute("SELECT * FROM patient_profiles WHERE user_id = ?", (patient_id,)).fetchone()
    diseases = conn.execute("""
        SELECT d.name, pd.diagnosis_date, pd.last_pickup_date FROM patient_diseases pd
        JOIN diseases d ON d.id = pd.disease_id WHERE pd.patient_id = ? AND pd.active = 1
    """, (patient_id,)).fetchall()
    prescriptions = conn.execute("""
        SELECT p.*, d.name as disease_name FROM prescriptions p
        LEFT JOIN diseases d ON d.id = p.disease_id
        WHERE p.patient_id = ? ORDER BY p.date_issued DESC
    """, (patient_id,)).fetchall()
    notes = conn.execute("""
        SELECT n.*, u.full_name as staff_name FROM notes n JOIN users u ON u.id = n.staff_id
        WHERE n.patient_id = ? ORDER BY n.created_on DESC
    """, (patient_id,)).fetchall()
    all_diseases = conn.execute("SELECT * FROM diseases").fetchall()
    conn.close()
    return render_template("clinician_patient.html", patient=patient, profile=profile,
                            diseases=diseases, prescriptions=prescriptions, notes=notes,
                            all_diseases=all_diseases)


@app.route("/clinician/patients/<int:patient_id>/prescriptions/new", methods=["POST"])
@role_required("clinician", "admin")
def clinician_new_prescription(patient_id):
    cid = session["user_id"]
    disease_id = request.form.get("disease_id") or None
    medication_name = request.form["medication_name"]
    dosage = request.form["dosage"]
    instructions = request.form.get("instructions", "")

    conn = get_connection()
    conn.execute("""
        INSERT INTO prescriptions (patient_id, clinician_id, disease_id, medication_name, dosage, instructions)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (patient_id, cid, disease_id, medication_name, dosage, instructions))
    conn.commit()
    conn.close()

    create_or_update_pending_order(patient_id)
    flash("Prescription issued and added to the patient's next delivery order.")
    return redirect(url_for("clinician_view_patient", patient_id=patient_id))


@app.route("/clinician/prescriptions/<int:rx_id>/adjust", methods=["POST"])
@role_required("clinician", "admin")
def clinician_adjust_prescription(rx_id):
    new_dosage = request.form["dosage"]
    instructions = request.form.get("instructions", "")
    patient_id = request.form["patient_id"]

    conn = get_connection()
    conn.execute("""
        UPDATE prescriptions SET dosage = ?, instructions = ?, status = 'Adjusted' WHERE id = ?
    """, (new_dosage, instructions, rx_id))
    conn.commit()
    conn.close()
    flash("Prescription dose adjusted.")
    return redirect(url_for("clinician_view_patient", patient_id=patient_id))


@app.route("/clinician/patients/<int:patient_id>/notes/new", methods=["POST"])
@role_required("clinician", "nurse", "lab", "admin")
def add_clinical_note(patient_id):
    staff_id = session["user_id"]
    role = session["role"]
    note_text = request.form["note_text"]
    related_type = request.form.get("related_type", "General")

    conn = get_connection()
    conn.execute("""
        INSERT INTO notes (patient_id, staff_id, role, note_text, related_type)
        VALUES (?, ?, ?, ?, ?)
    """, (patient_id, staff_id, role, note_text, related_type))
    conn.commit()
    conn.close()
    flash("Note added.")
    return redirect(request.referrer or url_for("home"))


@app.route("/clinician/lab-requests/new", methods=["POST"])
@role_required("clinician", "admin")
def clinician_request_lab():
    cid = session["user_id"]
    patient_id = request.form["patient_id"]
    test_name = request.form["test_name"]
    home_collection = 1 if request.form.get("home_collection") else 0

    conn = get_connection()
    conn.execute("""
        INSERT INTO lab_requests (patient_id, clinician_id, test_name, home_collection)
        VALUES (?, ?, ?, ?)
    """, (patient_id, cid, test_name, home_collection))
    conn.commit()
    conn.close()
    flash("Lab request sent.")
    return redirect(url_for("clinician_view_patient", patient_id=patient_id))


# ============================================================
# PHARMACIST - orders, inventory, billing
# ============================================================
@app.route("/pharmacist/dashboard")
@role_required("pharmacist", "admin")
def pharmacist_dashboard():
    conn = get_connection()
    orders = conn.execute("""
        SELECT o.*, u.full_name as patient_name FROM orders o JOIN users u ON u.id = o.patient_id
        WHERE o.status NOT IN ('Delivered','Cancelled') ORDER BY o.delivery_date
    """).fetchall()
    medications = conn.execute("SELECT * FROM medications ORDER BY name").fetchall()
    conn.close()
    return render_template("pharmacist_dashboard.html", orders=orders, medications=medications)


@app.route("/pharmacist/orders/<int:order_id>")
@role_required("pharmacist", "admin")
def pharmacist_view_order(order_id):
    conn = get_connection()
    order = conn.execute("""
        SELECT o.*, u.full_name as patient_name, u.phone as patient_phone
        FROM orders o JOIN users u ON u.id = o.patient_id WHERE o.id = ?
    """, (order_id,)).fetchone()
    items = conn.execute("""
        SELECT oi.*, m.name as med_name FROM order_items oi
        JOIN medications m ON m.id = oi.medication_id WHERE oi.order_id = ?
    """, (order_id,)).fetchall()
    prescriptions = conn.execute("""
        SELECT * FROM prescriptions WHERE patient_id = ? AND status != 'Stopped' ORDER BY date_issued DESC
    """, (order["patient_id"],)).fetchall()
    medications = conn.execute("SELECT * FROM medications ORDER BY name").fetchall()
    bill = conn.execute("SELECT * FROM bills WHERE order_id = ?", (order_id,)).fetchone()
    riders = conn.execute("SELECT * FROM users WHERE role = 'rider'").fetchall()
    conn.close()
    return render_template("pharmacist_order.html", order=order, items=items, prescriptions=prescriptions,
                            medications=medications, bill=bill, riders=riders)


@app.route("/pharmacist/orders/<int:order_id>/verify", methods=["POST"])
@role_required("pharmacist", "admin")
def pharmacist_verify_order(order_id):
    conn = get_connection()
    conn.execute("UPDATE orders SET status = 'Verifying' WHERE id = ? AND status = 'Received'", (order_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("pharmacist_view_order", order_id=order_id))


@app.route("/pharmacist/orders/<int:order_id>/items/add", methods=["POST"])
@role_required("pharmacist", "admin")
def pharmacist_add_item(order_id):
    medication_id = request.form["medication_id"]
    quantity = int(request.form["quantity"])
    prescription_id = request.form.get("prescription_id") or None

    conn = get_connection()
    med = conn.execute("SELECT * FROM medications WHERE id = ?", (medication_id,)).fetchone()
    conn.execute("""
        INSERT INTO order_items (order_id, prescription_id, medication_id, quantity, unit_price)
        VALUES (?, ?, ?, ?, ?)
    """, (order_id, prescription_id, medication_id, quantity, med["unit_price"]))
    conn.commit()
    conn.close()
    flash(f"Added {quantity} x {med['name']} to order.")
    return redirect(url_for("pharmacist_view_order", order_id=order_id))


@app.route("/pharmacist/orders/<int:order_id>/mark-available", methods=["POST"])
@role_required("pharmacist", "admin")
def pharmacist_mark_available(order_id):
    conn = get_connection()
    conn.execute("UPDATE orders SET status = 'Available' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    flash("Order marked available - ready to dispense.")
    return redirect(url_for("pharmacist_view_order", order_id=order_id))


@app.route("/pharmacist/orders/<int:order_id>/dispense", methods=["POST"])
@role_required("pharmacist", "admin")
def pharmacist_dispense(order_id):
    conn = get_connection()
    cur = conn.cursor()
    items = cur.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    for item in items:
        cur.execute("UPDATE medications SET stock_qty = stock_qty - ? WHERE id = ?",
                     (item["quantity"], item["medication_id"]))
    cur.execute("UPDATE orders SET status = 'Dispensed' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    flash("Order dispensed. Stock levels updated.")
    return redirect(url_for("pharmacist_view_order", order_id=order_id))


@app.route("/pharmacist/orders/<int:order_id>/generate-bill", methods=["POST"])
@role_required("pharmacist", "admin")
def pharmacist_generate_bill(order_id):
    conn = get_connection()
    cur = conn.cursor()
    order = cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    items = cur.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
    total = sum(i["quantity"] * i["unit_price"] for i in items)

    existing_bill = cur.execute("SELECT id FROM bills WHERE order_id = ?", (order_id,)).fetchone()
    if not existing_bill:
        cur.execute("""
            INSERT INTO bills (order_id, patient_id, amount, status) VALUES (?, ?, ?, 'Pending')
        """, (order_id, order["patient_id"], total))
    else:
        cur.execute("UPDATE bills SET amount = ? WHERE id = ?", (total, existing_bill["id"]))

    cur.execute("UPDATE orders SET status = 'Billed' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    flash(f"Bill generated: KES {total}. Patient can now pay from their dashboard.")
    return redirect(url_for("pharmacist_view_order", order_id=order_id))


@app.route("/pharmacist/orders/<int:order_id>/confirm-payment", methods=["POST"])
@role_required("pharmacist", "admin")
def pharmacist_confirm_payment(order_id):
    """Manual override - e.g. for cash, or confirming an SHA/M-Pesa payment came through."""
    billing.mark_bill_paid(order_id)
    flash("Payment confirmed. Order can now be assigned to a rider.")
    return redirect(url_for("pharmacist_view_order", order_id=order_id))


@app.route("/pharmacist/orders/<int:order_id>/assign-rider", methods=["POST"])
@role_required("pharmacist", "admin")
def assign_rider(order_id):
    rider_id = request.form["rider_id"]
    conn = get_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order["status"] != "Paid":
        conn.close()
        flash("Order must be fully paid before it can be assigned for delivery.")
        return redirect(url_for("pharmacist_view_order", order_id=order_id))

    conn.execute("UPDATE orders SET status = 'Assigned' WHERE id = ?", (order_id,))
    conn.execute("INSERT INTO deliveries (order_id, rider_id, status) VALUES (?, ?, 'Assigned')",
                 (order_id, rider_id))
    conn.commit()
    conn.close()
    flash("Rider assigned.")
    return redirect(url_for("pharmacist_view_order", order_id=order_id))


@app.route("/inventory")
@role_required("pharmacist", "admin")
def inventory():
    conn = get_connection()
    medications = conn.execute("SELECT * FROM medications ORDER BY name").fetchall()
    conn.close()
    return render_template("inventory.html", medications=medications)


@app.route("/inventory/add", methods=["POST"])
@role_required("pharmacist", "admin")
def inventory_add():
    conn = get_connection()
    conn.execute("""
        INSERT INTO medications (name, unit_price, stock_qty, low_stock_threshold, expiry_date)
        VALUES (?, ?, ?, ?, ?)
    """, (request.form["name"], float(request.form["unit_price"]), int(request.form["stock_qty"]),
          int(request.form.get("low_stock_threshold", 10)), request.form.get("expiry_date")))
    conn.commit()
    conn.close()
    flash("Medication added to inventory.")
    return redirect(url_for("inventory"))


@app.route("/inventory/<int:med_id>/update", methods=["POST"])
@role_required("pharmacist", "admin")
def inventory_update(med_id):
    conn = get_connection()
    conn.execute("""
        UPDATE medications SET unit_price = ?, stock_qty = ?, low_stock_threshold = ?, expiry_date = ?
        WHERE id = ?
    """, (float(request.form["unit_price"]), int(request.form["stock_qty"]),
          int(request.form.get("low_stock_threshold", 10)), request.form.get("expiry_date"), med_id))
    conn.commit()
    conn.close()
    flash("Inventory updated.")
    return redirect(url_for("inventory"))


# ============================================================
# NURSE - home-based services
# ============================================================
@app.route("/nurse/dashboard")
@role_required("nurse", "admin")
def nurse_dashboard():
    nid = session["user_id"]
    conn = get_connection()
    unassigned = conn.execute("""
        SELECT nr.*, u.full_name as patient_name, u.phone as patient_phone
        FROM nursing_requests nr JOIN users u ON u.id = nr.patient_id
        WHERE nr.nurse_id IS NULL AND nr.status = 'Requested'
        ORDER BY nr.scheduled_date
    """).fetchall()
    mine = conn.execute("""
        SELECT nr.*, u.full_name as patient_name, u.phone as patient_phone
        FROM nursing_requests nr JOIN users u ON u.id = nr.patient_id
        WHERE nr.nurse_id = ? AND nr.status != 'Completed'
        ORDER BY nr.scheduled_date
    """, (nid,)).fetchall()
    conn.close()
    return render_template("nurse_dashboard.html", unassigned=unassigned, mine=mine)


@app.route("/nurse/requests/<int:req_id>/claim", methods=["POST"])
@role_required("nurse", "admin")
def nurse_claim(req_id):
    nid = session["user_id"]
    conn = get_connection()
    conn.execute("UPDATE nursing_requests SET nurse_id = ?, status = 'Confirmed' WHERE id = ?", (nid, req_id))
    conn.commit()
    conn.close()
    flash("Nursing visit confirmed and assigned to you.")
    return redirect(url_for("nurse_dashboard"))


@app.route("/nurse/requests/<int:req_id>/complete", methods=["POST"])
@role_required("nurse", "admin")
def nurse_complete(req_id):
    notes = request.form.get("notes", "")
    conn = get_connection()
    conn.execute("UPDATE nursing_requests SET status = 'Completed', notes = ? WHERE id = ?", (notes, req_id))
    conn.commit()
    conn.close()
    flash("Visit marked completed.")
    return redirect(url_for("nurse_dashboard"))


# ============================================================
# LAB
# ============================================================
@app.route("/lab/dashboard")
@role_required("lab", "admin")
def lab_dashboard():
    conn = get_connection()
    requests_ = conn.execute("""
        SELECT lr.*, u.full_name as patient_name, u.phone as patient_phone
        FROM lab_requests lr JOIN users u ON u.id = lr.patient_id
        WHERE lr.status != 'Cancelled' ORDER BY lr.created_on DESC
    """).fetchall()
    conn.close()
    return render_template("lab_dashboard.html", requests=requests_)


@app.route("/lab/requests/<int:req_id>/collect", methods=["POST"])
@role_required("lab", "admin")
def lab_collect(req_id):
    conn = get_connection()
    conn.execute("UPDATE lab_requests SET status = 'Collected' WHERE id = ?", (req_id,))
    conn.commit()
    conn.close()
    flash("Sample marked collected.")
    return redirect(url_for("lab_dashboard"))


@app.route("/lab/requests/<int:req_id>/result", methods=["POST"])
@role_required("lab", "admin")
def lab_upload_result(req_id):
    result_text = request.form["result_text"]
    conn = get_connection()
    conn.execute("""
        UPDATE lab_requests SET result_text = ?, status = 'ResultReady', result_uploaded_on = datetime('now')
        WHERE id = ?
    """, (result_text, req_id))
    conn.commit()
    conn.close()
    flash("Result uploaded. Visible to the patient and their clinician.")
    return redirect(url_for("lab_dashboard"))


# ============================================================
# RIDER - delivery only, privacy-restricted (no meds/prices/diseases)
# ============================================================
@app.route("/rider/dashboard")
@role_required("rider", "admin")
def rider_dashboard():
    rid = session["user_id"]
    conn = get_connection()
    # Deliberately SELECT only what a rider needs: no diseases_covered, no bill amount, no medication names.
    deliveries = conn.execute("""
        SELECT del.id as delivery_id, del.status as delivery_status, o.id as order_id,
               o.delivery_date, u.full_name as patient_name, u.phone as patient_phone,
               pp.address as patient_address
        FROM deliveries del
        JOIN orders o ON o.id = del.order_id
        JOIN users u ON u.id = o.patient_id
        LEFT JOIN patient_profiles pp ON pp.user_id = u.id
        WHERE del.rider_id = ? AND del.status != 'Delivered'
        ORDER BY o.delivery_date
    """, (rid,)).fetchall()
    conn.close()
    return render_template("rider_dashboard.html", deliveries=deliveries)


@app.route("/rider/deliveries/<int:delivery_id>/collect", methods=["POST"])
@role_required("rider", "admin")
def rider_collect(delivery_id):
    conn = get_connection()
    conn.execute("""
        UPDATE deliveries SET status = 'Collected', collected_on = datetime('now') WHERE id = ?
    """, (delivery_id,))
    order_id = conn.execute("SELECT order_id FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()["order_id"]
    conn.execute("UPDATE orders SET status = 'Collected' WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()
    flash("Package collection confirmed.")
    return redirect(url_for("rider_dashboard"))


@app.route("/rider/deliveries/<int:delivery_id>/deliver", methods=["POST"])
@role_required("rider", "admin")
def rider_deliver(delivery_id):
    conn = get_connection()
    delivery = conn.execute("SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
    conn.execute("""
        UPDATE deliveries SET status = 'Delivered', delivered_on = datetime('now') WHERE id = ?
    """, (delivery_id,))
    conn.execute("UPDATE orders SET status = 'Delivered' WHERE id = ?", (delivery["order_id"],))
    conn.commit()
    conn.close()

    mark_order_delivered_and_reschedule(delivery["order_id"])
    flash("Delivery confirmed. Patient's next consolidated order has been generated automatically.")
    return redirect(url_for("rider_dashboard"))


# ============================================================
# ADMIN - users, packages, monitoring, booking-on-behalf
# ============================================================
@app.route("/admin/dashboard")
@role_required("admin")
def admin_dashboard():
    conn = get_connection()
    stats = {
        "patients": conn.execute("SELECT COUNT(*) c FROM users WHERE role='patient'").fetchone()["c"],
        "clinicians": conn.execute("SELECT COUNT(*) c FROM users WHERE role='clinician'").fetchone()["c"],
        "orders_active": conn.execute("SELECT COUNT(*) c FROM orders WHERE status NOT IN ('Delivered','Cancelled')").fetchone()["c"],
        "appointments_pending": conn.execute("SELECT COUNT(*) c FROM appointments WHERE status='Requested'").fetchone()["c"],
        "low_stock": conn.execute("SELECT COUNT(*) c FROM medications WHERE stock_qty <= low_stock_threshold").fetchone()["c"],
    }
    pending_on_behalf = conn.execute("""
        SELECT a.*, u.full_name as patient_name FROM appointments a JOIN users u ON u.id = a.patient_id
        WHERE a.booked_by = 'admin_on_behalf' AND a.status = 'Requested' ORDER BY a.requested_date
    """).fetchall()
    all_orders = conn.execute("""
        SELECT o.*, u.full_name as patient_name FROM orders o JOIN users u ON u.id = o.patient_id
        ORDER BY o.id DESC LIMIT 25
    """).fetchall()
    conn.close()
    return render_template("admin_dashboard.html", stats=stats, pending_on_behalf=pending_on_behalf, all_orders=all_orders)


@app.route("/admin/users")
@role_required("admin")
def admin_users():
    conn = get_connection()
    users = conn.execute("SELECT * FROM users WHERE role != 'patient' ORDER BY role, full_name").fetchall()
    conn.close()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/new", methods=["POST"])
@role_required("admin")
def admin_create_staff():
    username = request.form["username"].strip()
    password = request.form["password"]
    role = request.form["role"]
    full_name = request.form["full_name"]
    phone = request.form.get("phone")
    area = request.form.get("area")
    specialty = request.form.get("specialty") or None
    bio = request.form.get("bio") or None

    user_id, error = create_user(username, password, role, full_name, phone, area)
    if error:
        flash(error)
        return redirect(url_for("admin_users"))

    if role == "clinician":
        conn = get_connection()
        conn.execute("UPDATE users SET specialty = ?, bio = ? WHERE id = ?", (specialty, bio, user_id))
        conn.commit()
        conn.close()

    flash(f"{role.title()} account created for {full_name}.")
    return redirect(url_for("admin_users"))


@app.route("/admin/appointments/<int:appt_id>/assign", methods=["POST"])
@role_required("admin")
def admin_assign_appointment(appt_id):
    """Admin books an unassigned patient request onto a specific doctor's open slot."""
    slot_id = request.form["slot_id"]
    conn = get_connection()
    cur = conn.cursor()
    slot = cur.execute("SELECT * FROM availability_slots WHERE id = ? AND is_booked = 0", (slot_id,)).fetchone()
    if not slot:
        conn.close()
        flash("That slot is no longer available.")
        return redirect(url_for("admin_dashboard"))

    cur.execute("""
        UPDATE appointments SET clinician_id = ?, slot_id = ?, requested_date = ?, status = 'Confirmed'
        WHERE id = ?
    """, (slot["clinician_id"], slot_id, slot["slot_date"], appt_id))
    cur.execute("UPDATE availability_slots SET is_booked = 1 WHERE id = ?", (slot_id,))
    conn.commit()
    conn.close()
    flash("Appointment booked on the patient's behalf.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/packages")
@role_required("admin")
def admin_packages():
    conn = get_connection()
    packages = conn.execute("SELECT * FROM packages ORDER BY package_type, name").fetchall()
    conn.close()
    return render_template("admin_packages.html", packages=packages)


@app.route("/admin/packages/new", methods=["POST"])
@role_required("admin")
def admin_new_package():
    conn = get_connection()
    conn.execute("""
        INSERT INTO packages (name, package_type, price, description) VALUES (?, ?, ?, ?)
    """, (request.form["name"], request.form["package_type"], float(request.form["price"]),
          request.form.get("description", "")))
    conn.commit()
    conn.close()
    flash("Package created.")
    return redirect(url_for("admin_packages"))


@app.route("/admin/packages/<int:pkg_id>/toggle", methods=["POST"])
@role_required("admin")
def admin_toggle_package(pkg_id):
    conn = get_connection()
    pkg = conn.execute("SELECT active FROM packages WHERE id = ?", (pkg_id,)).fetchone()
    conn.execute("UPDATE packages SET active = ? WHERE id = ?", (0 if pkg["active"] else 1, pkg_id))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_packages"))


@app.route("/admin/packages/<int:pkg_id>/price", methods=["POST"])
@role_required("admin")
def admin_edit_package_price(pkg_id):
    conn = get_connection()
    conn.execute("UPDATE packages SET price = ? WHERE id = ?", (float(request.form["price"]), pkg_id))
    conn.commit()
    conn.close()
    flash("Package price updated.")
    return redirect(url_for("admin_packages"))


@app.route("/admin/deliveries")
@role_required("admin")
def admin_deliveries():
    conn = get_connection()
    deliveries = conn.execute("""
        SELECT del.*, o.delivery_date, o.status as order_status, u.full_name as patient_name,
               r.full_name as rider_name
        FROM deliveries del
        JOIN orders o ON o.id = del.order_id
        JOIN users u ON u.id = o.patient_id
        LEFT JOIN users r ON r.id = del.rider_id
        ORDER BY o.delivery_date DESC
    """).fetchall()
    conn.close()
    return render_template("admin_deliveries.html", deliveries=deliveries)


if __name__ == "__main__":
    import os
    if not os.path.exists("beacon.db"):
        init_db()
    app.run(debug=True)
