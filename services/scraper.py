import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
from .search import search_web, build_queries
from .db import upsert_lead, add_log, domain_seen, mark_domain_seen

USER_AGENT = "TroutLeadCRM/1.0 (+business-contact-discovery; public-business-emails-only)"
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

ROLE_PREFIXES = (
    "purchase", "purchasing", "procurement", "buying", "buyer", "sourcing",
    "import", "imports", "export", "sales", "commercial", "office", "info",
    "contact", "seafood", "fish", "orders", "order",
)

POSITIVE = {
    "rainbow trout": 35,
    "trout": 24,
    "forelle": 24,
    "pstrąg": 24,
    "pstruh": 24,
    "truite": 24,
    "trota": 24,
    "trucha": 24,
    "frozen fish": 25,
    "frozen seafood": 22,
    "tiefkühlfisch": 25,
    "poisson surgelé": 25,
    "pesce congelato": 25,
    "pescado congelado": 25,
    "seafood importer": 30,
    "fish importer": 30,
    "importer": 12,
    "wholesale": 18,
    "wholesaler": 18,
    "grosshandel": 18,
    "grossiste": 18,
    "mayorista": 18,
    "distributor": 16,
    "foodservice": 10,
    "horeca": 10,
    "salmon": 8,
    "aquaculture": 10,
}

NEGATIVE = {
    "restaurant": -35,
    "fishing charter": -50,
    "aquarium": -50,
    "pet food": -35,
    "sport fishing": -45,
    "recipe": -25,
    "hotel": -15,
}

CONTACT_PATHS = [
    "", "/contact", "/contact-us", "/contacts", "/about", "/about-us",
    "/purchasing", "/procurement", "/sales", "/impressum", "/kontakt",
]


def domain_of(url):
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def base_url(url):
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def robots_allowed(url):
    try:
        root = base_url(url)
        rp = RobotFileParser()
        rp.set_url(urljoin(root, "/robots.txt"))
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        # If robots.txt cannot be retrieved, stay conservative: only fetch the exact public result page.
        return True


def fetch_html(url):
    if not robots_allowed(url):
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15, allow_redirects=True)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "text/html" not in ctype:
            return ""
        return r.text[:1_500_000]
    except requests.RequestException:
        return ""


def normalize_company_name(title, domain):
    if title:
        for sep in [" | ", " – ", " - ", " — ", " :: "]:
            if sep in title:
                title = title.split(sep, 1)[0]
        title = re.sub(r"\s+", " ", title).strip()
        if 2 <= len(title) <= 120:
            return title
    return domain.split(".")[0].replace("-", " ").title()


def score_text(text):
    t = text.lower()
    score = 0
    evidence = []
    for phrase, pts in POSITIVE.items():
        if phrase in t:
            score += pts
            if pts > 0:
                evidence.append(phrase)
    for phrase, pts in NEGATIVE.items():
        if phrase in t:
            score += pts
    return max(score, 0), ", ".join(dict.fromkeys(evidence[:8]))


def extract_emails(html, domain):
    emails = set(e.lower().strip(".,;:()[]<>\"") for e in EMAIL_RE.findall(html or ""))
    filtered = []
    for email in emails:
        local, _, email_domain = email.partition("@")
        if not local or not email_domain:
            continue
        # Prefer business-role addresses and same-domain published contacts.
        role = any(local.startswith(prefix) for prefix in ROLE_PREFIXES)
        same_domain = email_domain == domain or email_domain.endswith("." + domain)
        if role or same_domain:
            if any(x in email for x in ["example.com", "sentry.io", "wixpress.com", "cloudflare.com"]):
                continue
            filtered.append((email, "role" if role else "published"))
    return sorted(filtered)


def _cancelled(cancel_event):
    return bool(cancel_event and cancel_event.is_set())


def crawl_company(result, country, min_score=55, cancel_event=None):
    start = result["url"]
    domain = domain_of(start)
    if not domain:
        return []
    root = base_url(start)

    collected_text = [result.get("title", ""), result.get("description", "")]
    pages = []
    for path in CONTACT_PATHS:
        if _cancelled(cancel_event):
            return []
        url = root if path == "" else urljoin(root, path)
        html = fetch_html(url)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        collected_text.append(text[:120_000])
        pages.append((url, html))
        # Short politeness delay, but keep cancellation responsive.
        for _ in range(5):
            if _cancelled(cancel_event):
                return []
            time.sleep(0.05)

    all_text = " ".join(collected_text)
    score, evidence = score_text(all_text)
    if score < min_score:
        return []

    emails = {}
    for url, html in pages:
        for email, email_type in extract_emails(html, domain):
            emails[email] = (email_type, url)

    company_name = normalize_company_name(result.get("title", ""), domain)
    leads = []
    for email, (email_type, source_url) in emails.items():
        leads.append({
            "company_name": company_name,
            "domain": domain,
            "website": root,
            "country": country,
            "email": email,
            "email_type": email_type,
            "score": score,
            "evidence": evidence or "frozen fish / seafood activity",
            "source_url": source_url,
        })
    return leads


def scrape_new_leads(countries, target=50, min_score=55, custom_keywords="", progress=None, cancel_event=None):
    # Target counts NEW COMPANY DOMAINS, not individual email addresses.
    # Permanent scrape_history prevents revisiting any previously inspected domain.
    target = max(1, min(int(target), 200))
    min_score = int(min_score)
    queries = build_queries(countries, custom_keywords)
    seen_this_run = set()
    saved_companies = 0
    saved_emails = 0
    inspected = 0
    skipped_seen = 0

    add_log("scrape_started", f"Target={target} new companies, countries={', '.join(countries)}")
    cancelled = False

    def report(message):
        if progress:
            try:
                progress(message, current=saved_companies, total=target, inspected=inspected, skipped_seen=skipped_seen)
            except TypeError:
                progress(message)

    for country, query in queries:
        if _cancelled(cancel_event):
            cancelled = True
            break
        if saved_companies >= target:
            break
        try:
            # Ask for more candidates because old domains are permanently skipped.
            results = search_web(query, count=30)
        except Exception as exc:
            add_log("scrape_error", f"Search failed for {query}: {exc}")
            continue

        for result in results:
            if _cancelled(cancel_event):
                cancelled = True
                break
            if saved_companies >= target:
                break
            source_url = result.get("url", "")
            domain = domain_of(source_url)
            if not domain or domain in seen_this_run:
                continue
            seen_this_run.add(domain)

            # Strong no-repeat rule across ALL previous runs, including domains
            # that had no email, failed qualification, or errored previously.
            if domain_seen(domain):
                skipped_seen += 1
                continue

            # Mark BEFORE crawling. Even if the website has no email or times out,
            # a later scraping run will not spend time on the same company again.
            mark_domain_seen(
                domain,
                country=country,
                source_url=source_url,
                status="inspecting",
                detail=f"Discovered from query: {query}",
            )
            inspected += 1
            report(
                f"Checking NEW domain {domain} ({country}) · "
                f"{saved_companies}/{target} companies · {skipped_seen} old skipped"
            )

            try:
                leads = crawl_company(result, country, min_score=min_score, cancel_event=cancel_event)
                if _cancelled(cancel_event):
                    cancelled = True
                    mark_domain_seen(
                        domain,
                        country=country,
                        source_url=source_url,
                        status="cancelled_partial",
                        detail="Inspection was stopped by user; domain remains remembered to avoid repeat scraping",
                    )
                    break
                if not leads:
                    mark_domain_seen(
                        domain,
                        country=country,
                        source_url=source_url,
                        status="no_qualified_contact",
                        detail="Inspected once; no qualifying public business email found",
                    )
                    continue

                company_added = False
                max_score = 0
                for lead in leads:
                    rowid = upsert_lead(lead)
                    max_score = max(max_score, int(lead.get("score", 0)))
                    if rowid:
                        saved_emails += 1
                        company_added = True
                        add_log(
                            "lead_found",
                            f"{lead['company_name']} · {lead['email']} · score {lead['score']}",
                            rowid,
                        )

                if company_added:
                    saved_companies += 1
                    mark_domain_seen(
                        domain,
                        country=country,
                        source_url=source_url,
                        status="lead_saved",
                        score=max_score,
                        detail=f"Saved {len(leads)} public business email(s)",
                    )
                else:
                    mark_domain_seen(
                        domain,
                        country=country,
                        source_url=source_url,
                        status="duplicate_contact",
                        score=max_score,
                        detail="Contacts already existed; no new lead inserted",
                    )
            except Exception as exc:
                mark_domain_seen(
                    domain,
                    country=country,
                    source_url=source_url,
                    status="error",
                    detail=str(exc)[:500],
                )
                add_log("scrape_error", f"{domain}: {exc}")

    final_event = "scrape_cancelled" if cancelled else "scrape_finished"
    add_log(
        final_event,
        f"Saved {saved_companies} new companies / {saved_emails} emails after inspecting "
        f"{inspected} new domains; skipped {skipped_seen} previously seen domains" +
        ("; stopped by user" if cancelled else ""),
    )
    return {
        "saved": saved_companies,
        "emails": saved_emails,
        "inspected": inspected,
        "skipped_seen": skipped_seen,
        "queries": len(queries),
        "cancelled": cancelled,
    }
