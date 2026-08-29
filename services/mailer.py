import random
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr
from datetime import datetime

from .settings_store import load_env_file
from .db import (
    get_template,
    update_lead,
    add_log,
    eligible_to_send,
    get_settings,
    get_leads_by_ids,
)


def render_quote(lead):
    cfg = load_env_file()
    template = get_template()
    values = {
        "greeting": "Purchasing Team",
        "company_name": lead["company_name"],
        "evidence": lead["evidence"] or "fish and seafood distribution",
        "monthly_capacity": cfg.get("MONTHLY_CAPACITY", "Available on request"),
        "moq": cfg.get("MOQ", "Available on request"),
        "price": cfg.get("PRICE", "Available on request"),
        "incoterm": cfg.get("INCOTERM", "FCA Armenia / CIF on request"),
        "sender_name": cfg.get("FROM_NAME", "Export Team"),
        "company_sender": cfg.get("SENDER_COMPANY", "Your Company"),
        "phone": cfg.get("SENDER_PHONE", ""),
        "website_sender": cfg.get("SENDER_WEBSITE", ""),
    }
    try:
        subject = template["subject"].format(**values)
        body = template["body"].format(**values)
    except KeyError as exc:
        raise RuntimeError(f"Unknown template placeholder: {exc}")
    return subject, body


def _email_config():
    cfg = load_env_file()
    provider = (cfg.get("EMAIL_PROVIDER") or "gmail").strip().lower()

    if provider == "custom":
        host = cfg.get("SMTP_HOST", "").strip()
        port = int(cfg.get("SMTP_PORT") or 465)
        security = (cfg.get("SMTP_SECURITY") or "ssl").strip().lower()
        username = cfg.get("SMTP_USERNAME", "").strip()
        password = cfg.get("SMTP_PASSWORD", "").strip()
        sender = cfg.get("SENDER_EMAIL", "").strip() or username
        if not all([host, username, password, sender]):
            raise RuntimeError("Complete the Custom SMTP fields in Settings first.")
        if security not in {"ssl", "starttls"}:
            raise RuntimeError("SMTP security must be SSL or STARTTLS.")
        return {
            "provider": "custom",
            "host": host,
            "port": port,
            "security": security,
            "username": username,
            "password": password,
            "sender": sender,
        }

    address = cfg.get("GMAIL_ADDRESS", "").strip()
    password = cfg.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        raise RuntimeError("Configure Gmail address and Google App Password in Settings first.")
    return {
        "provider": "gmail",
        "host": "smtp.gmail.com",
        "port": 465,
        "security": "ssl",
        "username": address,
        "password": password,
        "sender": address,
    }


def email_configured():
    try:
        _email_config()
        return True
    except Exception:
        return False


def gmail_configured():
    return email_configured()


def _smtp_login(config):
    context = ssl.create_default_context()
    if config["security"] == "starttls":
        smtp = smtplib.SMTP(config["host"], config["port"], timeout=30)
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
    else:
        smtp = smtplib.SMTP_SSL(config["host"], config["port"], context=context, timeout=30)
    smtp.login(config["username"], config["password"])
    return smtp


def _close_smtp(smtp):
    if smtp is None:
        return
    try:
        smtp.quit()
    except Exception:
        try:
            smtp.close()
        except Exception:
            pass


def test_email_connection():
    config = _email_config()
    smtp = _smtp_login(config)
    _close_smtp(smtp)
    return True


def test_gmail_connection():
    return test_email_connection()


def _build_message(lead, config, cfg=None):
    cfg = cfg or load_env_file()
    subject, body = render_quote(lead)
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("FROM_NAME", "Export Team"), config["sender"]))
    msg["To"] = lead["email"]
    msg["Subject"] = subject
    reply_to = cfg.get("REPLY_TO", "").strip()
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    return msg, subject, body


def _mark_sent(lead, config):
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    update_lead(lead["id"], status="sent", sent_at=now, last_error="")
    add_log("email_sent", f"Sent quote to {lead['email']} via {config['provider']}", lead["id"])


def _send_via_connection(smtp, lead, config, cfg=None):
    msg, subject, body = _build_message(lead, config, cfg)
    smtp.send_message(msg)
    _mark_sent(lead, config)
    return subject, body


def send_one(lead, dry_run=False):
    if lead["opted_out"]:
        raise RuntimeError("Lead is suppressed/opted out.")

    cfg = load_env_file()
    config = _email_config()
    msg, subject, body = _build_message(lead, config, cfg)
    if dry_run:
        return subject, body

    smtp = None
    try:
        smtp = _smtp_login(config)
        smtp.send_message(msg)
        _mark_sent(lead, config)
    finally:
        _close_smtp(smtp)
    return subject, body


def _cancelled(cancel_event):
    return bool(cancel_event and cancel_event.is_set())


def _interruptible_delay(seconds, cancel_event=None):
    """Sleep in short slices so a background job can be cancelled quickly."""
    end = time.monotonic() + max(0.0, float(seconds))
    while time.monotonic() < end:
        if _cancelled(cancel_event):
            return False
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))
    return not _cancelled(cancel_event)


def _send_many(leads, requested, sleep_between=True, cancel_event=None, progress=None):
    settings = get_settings()
    delay_min = max(0, int(settings.get("send_delay_min", "5")))
    delay_max = max(0, int(settings.get("send_delay_max", "10")))
    if delay_max < delay_min:
        delay_max = delay_min

    sent = 0
    failed = 0
    processed = 0
    cancelled = False
    total = len(leads)
    if total == 0:
        return {
            "sent": 0, "failed": 0, "skipped": max(0, requested),
            "attempted": 0, "eligible": 0, "remaining": 0, "cancelled": False,
        }

    cfg = load_env_file()
    config = _email_config()
    smtp = None

    def report(message, current=None):
        if progress:
            progress(
                message,
                current=processed if current is None else current,
                total=total,
                sent=sent,
                failed=failed,
            )

    try:
        if total:
            report(f"Connecting once to {config['provider']} SMTP…", current=0)
            smtp = _smtp_login(config)

        for idx, lead in enumerate(leads, start=1):
            if _cancelled(cancel_event):
                cancelled = True
                break

            report(f"Sending {idx}/{total} to {lead['email']}…", current=processed)
            try:
                try:
                    _send_via_connection(smtp, lead, config, cfg)
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
                    # Reconnect once if the provider closed an idle/long-lived session.
                    _close_smtp(smtp)
                    smtp = _smtp_login(config)
                    _send_via_connection(smtp, lead, config, cfg)
                sent += 1
            except Exception as exc:
                failed += 1
                update_lead(lead["id"], status="failed", last_error=str(exc))
                add_log("email_failed", str(exc), lead["id"])

            processed += 1
            report(
                f"Progress {processed}/{total} · {sent} sent · {failed} failed",
                current=processed,
            )

            if sleep_between and idx < total:
                delay = random.randint(delay_min, delay_max) if delay_max > delay_min else delay_min
                if delay > 0:
                    report(
                        f"Progress {processed}/{total} · waiting {delay}s before next email · Cancel is available",
                        current=processed,
                    )
                    if not _interruptible_delay(delay, cancel_event):
                        cancelled = True
                        break

        if _cancelled(cancel_event):
            cancelled = True
    finally:
        _close_smtp(smtp)

    skipped = max(0, requested - total)
    remaining = max(0, total - processed)
    return {
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
        "attempted": processed,
        "eligible": total,
        "remaining": remaining,
        "cancelled": cancelled,
    }


def send_batch(limit=None, sleep_between=True, cancel_event=None, progress=None):
    settings = get_settings()
    cap = int(limit or settings.get("daily_send_cap", "50"))
    leads = eligible_to_send(cap)
    return _send_many(
        leads,
        requested=len(leads),
        sleep_between=sleep_between,
        cancel_event=cancel_event,
        progress=progress,
    )


def send_selected(lead_ids, sleep_between=True, cancel_event=None, progress=None):
    """Send selected IDs using one SMTP session; sent/suppressed/unapproved leads are skipped."""
    leads = get_leads_by_ids(lead_ids, sendable_only=True)
    requested = len({int(x) for x in lead_ids if str(x).isdigit()})
    return _send_many(
        leads,
        requested=requested,
        sleep_between=sleep_between,
        cancel_event=cancel_event,
        progress=progress,
    )
