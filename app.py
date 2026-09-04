import csv
import io
import math
import threading
from urllib.parse import urlencode

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response

from services.db import (
    init_db, dashboard_stats, fetch_leads, count_leads, countries_in_db, update_lead,
    delete_lead, get_lead, recent_logs, get_settings, set_setting, get_template,
    save_template, add_log, upsert_lead, bulk_action_ids, approve_matching_leads,
    matching_lead_ids, fetch_scrape_history, count_scrape_history,
    scrape_history_countries, scrape_history_statuses, scrape_history_sources,
    scrape_history_stats,
)
from services.settings_store import masked_settings, save_env
from services.scraper import scrape_new_leads, scrape_google_places_leads, domain_of
from services.mailer import send_one, send_batch, send_selected, test_email_connection, render_quote
from services.scheduler import start_scheduler, reschedule

app = Flask(__name__)
app.secret_key = "local-troutlead-crm-change-me"

# Additive DB migration only. Existing data stays intact.
init_db()

JOB_STATE = {
    "scrape_running": False,
    "scrape_message": "Idle",
    "scrape_current": 0,
    "scrape_total": 0,
    "scrape_inspected": 0,
    "scrape_skipped_seen": 0,
    "scrape_cancel_requested": False,
    "send_running": False,
    "send_message": "Idle",
    "send_current": 0,
    "send_total": 0,
    "send_sent": 0,
    "send_failed": 0,
    "send_cancel_requested": False,
}
JOB_LOCK = threading.Lock()
JOB_CANCEL = {
    "scrape": threading.Event(),
    "send": threading.Event(),
}


def _start_job(kind, message, total=0):
    with JOB_LOCK:
        if JOB_STATE[f"{kind}_running"]:
            return False
        JOB_CANCEL[kind].clear()
        JOB_STATE[f"{kind}_running"] = True
        JOB_STATE[f"{kind}_cancel_requested"] = False
        JOB_STATE[f"{kind}_message"] = message
        JOB_STATE[f"{kind}_current"] = 0
        JOB_STATE[f"{kind}_total"] = int(total or 0)
        if kind == "send":
            JOB_STATE["send_sent"] = 0
            JOB_STATE["send_failed"] = 0
        else:
            JOB_STATE["scrape_inspected"] = 0
            JOB_STATE["scrape_skipped_seen"] = 0
        return True


def _finish_job(kind, message):
    with JOB_LOCK:
        JOB_STATE[f"{kind}_running"] = False
        JOB_STATE[f"{kind}_cancel_requested"] = False
        JOB_STATE[f"{kind}_message"] = message


def bool_form(name):
    return "1" if request.form.get(name) == "on" else "0"


def _lead_filters_from_request(form=False):
    source = request.form if form else request.args
    return {
        "search": source.get("q", "").strip(),
        "status": source.get("status", "").strip(),
        "country": source.get("country", "").strip(),
        "approved": source.get("approved", "").strip(),
    }


def _return_to_leads():
    return_to = request.form.get("return_to", "").strip()
    if return_to.startswith("/leads"):
        return redirect(return_to)
    return redirect(url_for("leads"))


@app.route("/")
def dashboard():
    stats = dashboard_stats()
    leads = fetch_leads(limit=8)
    logs = recent_logs(8)
    settings = get_settings()
    return render_template("dashboard.html", stats=stats, leads=leads, logs=logs, settings=settings, job=JOB_STATE)


@app.route("/leads")
def leads():
    filters = _lead_filters_from_request()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", "50"))
    except ValueError:
        per_page = 50
    per_page = per_page if per_page in {25, 50, 100, 200} else 50

    total = count_leads(**filters)
    total_pages = max(1, math.ceil(total / per_page)) if total else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    rows = fetch_leads(**filters, limit=per_page, offset=offset)

    base_query = {
        "q": filters["search"],
        "status": filters["status"],
        "country": filters["country"],
        "approved": filters["approved"],
        "per_page": per_page,
    }

    def page_url(target_page):
        values = {**base_query, "page": target_page}
        return url_for("leads") + "?" + urlencode({k: v for k, v in values.items() if v != ""})

    return render_template(
        "leads.html",
        leads=rows,
        countries=countries_in_db(),
        q=filters["search"],
        status=filters["status"],
        country=filters["country"],
        approved=filters["approved"],
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        page_url=page_url,
    )


@app.route("/analyzed")
def analyzed_websites():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    country = request.args.get("country", "").strip()
    source = request.args.get("source", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", "50"))
    except ValueError:
        per_page = 50
    per_page = per_page if per_page in {25, 50, 100, 200} else 50

    total = count_scrape_history(search=search, status=status, country=country, source=source)
    total_pages = max(1, math.ceil(total / per_page)) if total else 1
    page = min(page, total_pages)
    offset = (page - 1) * per_page
    rows = fetch_scrape_history(
        search=search, status=status, country=country, source=source,
        limit=per_page, offset=offset,
    )

    base_query = {
        "q": search, "status": status, "country": country, "source": source,
        "per_page": per_page,
    }

    def page_url(target_page):
        values = {**base_query, "page": target_page}
        return url_for("analyzed_websites") + "?" + urlencode({k: v for k, v in values.items() if v != ""})

    export_query = urlencode({k: v for k, v in base_query.items() if v != "" and k != "per_page"})
    export_url = url_for("export_analyzed_csv") + (("?" + export_query) if export_query else "")

    return render_template(
        "analyzed.html", rows=rows, stats=scrape_history_stats(),
        countries=scrape_history_countries(), statuses=scrape_history_statuses(),
        sources=scrape_history_sources(), q=search, status=status, country=country,
        source=source, page=page, per_page=per_page, total=total,
        total_pages=total_pages, page_url=page_url, export_url=export_url,
    )


@app.get("/analyzed/export.csv")
def export_analyzed_csv():
    search = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    country = request.args.get("country", "").strip()
    source = request.args.get("source", "").strip()
    rows = fetch_scrape_history(search=search, status=status, country=country, source=source, limit=100000, offset=0)
    output = io.StringIO()
    writer = csv.writer(output)
    fields = [
        "domain", "company_name", "website", "country", "status", "score",
        "evidence", "reason", "emails", "email_count", "phone", "address",
        "pages_checked", "http_status", "discovery_source", "search_query",
        "google_place_id", "source_url", "search_title", "first_seen", "last_seen", "detail",
    ]
    writer.writerow(fields)
    for row in rows:
        writer.writerow([row[field] for field in fields])
    return Response(
        output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=trout_analyzed_websites.csv"},
    )


@app.post("/lead/<int:lead_id>/approve")
def approve_lead(lead_id):
    update_lead(lead_id, approved=1, status="approved")
    add_log("lead_approved", "Approved for outreach", lead_id)
    flash("Lead approved for outreach.", "success")
    return redirect(request.referrer or url_for("leads"))


@app.post("/lead/<int:lead_id>/unapprove")
def unapprove_lead(lead_id):
    lead = get_lead(lead_id)
    new_status = "sent" if lead and lead["status"] == "sent" else "new"
    update_lead(lead_id, approved=0, status=new_status)
    add_log("lead_unapproved", "Approval removed", lead_id)
    flash("Approval removed.", "info")
    return redirect(request.referrer or url_for("leads"))


@app.post("/lead/<int:lead_id>/suppress")
def suppress_lead(lead_id):
    update_lead(lead_id, opted_out=1, approved=0, status="suppressed")
    add_log("lead_suppressed", "Added to do-not-contact list", lead_id)
    flash("Lead added to suppression list.", "success")
    return redirect(request.referrer or url_for("leads"))


@app.post("/lead/<int:lead_id>/restore")
def restore_lead(lead_id):
    update_lead(lead_id, opted_out=0, status="new")
    add_log("lead_restored", "Removed from suppression list", lead_id)
    flash("Lead restored. It is not approved automatically.", "info")
    return redirect(request.referrer or url_for("leads"))


@app.post("/lead/<int:lead_id>/delete")
def remove_lead(lead_id):
    delete_lead(lead_id)
    flash("Lead deleted. Its domain remains in scrape history, so it will not be re-scraped.", "info")
    return redirect(request.referrer or url_for("leads"))


@app.post("/leads/bulk")
def bulk_leads():
    action = request.form.get("action", "").strip()
    lead_ids = request.form.getlist("lead_ids")
    if not lead_ids:
        flash("Select at least one lead first.", "warning")
        return _return_to_leads()

    if action in {"approve", "unapprove", "suppress"}:
        changed = bulk_action_ids(lead_ids, action)
        label = {"approve": "approved", "unapprove": "unapproved", "suppress": "suppressed"}[action]
        add_log(f"bulk_{action}", f"{changed} selected leads {label}")
        flash(f"{changed} selected leads {label}.", "success")
        return _return_to_leads()

    if action == "send":
        ids = [int(x) for x in lead_ids if str(x).isdigit()]
        if not _start_job("send", "Preparing selected quotes…", total=len(ids)):
            flash("A send job is already running.", "warning")
        else:
            threading.Thread(target=_send_selected_thread, args=(ids, "selected"), daemon=True).start()
            flash("Sending selected approved quotes in the background. You can cancel from Dashboard.", "success")
        return _return_to_leads()

    flash("Unknown bulk action.", "error")
    return _return_to_leads()


@app.post("/leads/approve-all")
def approve_all_leads():
    filters = _lead_filters_from_request(form=True)
    changed = approve_matching_leads(**filters)
    add_log("approve_all", f"Approved {changed} matching leads")
    flash(f"Approved {changed} matching leads across all pages.", "success")
    return _return_to_leads()


@app.post("/leads/send-all")
def send_all_quotes():
    filters = _lead_filters_from_request(form=True)
    ids = matching_lead_ids(**filters, sendable_only=True)
    if not ids:
        flash("No approved, non-suppressed leads match the current filters.", "warning")
        return _return_to_leads()
    if not _start_job("send", "Preparing all matching approved quotes…", total=len(ids)):
        flash("A send job is already running.", "warning")
        return _return_to_leads()
    threading.Thread(target=_send_selected_thread, args=(ids, "all matching approved"), daemon=True).start()
    flash(f"Sending quotes to {len(ids)} approved matching leads in the background. You can cancel from Dashboard.", "success")
    return _return_to_leads()


@app.route("/lead/<int:lead_id>/preview")
def preview_lead(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        flash("Lead not found.", "error")
        return redirect(url_for("leads"))
    try:
        subject, body = render_quote(lead)
    except Exception as exc:
        subject, body = "Template error", str(exc)
    return render_template("preview.html", lead=lead, subject=subject, body=body)


@app.post("/lead/<int:lead_id>/send")
def send_lead(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        flash("Lead not found.", "error")
        return redirect(url_for("leads"))
    if not lead["approved"]:
        flash("Approve the lead before sending.", "warning")
        return redirect(url_for("preview_lead", lead_id=lead_id))
    try:
        send_one(lead)
        flash(f"Quote sent to {lead['email']}.", "success")
    except Exception as exc:
        update_lead(lead_id, status=("sent" if lead["sent_at"] else "failed"), last_error=str(exc))
        add_log("email_failed", str(exc), lead_id)
        flash(f"Send failed: {exc}", "error")
    return redirect(url_for("leads"))


def _send_progress(message, current=0, total=0, sent=0, failed=0, **_):
    with JOB_LOCK:
        JOB_STATE["send_message"] = message
        JOB_STATE["send_current"] = int(current or 0)
        JOB_STATE["send_total"] = int(total or 0)
        JOB_STATE["send_sent"] = int(sent or 0)
        JOB_STATE["send_failed"] = int(failed or 0)


def _send_batch_thread():
    msg = "Send job finished."
    try:
        result = send_batch(cancel_event=JOB_CANCEL["send"], progress=_send_progress)
        if result.get("cancelled"):
            msg = (
                f"Cancelled: {result['sent']} sent, {result['failed']} failed; "
                f"{result.get('remaining', 0)} eligible emails left unsent."
            )
            add_log("batch_send_cancelled", msg)
        else:
            msg = f"Finished: {result['sent']} sent, {result['failed']} failed."
            add_log("batch_send_finished", msg)
    except Exception as exc:
        msg = f"Batch send failed: {exc}"
        add_log("batch_send_error", msg)
    finally:
        _finish_job("send", msg)


def _send_selected_thread(lead_ids, label):
    msg = "Send job finished."
    try:
        result = send_selected(
            lead_ids,
            cancel_event=JOB_CANCEL["send"],
            progress=_send_progress,
        )
        if result.get("cancelled"):
            msg = (
                f"Cancelled {label}: {result['sent']} sent, {result['failed']} failed, "
                f"{result['skipped']} skipped; {result.get('remaining', 0)} eligible left unsent."
            )
            add_log("bulk_send_cancelled", msg)
        else:
            msg = f"Finished {label}: {result['sent']} sent, {result['failed']} failed, {result['skipped']} skipped."
            add_log("bulk_send_finished", msg)
    except Exception as exc:
        msg = f"Bulk send failed: {exc}"
        add_log("bulk_send_error", msg)
    finally:
        _finish_job("send", msg)


@app.post("/send-approved")
def send_approved():
    settings = get_settings()
    total = int(settings.get("daily_send_cap", "50"))
    if not _start_job("send", "Preparing approved quotes…", total=total):
        flash("A send job is already running.", "warning")
    else:
        threading.Thread(target=_send_batch_thread, daemon=True).start()
        flash("Batch sending started in the background. You can monitor or cancel it from Dashboard.", "success")
    return redirect(url_for("dashboard"))


def _scrape_thread(countries, target, min_score, custom_keywords, provider="web"):
    msg = "Scrape job finished."
    try:
        def progress(message, current=0, total=0, inspected=0, skipped_seen=0, **_):
            with JOB_LOCK:
                JOB_STATE["scrape_message"] = message
                JOB_STATE["scrape_current"] = int(current or 0)
                JOB_STATE["scrape_total"] = int(total or target)
                JOB_STATE["scrape_inspected"] = int(inspected or 0)
                JOB_STATE["scrape_skipped_seen"] = int(skipped_seen or 0)

        scrape_func = scrape_google_places_leads if provider == "google_places" else scrape_new_leads
        result = scrape_func(
            countries,
            target,
            min_score,
            custom_keywords,
            progress,
            cancel_event=JOB_CANCEL["scrape"],
        )
        if result.get("cancelled"):
            msg = (
                f"Cancelled: {result['saved']} new companies / {result.get('emails', 0)} emails; "
                f"{result['inspected']} new domains inspected."
            )
        else:
            msg = (
                f"Finished: {result['saved']} new companies / {result.get('emails', 0)} emails; "
                f"{result['inspected']} new domains inspected; {result.get('skipped_seen', 0)} old domains skipped."
            )
    except Exception as exc:
        msg = f"Scrape failed: {exc}"
        add_log("scrape_error", msg)
    finally:
        _finish_job("scrape", msg)


@app.route("/scrape", methods=["GET", "POST"])
def scrape_page():
    settings = get_settings()
    all_countries = [
        "Netherlands", "Germany", "Poland", "Denmark", "France", "Belgium",
        "Spain", "Italy", "Sweden", "Czechia", "Lithuania", "Austria",
        "Romania", "Bulgaria", "Portugal", "Greece", "Finland", "Ireland",
        "Croatia", "Slovenia",
    ]
    selected = [x.strip() for x in settings.get("selected_countries", "").split(",") if x.strip()]
    if request.method == "POST":
        countries = request.form.getlist("countries")
        target = int(request.form.get("target", settings.get("daily_target", "50")))
        min_score = int(request.form.get("min_score", settings.get("min_score", "55")))
        custom_keywords = request.form.get("custom_keywords", "")
        provider = request.form.get("provider", "web").strip()
        if provider not in {"web", "google_places"}:
            provider = "web"
        if not countries:
            flash("Select at least one country.", "error")
            return redirect(url_for("scrape_page"))
        if not _start_job("scrape", "Starting search…", total=target):
            flash("A scrape job is already running.", "warning")
            return redirect(url_for("scrape_page"))
        threading.Thread(
            target=_scrape_thread,
            args=(countries, target, min_score, custom_keywords, provider),
            daemon=True,
        ).start()
        flash("Lead discovery started in the background. Previously inspected domains are skipped and the job can be cancelled.", "success")
        return redirect(url_for("scrape_page"))
    return render_template(
        "scrape.html",
        settings=settings,
        secrets=masked_settings(),
        all_countries=all_countries,
        selected=selected,
        job=JOB_STATE,
    )


@app.get("/api/job-status")
def job_status():
    with JOB_LOCK:
        return jsonify(dict(JOB_STATE))


@app.post("/jobs/cancel/<kind>")
def cancel_job(kind):
    if kind not in {"scrape", "send"}:
        return jsonify({"ok": False, "error": "Unknown job"}), 404
    with JOB_LOCK:
        if not JOB_STATE[f"{kind}_running"]:
            message = f"No {kind} job is currently running."
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"ok": False, "message": message})
            flash(message, "info")
            return redirect(request.referrer or url_for("dashboard"))
        JOB_CANCEL[kind].set()
        JOB_STATE[f"{kind}_cancel_requested"] = True
        JOB_STATE[f"{kind}_message"] = "Cancellation requested… finishing the current network operation."
    add_log(f"{kind}_cancel_requested", "User requested background job cancellation")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "message": "Cancellation requested"})
    flash(f"{kind.title()} cancellation requested.", "warning")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    settings = get_settings()
    secrets = masked_settings()
    if request.method == "POST":
        section = request.form.get("section")
        if section == "email":
            save_env({
                "EMAIL_PROVIDER": request.form.get("email_provider", "gmail"),
                "GMAIL_ADDRESS": request.form.get("gmail_address", ""),
                "GMAIL_APP_PASSWORD": request.form.get("gmail_app_password", ""),
                "SMTP_HOST": request.form.get("smtp_host", ""),
                "SMTP_PORT": request.form.get("smtp_port", "465"),
                "SMTP_SECURITY": request.form.get("smtp_security", "ssl"),
                "SMTP_USERNAME": request.form.get("smtp_username", ""),
                "SMTP_PASSWORD": request.form.get("smtp_password", ""),
                "SENDER_EMAIL": request.form.get("sender_email", ""),
                "FROM_NAME": request.form.get("from_name", ""),
                "REPLY_TO": request.form.get("reply_to", ""),
                "SENDER_COMPANY": request.form.get("sender_company", ""),
                "SENDER_PHONE": request.form.get("sender_phone", ""),
                "SENDER_WEBSITE": request.form.get("sender_website", ""),
                "MONTHLY_CAPACITY": request.form.get("monthly_capacity", ""),
                "MOQ": request.form.get("moq", ""),
                "PRICE": request.form.get("price", ""),
                "INCOTERM": request.form.get("incoterm", ""),
            })
            flash("Email sender and offer settings saved locally.", "success")
        elif section == "search":
            save_env({
                "SEARCH_PROVIDER": request.form.get("search_provider", "ddgs"),
                "BRAVE_API_KEY": request.form.get("brave_api_key", ""),
                "GOOGLE_PLACES_API_KEY": request.form.get("google_places_api_key", ""),
            })
            flash("Search settings saved. DDGS needs no API key.", "success")
        elif section == "automation":
            for key in ["daily_target", "daily_send_cap", "min_score", "schedule_time", "send_delay_min", "send_delay_max"]:
                set_setting(key, request.form.get(key, settings.get(key, "")))
            set_setting("auto_scrape", bool_form("auto_scrape"))
            set_setting("auto_send", bool_form("auto_send"))
            countries = request.form.getlist("countries")
            set_setting("selected_countries", ",".join(countries))
            try:
                reschedule()
            except Exception as exc:
                add_log("scheduler_error", str(exc))
            flash("Automation settings saved.", "success")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", settings=settings, secrets=secrets)


@app.post("/test-email")
@app.post("/test-gmail")
def test_email():
    try:
        test_email_connection()
        flash("Email login succeeded. SMTP is ready.", "success")
    except Exception as exc:
        flash(f"Email test failed: {exc}", "error")
    return redirect(url_for("settings_page"))


@app.route("/template", methods=["GET", "POST"])
def template_page():
    tpl = get_template()
    if request.method == "POST":
        save_template(request.form.get("subject", ""), request.form.get("body", ""))
        flash("Quote template saved.", "success")
        return redirect(url_for("template_page"))
    return render_template("template.html", template=tpl)


@app.route("/logs")
def logs_page():
    return render_template("logs.html", logs=recent_logs(500))


@app.get("/export.csv")
def export_csv():
    rows = fetch_leads(limit=100000)
    output = io.StringIO()
    writer = csv.writer(output)
    export_fields = [
        "company_name", "country", "website", "domain", "email", "email_type",
        "phone", "address", "score", "evidence", "status", "approved", "opted_out",
        "first_seen", "sent_at", "send_count", "source_url", "discovery_source",
        "google_place_id",
    ]
    writer.writerow(export_fields)
    for r in rows:
        writer.writerow([r[k] for k in export_fields])
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=trout_leads.csv"})


@app.post("/import.csv")
def import_csv():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file.", "error")
        return redirect(url_for("leads"))
    try:
        text = file.stream.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        count = 0
        for row in reader:
            website = (row.get("website") or row.get("url") or "").strip()
            email = (row.get("email") or "").strip().lower()
            if not email:
                continue
            domain = (row.get("domain") or (email.split("@", 1)[1] if "@" in email else domain_of(website))).strip().lower()
            if not domain:
                continue
            lead = {
                "company_name": (row.get("company_name") or row.get("company") or domain).strip(),
                "domain": domain,
                "website": website or f"https://{domain}",
                "country": (row.get("country") or "").strip(),
                "email": email,
                "email_type": (row.get("email_type") or "imported").strip(),
                "score": int(row.get("score") or 50),
                "evidence": (row.get("evidence") or "Imported lead").strip(),
                "source_url": (row.get("source_url") or website).strip(),
            }
            upsert_lead(lead)
            count += 1
        # init_db backfill also handles imported domains at next start; they are already skipped via leads now.
        add_log("csv_import", f"Imported/updated {count} rows")
        flash(f"Imported/updated {count} leads.", "success")
    except Exception as exc:
        flash(f"CSV import failed: {exc}", "error")
    return redirect(url_for("leads"))


if __name__ == "__main__":
    try:
        start_scheduler()
    except Exception as exc:
        add_log("scheduler_error", str(exc))
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
