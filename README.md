# Beacon of Hope — Unified Telemedicine + Pharmacy + Delivery Platform

## Setup (VS Code)

```bash
cd beacon_v2
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python database.py            # creates beacon.db, seeds diseases + 6 demo doctors + admin
python app.py
```

Open **http://127.0.0.1:5000**

**Default admin login:** `admin` / `ChangeMe123!` — change this immediately after first login (create yourself a fresh admin account via `/admin/users`, then you can stop using the default one).

**Demo doctor logins** (seeded so you can test telemedicine booking right away — password for all: `Doctor123!`):
| Username | Doctor | Specialty |
|---|---|---|
| dr.mwangi | Dr. James Mwangi | Hypertension |
| dr.achieng | Dr. Faith Achieng | Diabetes Mellitus |
| dr.otieno | Dr. Brian Otieno | HIV |
| dr.wanjiru | Dr. Grace Wanjiru | TB |
| dr.kamau | Dr. Susan Kamau | Mental Health |
| dr.njoroge | Dr. Peter Njoroge | Mental Health |

## How the telemedicine booking works (the part you asked about)

This was built specifically so patients can pick a **specific doctor**, not just request "an appointment":

1. **Doctor sets availability** — log in as a doctor (e.g. `dr.kamau`) → Clinician Dashboard → "Set Availability" → add a date + time. Each slot is unique per doctor.
2. **Patient browses doctors** — log in as a patient → "Book a Doctor" in the nav → filter by specialty (an NCD: Hypertension / Diabetes / HIV / TB, or Mental Health) → click a doctor to see their bio and open slots.
3. **Patient books a specific slot** — choosing a slot immediately creates a **Confirmed** appointment tied to that exact doctor, marks the slot as booked, and shows up on both the patient's and the doctor's dashboards.
4. **Doctor adds a consult link** — from their dashboard, the doctor pastes in a video link (Zoom/Jitsi/Google Meet/Twilio — whatever you use) which then appears as "Join Call" on the patient's dashboard.
5. **Fallback** — if a patient doesn't want to pick themselves, "Ask Beacon of Hope to Book For Me" on the doctor directory page sends a request to the admin dashboard, where staff assign it to an open slot on the patient's behalf.

I tested this exact flow end-to-end (signup → doctor adds slot → patient books it → doctor sees it on their dashboard → doctor adds a consult link) before handing this over, so it works as described.

## What's also included (from the fuller platform spec)

Since this grew out of the earlier medication-delivery dashboard, the following are wired in and functional, in case you want them:
- **Prescriptions**: clinician-only, tied to a patient's condition.
- **Pharmacy workflow**: order → verify → confirm availability → dispense → bill → payment → assign rider → collect → deliver, with delivery **restricted to Monday/Wednesday/Saturday** and comorbidity consolidation (e.g. HIV + TB merged into one delivery trip) via `scheduling.py`.
- **Billing**: SHA, M-Pesa STK Push, and Mastercard (via a card gateway) — all in `billing.py`, running in simulation mode until you add real credentials.
- **Lab requests** (including home sample collection) and **home nursing visits**.
- **Rider interface** with deliberately restricted visibility (no medication names, prices, or diagnoses — just what's needed to deliver).
- **Admin**: staff account creation, packages, inventory oversight, delivery monitoring.
- **FAQ / patient education** page covering all 4 conditions plus platform features.

If you only want the telemedicine booking piece for now, everything else can simply go unused — none of it is required for `/doctors` → booking → consult link to work.

## Where you'll actually edit things

- **Add more doctors**: log in as admin → Staff page → create a `clinician` account with a specialty and short bio.
- **Change refill intervals / delivery day rules**: `database.py` (disease refill days) and `scheduling.py` (`ALLOWED_DELIVERY_WEEKDAYS`, `GRACE_WINDOW_DAYS`).
- **Real payment credentials**: `billing.py` — fill in the M-Pesa Daraja, card gateway, and SHA config blocks, then set `SIMULATE = False`.
- **`app.secret_key`** in `app.py` — change before deploying anywhere real.

You do not need to edit Python to add patients, doctors' slots, prescriptions, or anything day-to-day — it's all through the web forms once the app is running.
