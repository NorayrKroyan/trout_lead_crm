from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from .db import get_settings, add_log
from .scraper import scrape_new_leads
from .mailer import send_batch

_scheduler = None


def daily_job():
    settings = get_settings()
    countries = [x.strip() for x in settings.get("selected_countries", "").split(",") if x.strip()]
    if settings.get("auto_scrape") == "1":
        try:
            scrape_new_leads(
                countries=countries,
                target=int(settings.get("daily_target", "50")),
                min_score=int(settings.get("min_score", "55")),
            )
        except Exception as exc:
            add_log("scheduled_scrape_error", str(exc))
    if settings.get("auto_send") == "1":
        try:
            send_batch(limit=int(settings.get("daily_send_cap", "50")), sleep_between=True)
        except Exception as exc:
            add_log("scheduled_send_error", str(exc))


def start_scheduler():
    global _scheduler
    settings = get_settings()
    hour, minute = [int(x) for x in settings.get("schedule_time", "07:00").split(":")]
    if _scheduler:
        _scheduler.shutdown(wait=False)
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(daily_job, "cron", hour=hour, minute=minute, id="daily_outreach", replace_existing=True)
    _scheduler.start()
    add_log("scheduler_started", f"Daily job scheduled at {hour:02d}:{minute:02d} local server time")
    return _scheduler


def reschedule():
    return start_scheduler()
