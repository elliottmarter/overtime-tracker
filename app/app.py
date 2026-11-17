import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for

import db

TZ = ZoneInfo(os.environ.get("TZ") or "UTC")
APP_USERNAME = os.environ.get("APP_USERNAME", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if not (APP_USERNAME and APP_PASSWORD):
    raise SystemExit("APP_USERNAME and APP_PASSWORD must both be set. Refusing to start.")

app = Flask(__name__)
app.secret_key = db.init_db()
app.teardown_appcontext(db.close_db)
# Stay signed in on this browser for a month. Lax keeps other websites from using the session.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Too many wrong passwords in a row pauses sign-in for a minute.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 60
_login_failures = []
_login_lock = threading.Lock()

# Expected pay for an entry, in pence. Rounded half-up to the penny like a payroll would.
ENTRY_PENCE = "CAST(ROUND(e.hours * e.rate + 0.0000001) AS INTEGER)"


def now():
    return datetime.now(TZ)


def today():
    return now().date().isoformat()


def safe_next(default):
    target = request.form.get("next") or request.args.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return default


# --- parsing -------------------------------------------------------------------

class FormError(ValueError):
    pass


def parse_date(value, label):
    try:
        return date.fromisoformat((value or "").strip()).isoformat()
    except ValueError:
        raise FormError(f"{label}: enter a date.") from None


def parse_month(value):
    """'2026-10' or blank."""
    value = (value or "").strip()
    if not value:
        return None
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise FormError("Overtime month: pick a month.")
    return value


def previous_month():
    first = now().date().replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def parse_hours(value, label="Hours", required=True):
    """Accepts 2, 2.5 or 2:30."""
    value = (value or "").strip()
    if not value:
        if required:
            raise FormError(f"{label}: enter the number of hours.")
        return None
    m = re.fullmatch(r"(\d+):([0-5]\d)", value)
    try:
        hours = int(m[1]) + int(m[2]) / 60 if m else float(Decimal(value))
    except (InvalidOperation, ValueError):
        raise FormError(f"{label}: “{value}” isn't a number of hours.") from None
    if hours < 0 or hours > 1000 or (required and hours == 0):
        raise FormError(f"{label}: “{value}” is out of range.")
    return round(hours, 4)


def parse_money(value, label, required=True):
    """Pounds as typed (£1,234.56) to whole pence."""
    value = (value or "").strip().replace("£", "").replace(",", "")
    if not value:
        if required:
            raise FormError(f"{label}: enter an amount.")
        return None
    try:
        pounds = Decimal(value)
    except InvalidOperation:
        raise FormError(f"{label}: “{value}” isn't an amount.") from None
    if pounds < 0 or pounds > 1_000_000:
        raise FormError(f"{label}: “{value}” is out of range.")
    return int((pounds * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def current_rate():
    return int(db.get_setting("rate", db.DEFAULT_RATE_PENCE))


# --- sign-in -------------------------------------------------------------------

def login_token():
    """What a signed-in session carries. Changing the username or password signs everyone out."""
    creds = f"{APP_USERNAME}\0{APP_PASSWORD}".encode()
    return hmac.new(app.secret_key.encode(), creds, hashlib.sha256).hexdigest()


def signed_in():
    return secrets.compare_digest(session.get("auth", ""), login_token())


def login_locked():
    cutoff = time.monotonic() - LOGIN_LOCK_SECONDS
    with _login_lock:
        _login_failures[:] = [t for t in _login_failures if t > cutoff]
        return len(_login_failures) >= LOGIN_MAX_FAILURES


@app.before_request
def same_origin_posts():
    # Refuse form posts that another website triggers.
    if request.method != "POST":
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and origin != "null" and urlsplit(origin).netloc != request.host:
        abort(403)
    return None


@app.before_request
def require_auth():
    if request.endpoint in ("healthz", "static", "login", "login_submit") or signed_in():
        return None
    target = request.full_path.rstrip("?") if request.method == "GET" else "/"
    return redirect(url_for("login", next=target if target != "/" else None))


@app.get("/login")
def login():
    if signed_in():
        return redirect(safe_next(url_for("overview")))
    return render_template("login.html", next=safe_next(""))


@app.post("/login")
def login_submit():
    if login_locked():
        flash("Too many wrong attempts. Wait a minute and try again.", "error")
        return redirect(url_for("login", next=safe_next("") or None))
    user_ok = secrets.compare_digest(request.form.get("username", "").encode(), APP_USERNAME.encode())
    pass_ok = secrets.compare_digest(request.form.get("password", "").encode(), APP_PASSWORD.encode())
    if not (user_ok and pass_ok):
        with _login_lock:
            _login_failures.append(time.monotonic())
        flash("Wrong username or password.", "error")
        return redirect(url_for("login", next=safe_next("") or None))
    session.clear()
    session["auth"] = login_token()
    session.permanent = True
    return redirect(safe_next(url_for("overview")))


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- template helpers ----------------------------------------------------------

@app.context_processor
def inject_globals():
    return {"signed_in": signed_in(), "today": today()}


@app.template_filter("gbp")
def gbp(pence, signed=False):
    if pence is None:
        return "—"
    sign = "−" if pence < 0 else ("+" if signed and pence > 0 else "")
    return f"{sign}£{abs(pence) / 100:,.2f}"


@app.template_filter("hrs")
def hrs(hours):
    if hours is None:
        return "—"
    text = f"{hours:,.2f}".rstrip("0").rstrip(".")
    return f"{text} h"


@app.template_filter("nicedate")
def nicedate(value):
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        return "—"
    return f"{d:%a} {d.day} {d:%b %Y}"


@app.template_filter("month")
def month_label(value):
    try:
        d = date.fromisoformat(value + "-01")
    except (TypeError, ValueError):
        return value
    return f"{d:%B %Y}"


# --- queries -------------------------------------------------------------------

def payslip_rows(conn, where="", args=()):
    """Payslips with the overtime linked to them and how the payment compares."""
    rows = conn.execute(
        f"""
        SELECT p.*,
               COUNT(e.id) AS entry_count,
               COALESCE(SUM(e.hours), 0) AS claimed_hours,
               COALESCE(SUM({ENTRY_PENCE}), 0) AS claimed_pence
        FROM payslips p LEFT JOIN entries e ON e.payslip_id = p.id
        {where}
        GROUP BY p.id
        ORDER BY p.pay_date DESC, p.id DESC
        """,
        args,
    ).fetchall()
    return [reconcile(r) for r in rows]


def reconcile(row):
    p = dict(row)
    p["diff"] = p["amount_paid"] - p["claimed_pence"]
    p["hours_diff"] = None if p["hours_paid"] is None else round(p["hours_paid"] - p["claimed_hours"], 4)
    if p["entry_count"] == 0:
        p["status"], p["status_label"] = "none", "No overtime linked"
    elif abs(p["diff"]) < 1:
        p["status"], p["status_label"] = "ok", "Paid correctly"
    elif p["diff"] < 0:
        p["status"], p["status_label"] = "under", f"Underpaid {gbp(-p['diff'])}"
    else:
        p["status"], p["status_label"] = "over", f"Overpaid {gbp(p['diff'])}"
    return p


def get_payslip(conn, payslip_id):
    rows = payslip_rows(conn, "WHERE p.id = ?", (payslip_id,))
    if not rows:
        abort(404)
    return rows[0]


def entry_rows(conn, where="", args=(), order="e.work_date DESC, e.id DESC"):
    return conn.execute(
        f"""
        SELECT e.*, {ENTRY_PENCE} AS pence, p.pay_date AS paid_on
        FROM entries e LEFT JOIN payslips p ON p.id = e.payslip_id
        {where}
        ORDER BY {order}
        """,
        args,
    ).fetchall()


def totals(conn):
    worked = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(e.hours), 0) AS hours, COALESCE(SUM({ENTRY_PENCE}), 0) AS pence "
        "FROM entries e"
    ).fetchone()
    awaiting = conn.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(e.hours), 0) AS hours, COALESCE(SUM({ENTRY_PENCE}), 0) AS pence "
        "FROM entries e WHERE e.payslip_id IS NULL"
    ).fetchone()
    paid = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(hours_paid), 0) AS hours, COALESCE(SUM(amount_paid), 0) AS pence "
        "FROM payslips"
    ).fetchone()
    slips = payslip_rows(conn)
    shortfall = sum(-p["diff"] for p in slips if p["entry_count"] or p["amount_paid"])
    return {
        "worked": worked,
        "awaiting": awaiting,
        "paid": paid,
        # Positive: you're owed money on payslips already received. Negative: overpaid.
        "shortfall": shortfall,
        # Everything worked minus everything paid.
        "outstanding": worked["pence"] - paid["pence"],
        "problems": [p for p in slips if p["status"] in ("under", "over")],
        "recent": slips[:6],
    }


# --- pages ---------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    db.get_db().execute("SELECT 1")
    return "ok"


@app.get("/")
def overview():
    conn = db.get_db()
    months = conn.execute(
        f"""
        SELECT substr(e.work_date, 1, 7) AS month, COUNT(*) AS n, SUM(e.hours) AS hours,
               SUM({ENTRY_PENCE}) AS pence,
               SUM(CASE WHEN e.payslip_id IS NULL THEN e.hours ELSE 0 END) AS awaiting_hours
        FROM entries e GROUP BY month ORDER BY month DESC LIMIT 12
        """
    ).fetchall()
    slips_by_month = {}
    for p in payslip_rows(conn, "WHERE p.covers_month IS NOT NULL"):
        slips_by_month.setdefault(p["covers_month"], []).append(p)
    return render_template(
        "overview.html", t=totals(conn), months=months, slips_by_month=slips_by_month, rate=current_rate()
    )


@app.get("/overtime")
def entries_page():
    conn = db.get_db()
    show = request.args.get("show", "all")
    clauses, args = [], []
    if show == "awaiting":
        clauses.append("e.payslip_id IS NULL")
    elif show == "paid":
        clauses.append("e.payslip_id IS NOT NULL")
    month = request.args.get("month", "")
    if re.fullmatch(r"\d{4}-\d{2}", month):
        clauses.append("substr(e.work_date, 1, 7) = ?")
        args.append(month)
    rows = entry_rows(conn, "WHERE " + " AND ".join(clauses) if clauses else "", args)
    return render_template(
        "entries.html",
        entries=rows,
        show=show,
        rate=current_rate(),
        total_hours=sum(r["hours"] for r in rows),
        total_pence=sum(r["pence"] for r in rows),
        form=request.args,
    )


def read_entry_form():
    f = request.form
    return {
        "work_date": parse_date(f.get("work_date"), "Date"),
        "hours": parse_hours(f.get("hours")),
        "rate": parse_money(f.get("rate"), "Rate"),
        "ticket": (f.get("ticket") or "").strip()[:100],
        "note": (f.get("note") or "").strip()[:500],
    }


@app.post("/overtime")
def entry_add():
    try:
        e = read_entry_form()
    except FormError as err:
        flash(str(err), "error")
        return redirect(url_for("entries_page"))
    conn = db.get_db()
    conn.execute(
        "INSERT INTO entries (work_date, hours, rate, ticket, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (e["work_date"], e["hours"], e["rate"], e["ticket"], e["note"], now().isoformat(timespec="seconds")),
    )
    conn.commit()
    flash(f"Logged {hrs(e['hours'])} on {nicedate(e['work_date'])} ({gbp(round(e['hours'] * e['rate']))}).", "success")
    return redirect(url_for("entries_page"))


@app.get("/overtime/<int:entry_id>")
def entry_edit(entry_id):
    rows = entry_rows(db.get_db(), "WHERE e.id = ?", (entry_id,))
    if not rows:
        abort(404)
    return render_template("entry.html", e=rows[0])


@app.post("/overtime/<int:entry_id>")
def entry_save(entry_id):
    conn = db.get_db()
    if request.form.get("action") == "delete":
        conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        conn.commit()
        flash("Entry deleted.", "info")
        return redirect(url_for("entries_page"))
    try:
        e = read_entry_form()
    except FormError as err:
        flash(str(err), "error")
        return redirect(url_for("entry_edit", entry_id=entry_id))
    conn.execute(
        "UPDATE entries SET work_date = ?, hours = ?, rate = ?, ticket = ?, note = ? WHERE id = ?",
        (e["work_date"], e["hours"], e["rate"], e["ticket"], e["note"], entry_id),
    )
    conn.commit()
    flash("Entry saved.", "success")
    return redirect(url_for("entries_page"))


@app.get("/payslips")
def payslips_page():
    conn = db.get_db()
    return render_template("payslips.html", payslips=payslip_rows(conn), t=totals(conn))


def payslip_form_entries(conn, payslip_id=None):
    """Overtime that can go on this payslip: anything not yet on another one."""
    return entry_rows(
        conn,
        "WHERE e.payslip_id IS NULL OR e.payslip_id = ?",
        (payslip_id or -1,),
        order="e.work_date ASC, e.id ASC",
    )


@app.get("/payslips/new")
def payslip_new():
    conn = db.get_db()
    return render_template(
        "payslip.html", p=None, entries=payslip_form_entries(conn), rate=current_rate(),
        default_month=request.args.get("month") or previous_month(),
    )


@app.get("/payslips/<int:payslip_id>")
def payslip_edit(payslip_id):
    conn = db.get_db()
    return render_template(
        "payslip.html",
        p=get_payslip(conn, payslip_id),
        entries=payslip_form_entries(conn, payslip_id),
        rate=current_rate(),
    )


def save_payslip(payslip_id=None):
    f = request.form
    conn = db.get_db()
    try:
        values = (
            parse_date(f.get("pay_date"), "Pay date"),
            parse_month(f.get("covers_month")),
            parse_hours(f.get("hours_paid"), "Overtime hours paid", required=False),
            parse_money(f.get("amount_paid"), "Overtime amount paid"),
            (f.get("note") or "").strip()[:500],
        )
    except FormError as err:
        flash(str(err), "error")
        return redirect(url_for("payslip_edit", payslip_id=payslip_id) if payslip_id else url_for("payslip_new"))
    if payslip_id is None:
        cur = conn.execute(
            "INSERT INTO payslips (pay_date, covers_month, hours_paid, amount_paid, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (*values, now().isoformat(timespec="seconds")),
        )
        payslip_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE payslips SET pay_date = ?, covers_month = ?, hours_paid = ?, amount_paid = ?, note = ? "
            "WHERE id = ?",
            (*values, payslip_id),
        )
    chosen = {int(x) for x in f.getlist("entry") if x.isdigit()}
    conn.execute("UPDATE entries SET payslip_id = NULL WHERE payslip_id = ?", (payslip_id,))
    if chosen:
        marks = ",".join("?" * len(chosen))
        conn.execute(
            f"UPDATE entries SET payslip_id = ? WHERE payslip_id IS NULL AND id IN ({marks})",
            (payslip_id, *chosen),
        )
    conn.commit()
    p = get_payslip(conn, payslip_id)
    flash(f"Payslip for {nicedate(p['pay_date'])} saved: {p['status_label'].lower()}.",
          "error" if p["status"] == "under" else "success")
    return redirect(url_for("payslip_edit", payslip_id=payslip_id))


@app.post("/payslips/new")
def payslip_create():
    return save_payslip()


@app.post("/payslips/<int:payslip_id>")
def payslip_save(payslip_id):
    conn = db.get_db()
    get_payslip(conn, payslip_id)
    if request.form.get("action") == "delete":
        conn.execute("DELETE FROM payslips WHERE id = ?", (payslip_id,))
        conn.commit()
        flash("Payslip deleted. Its overtime is back to awaiting payment.", "info")
        return redirect(url_for("payslips_page"))
    return save_payslip(payslip_id)


@app.get("/settings")
def settings_page():
    return render_template("settings.html", rate=current_rate())


@app.post("/settings")
def settings_save():
    try:
        rate = parse_money(request.form.get("rate"), "Hourly rate")
    except FormError as err:
        flash(str(err), "error")
        return redirect(url_for("settings_page"))
    if rate <= 0:
        flash("Hourly rate must be more than zero.", "error")
        return redirect(url_for("settings_page"))
    conn = db.get_db()
    db.set_setting("rate", rate)
    msg = f"Overtime rate set to {gbp(rate)} an hour for new entries."
    if request.form.get("apply_awaiting"):
        n = conn.execute("UPDATE entries SET rate = ? WHERE payslip_id IS NULL", (rate,)).rowcount
        msg += f" Updated {n} entr{'y' if n == 1 else 'ies'} awaiting payment."
    conn.commit()
    flash(msg, "success")
    return redirect(url_for("settings_page"))


def csv_response(name, header, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=overtime-{name}-{today()}.csv"},
    )


@app.get("/export/overtime.csv")
def export_entries():
    rows = entry_rows(db.get_db(), order="e.work_date ASC, e.id ASC")
    return csv_response(
        "entries",
        ["date", "hours", "rate", "expected", "ticket", "note", "paid_on_payslip_dated"],
        [[r["work_date"], r["hours"], f"{r['rate'] / 100:.2f}", f"{r['pence'] / 100:.2f}", r["ticket"], r["note"],
          r["paid_on"] or ""] for r in rows],
    )


@app.get("/export/payslips.csv")
def export_payslips():
    rows = sorted(payslip_rows(db.get_db()), key=lambda p: (p["pay_date"], p["id"]))
    return csv_response(
        "payslips",
        ["pay_date", "overtime_month", "hours_paid", "amount_paid", "hours_claimed", "amount_expected",
         "difference", "status", "note"],
        [[p["pay_date"], p["covers_month"] or "", "" if p["hours_paid"] is None else p["hours_paid"],
          f"{p['amount_paid'] / 100:.2f}", p["claimed_hours"], f"{p['claimed_pence'] / 100:.2f}",
          f"{p['diff'] / 100:.2f}", p["status_label"], p["note"]] for p in rows],
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
