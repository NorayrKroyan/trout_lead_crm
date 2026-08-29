import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "troutlead.db"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    website TEXT,
    country TEXT,
    email TEXT NOT NULL,
    email_type TEXT DEFAULT 'business',
    score INTEGER DEFAULT 0,
    evidence TEXT,
    source_url TEXT,
    status TEXT DEFAULT 'new',
    approved INTEGER DEFAULT 0,
    opted_out INTEGER DEFAULT 0,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    sent_at TEXT,
    last_error TEXT,
    UNIQUE(domain, email)
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score);
CREATE INDEX IF NOT EXISTS idx_leads_country ON leads(country);
CREATE INDEX IF NOT EXISTS idx_leads_sent_at ON leads(sent_at);

CREATE TABLE IF NOT EXISTS send_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER,
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(lead_id) REFERENCES leads(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scrape_history (
    domain TEXT PRIMARY KEY COLLATE NOCASE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    country TEXT,
    source_url TEXT,
    status TEXT NOT NULL DEFAULT 'seen',
    score INTEGER DEFAULT 0,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_scrape_history_status ON scrape_history(status);
CREATE INDEX IF NOT EXISTS idx_scrape_history_last_seen ON scrape_history(last_seen);
"""

DEFAULT_SETTINGS = {
    "daily_target": "50",
    "daily_send_cap": "50",
    "min_score": "55",
    "schedule_time": "07:00",
    "auto_scrape": "0",
    "auto_send": "0",
    "send_delay_min": "5",
    "send_delay_max": "10",
    "selected_countries": "Netherlands,Germany,Poland,Denmark,France,Belgium,Spain,Italy,Sweden,Czechia,Lithuania",
}

DEFAULT_SUBJECT = "Frozen Rainbow Trout from Armenia – Supply Offer"
DEFAULT_BODY = """Dear {greeting},

I am contacting you regarding a possible supply cooperation with {company_name}.

We offer frozen farmed Rainbow Trout (Oncorhynchus mykiss) originating from Armenia.

PRODUCT
Origin: Armenia
Species: Oncorhynchus mykiss
Product: Whole gutted / H&G frozen trout
Sizes: 300–500 g / 500–700 g / 700–1000 g
Packing: 10 kg carton
Quantity: {monthly_capacity}
MOQ: {moq}
Indicative price: {price}
Incoterm: {incoterm}

We found that your company is active in {evidence}, so we believe the product may fit your seafood portfolio.

We can provide product specifications, photographs, export/health documentation and samples on request.

Would you be interested in receiving our current specification and quotation?

Best regards,
{sender_name}
{company_sender}
{phone}
{website_sender}

If this commercial inquiry is not relevant, please reply REMOVE and we will suppress this address from future outreach.
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Additive migration only. Existing CRM data is never deleted or recreated."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",
                (key, value),
            )
        conn.execute(
            """INSERT OR IGNORE INTO templates(id,subject,body,updated_at)
               VALUES(1,?,?,?)""",
            (DEFAULT_SUBJECT, DEFAULT_BODY, utcnow()),
        )
        # Seed permanent scrape history from every company already in the CRM.
        # This makes the upgrade immediately avoid re-scraping old lead domains.
        conn.execute(
            """
            INSERT OR IGNORE INTO scrape_history(
                domain, first_seen, last_seen, country, source_url, status, score, detail
            )
            SELECT
                lower(domain),
                COALESCE(MIN(first_seen), ?),
                COALESCE(MAX(updated_at), ?),
                MAX(country),
                MAX(COALESCE(source_url, website)),
                'existing_lead',
                MAX(score),
                'Backfilled from existing leads during no-repeat migration'
            FROM leads
            WHERE domain IS NOT NULL AND trim(domain) <> ''
            GROUP BY lower(domain)
            """,
            (utcnow(), utcnow()),
        )


def get_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key,value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def add_log(event_type, detail="", lead_id=None):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO send_log(lead_id,event_type,detail,created_at) VALUES(?,?,?,?)",
            (lead_id, event_type, detail, utcnow()),
        )


def upsert_lead(lead):
    now = utcnow()
    domain = (lead["domain"] or "").strip().lower()
    email = (lead["email"] or "").strip().lower()
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO leads(
                company_name,domain,website,country,email,email_type,score,evidence,
                source_url,status,approved,opted_out,first_seen,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'new',0,0,?,?)
            ON CONFLICT(domain,email) DO UPDATE SET
                company_name=excluded.company_name,
                website=COALESCE(excluded.website, leads.website),
                country=COALESCE(excluded.country, leads.country),
                email_type=excluded.email_type,
                score=MAX(leads.score, excluded.score),
                evidence=COALESCE(excluded.evidence, leads.evidence),
                source_url=COALESCE(excluded.source_url, leads.source_url),
                updated_at=excluded.updated_at
            """,
            (
                lead["company_name"], domain, lead.get("website"),
                lead.get("country"), email, lead.get("email_type", "business"),
                int(lead.get("score", 0)), lead.get("evidence", ""),
                lead.get("source_url", ""), now, now,
            ),
        )
        row = conn.execute(
            "SELECT id FROM leads WHERE domain=? AND email=?",
            (domain, email),
        ).fetchone()
        return row["id"] if row else cur.lastrowid


def _lead_filter(search="", status="", country="", approved=""):
    clauses = ["1=1"]
    params = []
    if search:
        clauses.append("(company_name LIKE ? OR email LIKE ? OR domain LIKE ?)")
        q = f"%{search}%"
        params += [q, q, q]
    if status:
        clauses.append("status=?")
        params.append(status)
    if country:
        clauses.append("country=?")
        params.append(country)
    if approved in {"0", "1"}:
        clauses.append("approved=?")
        params.append(int(approved))
    return clauses, params


def fetch_leads(search="", status="", country="", approved="", limit=500, offset=0):
    clauses, params = _lead_filter(search, status, country, approved)
    sql = f"SELECT * FROM leads WHERE {' AND '.join(clauses)} ORDER BY score DESC, id DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(offset)]
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def count_leads(search="", status="", country="", approved=""):
    clauses, params = _lead_filter(search, status, country, approved)
    with get_conn() as conn:
        return conn.execute(
            f"SELECT COUNT(*) FROM leads WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()[0]


def matching_lead_ids(search="", status="", country="", approved="", sendable_only=False):
    clauses, params = _lead_filter(search, status, country, approved)
    if sendable_only:
        clauses += ["approved=1", "opted_out=0", "status IN ('new','approved','failed')"]
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT id FROM leads WHERE {' AND '.join(clauses)} ORDER BY score DESC, id ASC",
            params,
        ).fetchall()
    return [r["id"] for r in rows]


def get_lead(lead_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()


def get_leads_by_ids(lead_ids, sendable_only=False):
    ids = []
    seen = set()
    for raw in lead_ids:
        try:
            lead_id = int(raw)
        except (TypeError, ValueError):
            continue
        if lead_id > 0 and lead_id not in seen:
            ids.append(lead_id)
            seen.add(lead_id)
    if not ids:
        return []
    extra = " AND approved=1 AND opted_out=0 AND status IN ('new','approved','failed')" if sendable_only else ""
    rows = []
    # Chunk to stay below SQLite parameter limits even after the CRM grows large.
    with get_conn() as conn:
        for start in range(0, len(ids), 800):
            chunk = ids[start:start + 800]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(conn.execute(
                f"SELECT * FROM leads WHERE id IN ({placeholders}){extra}",
                chunk,
            ).fetchall())
    rows.sort(key=lambda r: (-int(r["score"] or 0), int(r["id"])))
    return rows


def update_lead(lead_id, **fields):
    allowed = {
        "company_name", "country", "email", "score", "evidence", "status",
        "approved", "opted_out", "sent_at", "last_error", "website", "source_url",
        "email_type"
    }
    pairs = [(k, v) for k, v in fields.items() if k in allowed]
    if not pairs:
        return
    pairs.append(("updated_at", utcnow()))
    set_sql = ",".join(f"{k}=?" for k, _ in pairs)
    values = [v for _, v in pairs] + [lead_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE leads SET {set_sql} WHERE id=?", values)


def bulk_action_ids(lead_ids, action):
    ids = []
    seen = set()
    for raw in lead_ids:
        try:
            lead_id = int(raw)
        except (TypeError, ValueError):
            continue
        if lead_id > 0 and lead_id not in seen:
            ids.append(lead_id)
            seen.add(lead_id)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    now = utcnow()
    with get_conn() as conn:
        if action == "approve":
            cur = conn.execute(
                f"""UPDATE leads SET approved=1,
                    status=CASE WHEN status='sent' THEN status ELSE 'approved' END,
                    updated_at=?
                    WHERE id IN ({placeholders}) AND opted_out=0""",
                [now] + ids,
            )
        elif action == "unapprove":
            cur = conn.execute(
                f"""UPDATE leads SET approved=0,
                    status=CASE WHEN status='sent' THEN status ELSE 'new' END,
                    updated_at=?
                    WHERE id IN ({placeholders}) AND opted_out=0""",
                [now] + ids,
            )
        elif action == "suppress":
            cur = conn.execute(
                f"UPDATE leads SET opted_out=1,approved=0,status='suppressed',updated_at=? WHERE id IN ({placeholders})",
                [now] + ids,
            )
        else:
            return 0
        return cur.rowcount


def approve_matching_leads(search="", status="", country="", approved=""):
    clauses, params = _lead_filter(search, status, country, approved)
    clauses += ["opted_out=0"]
    now = utcnow()
    with get_conn() as conn:
        cur = conn.execute(
            f"""UPDATE leads SET approved=1,
                status=CASE WHEN status='sent' THEN status ELSE 'approved' END,
                updated_at=?
                WHERE {' AND '.join(clauses)}""",
            [now] + params,
        )
        return cur.rowcount


def delete_lead(lead_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM leads WHERE id=?", (lead_id,))


def dashboard_stats():
    today = datetime.now().date().isoformat()
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        new = conn.execute("SELECT COUNT(*) FROM leads WHERE status='new'").fetchone()[0]
        approved = conn.execute("SELECT COUNT(*) FROM leads WHERE approved=1 AND opted_out=0").fetchone()[0]
        sent = conn.execute("SELECT COUNT(*) FROM leads WHERE status='sent'").fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM leads WHERE status='failed'").fetchone()[0]
        opted = conn.execute("SELECT COUNT(*) FROM leads WHERE opted_out=1").fetchone()[0]
        sent_today = conn.execute("SELECT COUNT(*) FROM leads WHERE sent_at LIKE ?", (f"{today}%",)).fetchone()[0]
    return {
        "total": total, "new": new, "approved": approved,
        "sent": sent, "failed": failed, "opted_out": opted,
        "sent_today": sent_today,
    }


def countries_in_db():
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT country FROM leads WHERE country IS NOT NULL AND country<>'' ORDER BY country").fetchall()
    return [r[0] for r in rows]


def recent_logs(limit=100):
    with get_conn() as conn:
        return conn.execute(
            """SELECT l.*, leads.company_name, leads.email
               FROM send_log l LEFT JOIN leads ON leads.id=l.lead_id
               ORDER BY l.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def get_template():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM templates WHERE id=1").fetchone()


def save_template(subject, body):
    with get_conn() as conn:
        conn.execute(
            "UPDATE templates SET subject=?,body=?,updated_at=? WHERE id=1",
            (subject, body, utcnow()),
        )


def eligible_to_send(limit):
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT * FROM leads
            WHERE approved=1 AND opted_out=0 AND status IN ('new','approved','failed')
            ORDER BY score DESC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def domain_exists(domain):
    """True for any lead domain already in the CRM."""
    if not domain:
        return False
    domain = domain.strip().lower()
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM leads WHERE lower(domain)=? LIMIT 1", (domain,)).fetchone() is not None


def domain_seen(domain):
    """Permanent no-repeat check across both saved leads and all previously inspected domains."""
    if not domain:
        return False
    domain = domain.strip().lower()
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM scrape_history WHERE domain=? LIMIT 1", (domain,)).fetchone():
            return True
        return conn.execute("SELECT 1 FROM leads WHERE lower(domain)=? LIMIT 1", (domain,)).fetchone() is not None


def mark_domain_seen(domain, country="", source_url="", status="seen", score=0, detail=""):
    """Persist a domain before crawling it so later scraping jobs never inspect it again."""
    if not domain:
        return
    domain = domain.strip().lower()
    now = utcnow()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO scrape_history(domain,first_seen,last_seen,country,source_url,status,score,detail)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(domain) DO UPDATE SET
                last_seen=excluded.last_seen,
                country=COALESCE(NULLIF(excluded.country,''), scrape_history.country),
                source_url=COALESCE(NULLIF(excluded.source_url,''), scrape_history.source_url),
                status=excluded.status,
                score=MAX(scrape_history.score, excluded.score),
                detail=COALESCE(NULLIF(excluded.detail,''), scrape_history.detail)
            """,
            (domain, now, now, country, source_url, status, int(score or 0), detail),
        )


def scrape_history_count():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM scrape_history").fetchone()[0]
