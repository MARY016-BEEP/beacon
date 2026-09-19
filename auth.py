"""
auth.py
-------
Handles patient self-signup/login and staff login, plus a
role-required decorator used throughout app.py for RBAC.

Passwords are never stored in plain text - werkzeug's
generate_password_hash / check_password_hash (PBKDF2) is used.
"""

from functools import wraps
from flask import session, redirect, url_for, flash, request
from werkzeug.security import generate_password_hash, check_password_hash
from database import get_connection


def create_user(username, password, role, full_name, phone=None, area=None):
    conn = get_connection()
    cur = conn.cursor()
    existing = cur.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing:
        conn.close()
        return None, "Username already taken."

    pw_hash = generate_password_hash(password)
    cur.execute("""
        INSERT INTO users (username, password_hash, role, full_name, phone, area)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (username, pw_hash, role, full_name, phone, area))
    user_id = cur.lastrowid
    conn.commit()
    conn.close()
    return user_id, None


def verify_login(username, password):
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if user and check_password_hash(user["password_hash"], password):
        return dict(user)
    return None


def current_user():
    if "user_id" not in session:
        return None
    conn = get_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    conn.close()
    return dict(user) if user else None


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    """Usage: @role_required('clinician', 'admin')"""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in first.")
                return redirect(url_for("login", next=request.path))
            if session.get("role") not in roles:
                flash("You do not have permission to access that page.")
                return redirect(url_for("home"))
            return f(*args, **kwargs)
        return wrapper
    return decorator
