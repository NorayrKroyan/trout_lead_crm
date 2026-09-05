import html as html_lib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from .db import (
    add_log,
    get_conn,
    get_settings,
    mark_domain_seen,
    set_setting,
    upsert_lead,
)
from .google_places import iter_google_place_candidates
from .search import build_queries, search_web

USER_AGENT = "TroutLeadCRM/1.1 (+business-contact-discovery; public-business-emails-only)"
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s()./-]{6,}\d)")
MAX_HTML_BYTES = 900_000
MAX_CRAWL_WORKERS = 12

ROLE_PREFIXES = (
    "purchase", "purchasing", "procurement", "buying", "buyer", "sourcing",
    "import", "imports", "export", "sales", "commercial", "office", "info",
    "contact", "seafood", "fish", "orders", "order", "hello", "trade",
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
    "großhandel": 18,
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
    "market research report": -30,
    "industry report": -20,
    "conference": -15,
    "trade show": -15,
}

_CONTACT_HINTS = (
    "contact", "kontakt", "contacts", "contacto", "contatti", "kontakty",
    "about", "company", "impressum", "imprint", "sales", "commercial",
    "purchase", "purchasing", "procurement", "sourcing", "buying",
    "wholesale", "import", "export",
)
_CONTACT_PRIORITY = {
    "procurement": 100, "purchasing": 100, "purchase": 95, "sourcing": 95,
    "buying": 90, "sales": 85, "commercial": 82, "contact": 80,
    "kontakt": 80, "impressum": 75, "imprint": 75, "wholesale": 70,
    "import": 65, "export": 65, "about": 50, "company": 45,
}

_THREAD_LOCAL = threading.local()
_ROBOTS_CACHE = {}
_ROBOTS_LOCK = threading.Lock()


def domain_of(url):
    host = urlparse(url).netloc.lower().split(":")[0].strip(".")
    if not host:
        return ""
    prefixes = {
        "www", "m", "mobile", "en", "de", "fr", "es", "it", "pl", "nl",
        "dk", "se", "sv", "cz", "lt", "pt", "ro", "bg", "fi", "hr", "si",
    }
    parts = host.split(".")
    while len(parts) > 2 and parts[0] in prefixes:
        parts.pop(0)
    return ".".join(parts)


def base_url(url):
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}"


def _session():
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
            "Accept-Language": "en,*;q=0.7",
        })
        _THREAD_LOCAL.session = session
    return session


def robots_allowed(url):
    root = base_url(url)
    with _ROBOTS_LOCK:
        if root in _ROBOTS_CACHE:
            cached = _ROBOTS_CACHE[root]
            return True if cached is None else cached.can_fetch(USER_AGENT, url)

    parser = None
    try:
        response = _session().get(
            urljoin(root, "/robots.txt"),
            timeout=(2.5, 4.5),
            allow_redirects=True,
        )
        if response.status_code == 200 and response.text:
            parser = RobotFileParser()
            parser.set_url(urljoin(root, "/robots.txt"))
            parser.parse(response.text.splitlines())
    except requests.RequestException:
        parser = None

    with _ROBOTS_LOCK:
        _ROBOTS_CACHE[root] = parser
    return True if parser is None else parser.can_fetch(USER_AGENT, url)


def fetch_html_detailed(url):
    """Fetch a bounded HTML response and retain the failure/status reason for history."""
    if not robots_allowed(url):
        return "", 0, url, "blocked_by_robots"
    try:
        with _session().get(
            url,
            timeout=(3.5, 8.0),
            allow_redirects=True,
            stream=True,
        ) as response:
            status = int(response.status_code or 0)
            final_url = response.url or url
            if status >= 400:
                return "", status, final_url, f"http_{status}"
            ctype = (response.headers.get("content-type") or "").lower()
            if ctype and "text/html" not in ctype and "application/xhtml" not in ctype:
                return "", status, final_url, "not_html"
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                remaining = MAX_HTML_BYTES - total
                if remaining <= 0:
                    break
                chunk = chunk[:remaining]
                chunks.append(chunk)
                total += len(chunk)
                if total >= MAX_HTML_BYTES:
                    break
            raw = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            try:
                text = raw.decode(encoding, errors="replace")
            except (LookupError, UnicodeError):
                text = raw.decode("utf-8", errors="replace")
            return text, status, final_url, ""
    except requests.Timeout:
        return "", 0, url, "timeout"
    except requests.RequestException as exc:
        return "", 0, url, exc.__class__.__name__.lower()


def fetch_html(url):
    """Backward-compatible wrapper used by older code/tests."""
    return fetch_html_detailed(url)[0]


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


def _decode_cfemail(value):
    try:
        data = bytes.fromhex(value)
        if len(data) < 2:
            return ""
        key = data[0]
        return "".join(chr(b ^ key) for b in data[1:])
    except Exception:
        return ""


def extract_emails(html, domain):
    raw_html = html or ""
    candidates = set(
        e.lower().strip(".,;:()[]<>\"") for e in EMAIL_RE.findall(html_lib.unescape(raw_html))
    )
    # mailto links may contain URL-encoded characters not caught by the first regex.
    for match in re.findall(r"mailto:([^\"'<>?\s]+)", raw_html, flags=re.I):
        decoded = unquote(html_lib.unescape(match)).strip()
        if EMAIL_RE.fullmatch(decoded):
            candidates.add(decoded.lower())
    # Cloudflare email protection is common on small company sites.
    for encoded in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', raw_html):
        decoded = _decode_cfemail(encoded).lower().strip()
        if EMAIL_RE.fullmatch(decoded):
            candidates.add(decoded)

    filtered = []
    for email in candidates:
        local, _, email_domain = email.partition("@")
        if not local or not email_domain:
            continue
        role = any(local.startswith(prefix) for prefix in ROLE_PREFIXES)
        same_domain = email_domain == domain or email_domain.endswith("." + domain)
        if role or same_domain:
            if any(x in email for x in [
                "example.com", "sentry.io", "wixpress.com", "cloudflare.com",
                "wordpress.org", "schema.org",
            ]):
                continue
            filtered.append((email, "role" if role else "published"))
    return sorted(set(filtered))


def _iter_jsonld(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_jsonld(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_jsonld(child)


def _format_address(value):
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if not isinstance(value, dict):
        return ""
    parts = [
        value.get("streetAddress"), value.get("addressLocality"),
        value.get("addressRegion"), value.get("postalCode"), value.get("addressCountry"),
    ]
    return ", ".join(str(x).strip() for x in parts if x and str(x).strip())


def extract_site_details(pages):
    phone = ""
    address = ""
    for _, page_html in pages:
        if not page_html:
            continue
        soup = BeautifulSoup(page_html, "html.parser")
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text(" ", strip=True)
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            for item in _iter_jsonld(payload):
                if not phone:
                    candidate = item.get("telephone") or item.get("phone")
                    if candidate:
                        phone = re.sub(r"\s+", " ", str(candidate)).strip()
                if not address:
                    address = _format_address(item.get("address"))
                if phone and address:
                    return phone[:100], address[:500]
        if not phone:
            tel = soup.find("a", href=lambda v: isinstance(v, str) and v.lower().startswith("tel:"))
            if tel:
                phone = re.sub(r"\s+", " ", tel.get("href", "")[4:]).strip()
            else:
                match = PHONE_RE.search(soup.get_text(" ", strip=True))
                if match:
                    phone = re.sub(r"\s+", " ", match.group(0)).strip()
        if not address:
            tag = soup.find("address")
            if tag:
                address = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if phone and address:
            break
    return phone[:100], address[:500]


_SCRAPER_V2_MARKER = True
_SCRAPER_V3_MARKER = True


def _load_seen_domains():
    """Load the no-repeat set once per run instead of doing a SQL query per result."""
    domains = set()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT domain FROM scrape_history
            UNION
            SELECT lower(domain) FROM leads
            """
        ).fetchall()
    for row in rows:
        raw = row[0] if row else ""
        if not raw:
            continue
        canonical = domain_of("https://" + str(raw).strip().lower())
        if canonical:
            domains.add(canonical)
    return domains


def _same_company_link(url, domain):
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    return domain_of(url) == domain


def _contact_links(page_url, soup, domain, limit=7):
    """Discover and prioritize real same-site contact/commercial links."""
    found = {}
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        label = (anchor.get_text(" ", strip=True) + " " + href).lower()
        hits = [word for word in _CONTACT_HINTS if word in label]
        if not hits:
            continue
        absolute = urljoin(page_url, href).split("#", 1)[0]
        if not _same_company_link(absolute, domain):
            continue
        score = max((_CONTACT_PRIORITY.get(hit, 30) for hit in hits), default=30)
        found[absolute] = max(found.get(absolute, 0), score)
    return [url for url, _ in sorted(found.items(), key=lambda item: (-item[1], len(item[0])))[:limit]]


def _cancelled(cancel_event):
    return bool(cancel_event and cancel_event.is_set())


def analyze_company(result, country, min_score=55, cancel_event=None):
    """Analyze one company website and return leads plus a complete audit record."""
    start = (result.get("url") or "").strip()
    domain = domain_of(start)
    analysis = {
        "domain": domain,
        "company_name": "",
        "website": base_url(start) if domain else start,
        "score": 0,
        "evidence": "",
        "emails": [],
        "email_count": 0,
        "phone": "",
        "address": "",
        "pages_checked": 0,
        "http_status": 0,
        "reason": "",
        "cancelled": False,
        "leads": [],
    }
    if not domain:
        analysis["reason"] = "Invalid or missing company website URL."
        return analysis

    root = base_url(start)
    collected_text = [result.get("title", ""), result.get("description", "")]
    pages = []
    emails = {}
    site_title = ""
    fetch_errors = []
    http_statuses = []

    queue = []
    for candidate in (start, root):
        if candidate and candidate not in queue:
            queue.append(candidate)
    visited = set()
    max_pages = 6

    while queue and len(pages) < max_pages:
        if _cancelled(cancel_event):
            analysis["cancelled"] = True
            analysis["reason"] = "Stopped by user during website analysis."
            break

        url = queue.pop(0).split("#", 1)[0]
        if url in visited or not _same_company_link(url, domain):
            continue
        visited.add(url)

        page_html, status, final_url, error = fetch_html_detailed(url)
        if status:
            http_statuses.append(status)
        if error:
            fetch_errors.append(f"{url}: {error}")
        if not page_html:
            continue

        soup = BeautifulSoup(page_html, "html.parser")
        if not site_title and soup.title:
            site_title = soup.title.get_text(" ", strip=True)

        text = soup.get_text(" ", strip=True)
        collected_text.append(text[:100_000])
        pages.append((final_url, page_html))

        for email, email_type in extract_emails(page_html, domain):
            emails[email] = (email_type, final_url)

        for link in _contact_links(final_url, soup, domain):
            if link not in visited and link not in queue:
                queue.append(link)

        if len(pages) == 1 and len(queue) < 2:
            for path in ("/contact", "/kontakt", "/impressum", "/contact-us", "/about-us", "/sales", "/procurement"):
                candidate = urljoin(root, path)
                if candidate not in visited and candidate not in queue:
                    queue.append(candidate)

        score_now, _ = score_text(" ".join(collected_text))
        if emails and score_now >= int(min_score) and len(pages) >= 2:
            break

        # Tiny courtesy pause per site. Multiple different domains are processed concurrently.
        if not _cancelled(cancel_event):
            time.sleep(0.03)

    all_text = " ".join(collected_text)
    score, evidence = score_text(all_text)
    phone, address = extract_site_details(pages) if pages else ("", "")
    company_name = normalize_company_name(site_title or result.get("title", ""), domain)

    analysis.update({
        "company_name": company_name,
        "website": root,
        "score": score,
        "evidence": evidence,
        "emails": sorted(emails),
        "email_count": len(emails),
        "phone": phone,
        "address": address,
        "pages_checked": len(pages),
        "http_status": max(http_statuses) if http_statuses else 0,
    })

    if analysis["cancelled"]:
        return analysis
    if not pages:
        suffix = fetch_errors[0].split(": ", 1)[-1] if fetch_errors else "no readable HTML"
        analysis["reason"] = f"Website could not be analyzed ({suffix})."
        return analysis
    if score < int(min_score):
        analysis["reason"] = f"Fit score {score} is below the minimum {int(min_score)}."
        return analysis
    if not emails:
        analysis["reason"] = "Relevant website, but no qualifying public business email was found."
        return analysis

    discovery_source = result.get("discovery_source") or result.get("search_source") or "web"
    place_id = result.get("place_id", "")
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
            "phone": phone,
            "address": address,
            "discovery_source": discovery_source,
            "google_place_id": place_id,
        })
    analysis["leads"] = leads
    analysis["reason"] = f"Qualified: {len(leads)} public business email(s) found."
    return analysis


def crawl_company(result, country, min_score=55, cancel_event=None):
    """Backward-compatible API: callers expecting only leads still receive a list."""
    return analyze_company(result, country, min_score=min_score, cancel_event=cancel_event)["leads"]


def _history_kwargs(result, country, query, analysis, discovery_source):
    return {
        "country": country,
        "source_url": result.get("url", ""),
        "company_name": analysis.get("company_name", "") or result.get("title", ""),
        "website": analysis.get("website", "") or result.get("url", ""),
        "discovery_source": discovery_source,
        "search_query": query,
        "evidence": analysis.get("evidence", ""),
        "emails": ", ".join(analysis.get("emails", [])),
        "email_count": analysis.get("email_count", 0),
        "phone": analysis.get("phone", ""),
        "address": analysis.get("address", ""),
        "pages_checked": analysis.get("pages_checked", 0),
        "reason": analysis.get("reason", ""),
        "google_place_id": result.get("place_id", ""),
        "http_status": analysis.get("http_status", 0),
        "search_title": result.get("title", ""),
    }


def _persist_analysis(result, country, query, analysis, discovery_source, lead_log_event):
    """Save leads and update the analyzed-websites audit row. Returns (company_added, email_count)."""
    domain = analysis.get("domain") or domain_of(result.get("url", ""))
    history = _history_kwargs(result, country, query, analysis, discovery_source)

    if analysis.get("cancelled"):
        mark_domain_seen(
            domain, status="cancelled_partial", score=analysis.get("score", 0),
            detail="Stopped by user; partial website analysis retained", **history,
        )
        return False, 0

    leads = analysis.get("leads", [])
    if not leads:
        status = "unreachable" if analysis.get("pages_checked", 0) == 0 else "no_qualified_contact"
        mark_domain_seen(
            domain, status=status, score=analysis.get("score", 0),
            detail=analysis.get("reason", "No qualifying public business contact found"), **history,
        )
        return False, 0

    company_added = False
    saved_emails = 0
    max_score = 0
    for lead in leads:
        rowid = upsert_lead(lead)
        max_score = max(max_score, int(lead.get("score", 0)))
        if rowid:
            saved_emails += 1
            company_added = True
            add_log(lead_log_event, f"{lead['company_name']} · {lead['email']} · score {lead['score']}", rowid)

    if company_added:
        mark_domain_seen(
            domain, status="lead_saved", score=max_score,
            detail=f"Saved {len(leads)} public business email(s)", **history,
        )
    else:
        mark_domain_seen(
            domain, status="duplicate_contact", score=max_score,
            detail="Contacts already existed; no new lead inserted", **history,
        )
    return company_added, saved_emails


def scrape_new_leads(countries, target=50, min_score=55, custom_keywords="", progress=None, cancel_event=None):
    """Stateful no-repeat discovery with concurrent crawling of different company domains."""
    target = max(1, min(int(target), 200))
    min_score = int(min_score)
    all_queries = build_queries(countries, custom_keywords)
    if not all_queries:
        return {"saved": 0, "emails": 0, "inspected": 0, "skipped_seen": 0, "queries": 0, "cancelled": False}

    settings = get_settings()
    try:
        cursor = int(settings.get("scrape_query_cursor", "0"))
    except (TypeError, ValueError):
        cursor = 0
    try:
        run_number = int(settings.get("scrape_run_number", "0"))
    except (TypeError, ValueError):
        run_number = 0

    cursor %= len(all_queries)
    queries = all_queries[cursor:] + all_queries[:cursor]
    query_budget = min(len(queries), max(12, min(50, target + 10)))
    known_domains = _load_seen_domains()
    seen_this_run = set()

    saved_companies = 0
    saved_emails = 0
    inspected = 0
    skipped_seen = 0
    queries_attempted = 0
    cancelled = False

    add_log(
        "scrape_started",
        f"Target={target} new companies, countries={', '.join(countries)}, cursor={cursor}, run={run_number}, workers={MAX_CRAWL_WORKERS}",
    )

    def report(message):
        if progress:
            try:
                progress(message, current=saved_companies, total=target, inspected=inspected, skipped_seen=skipped_seen)
            except TypeError:
                progress(message)

    with ThreadPoolExecutor(max_workers=MAX_CRAWL_WORKERS, thread_name_prefix="troutcrawl") as executor:
        for query_index, (country, query) in enumerate(queries[:query_budget]):
            if _cancelled(cancel_event):
                cancelled = True
                break
            if saved_companies >= target:
                break

            queries_attempted += 1
            page = (run_number + query_index) % 5
            report(f"Searching {country}: {query} · page {page + 1} · {saved_companies}/{target} saved")
            try:
                results = search_web(query, count=30, page=page, country=country)
            except Exception as exc:
                add_log("scrape_error", f"Search failed for {query}: {exc}")
                continue

            index = 0
            while index < len(results) and saved_companies < target and not _cancelled(cancel_event):
                slots = min(MAX_CRAWL_WORKERS, target - saved_companies)
                batch = []
                while index < len(results) and len(batch) < slots:
                    result = results[index]
                    index += 1
                    source_url = result.get("url", "")
                    domain = domain_of(source_url)
                    if not domain or domain in seen_this_run:
                        continue
                    seen_this_run.add(domain)
                    if domain in known_domains:
                        skipped_seen += 1
                        continue
                    known_domains.add(domain)
                    discovery_source = result.get("search_source") or "web"
                    mark_domain_seen(
                        domain, country=country, source_url=source_url, status="inspecting",
                        detail=f"Query: {query}; page={page + 1}", website=base_url(source_url),
                        discovery_source=discovery_source, search_query=query,
                        search_title=result.get("title", ""),
                    )
                    inspected += 1
                    batch.append((domain, result, discovery_source))

                if not batch:
                    continue

                report(f"Analyzing {len(batch)} new websites in parallel · {saved_companies}/{target} saved · {inspected} inspected")
                futures = {
                    executor.submit(analyze_company, result, country, min_score, cancel_event): (domain, result, source)
                    for domain, result, source in batch
                }
                for future in as_completed(futures):
                    domain, result, source = futures[future]
                    try:
                        analysis = future.result()
                        added, email_count = _persist_analysis(
                            result, country, query, analysis, source, "lead_found"
                        )
                        if added:
                            saved_companies += 1
                            saved_emails += email_count
                    except Exception as exc:
                        mark_domain_seen(
                            domain, country=country, source_url=result.get("url", ""), status="error",
                            detail=str(exc)[:500], discovery_source=source, search_query=query,
                            website=base_url(result.get("url", "")), reason=str(exc)[:500],
                            search_title=result.get("title", ""),
                        )
                        add_log("scrape_error", f"{domain}: {exc}")
                    report(f"Processed {domain} · {saved_companies}/{target} saved · {inspected} inspected · {skipped_seen} old skipped")

                if _cancelled(cancel_event):
                    cancelled = True
                    break

            if cancelled:
                break

    advance = max(queries_attempted, 1)
    next_cursor = (cursor + advance) % len(all_queries)
    set_setting("scrape_query_cursor", next_cursor)
    set_setting("scrape_run_number", run_number + 1)

    final_event = "scrape_cancelled" if cancelled else "scrape_finished"
    add_log(
        final_event,
        f"Saved {saved_companies} new companies / {saved_emails} emails after inspecting {inspected} new domains; "
        f"skipped {skipped_seen} old domains; searched {queries_attempted} query variations; next cursor={next_cursor}"
        + ("; stopped by user" if cancelled else ""),
    )
    return {
        "saved": saved_companies, "emails": saved_emails, "inspected": inspected,
        "skipped_seen": skipped_seen, "queries": queries_attempted, "cancelled": cancelled,
    }


def scrape_google_places_leads(countries, target=50, min_score=55, custom_keywords="", progress=None, cancel_event=None):
    """Discover with Google Places, then analyze different company websites concurrently."""
    target = max(1, min(int(target), 200))
    min_score = int(min_score)
    known_domains = _load_seen_domains()
    seen_this_run = set()
    saved_companies = 0
    saved_emails = 0
    inspected = 0
    skipped_seen = 0
    cancelled = False

    add_log("places_scrape_started", f"Google Places target={target} new companies, countries={', '.join(countries)}, workers={MAX_CRAWL_WORKERS}")

    def report(message):
        if progress:
            try:
                progress(message, current=saved_companies, total=target, inspected=inspected, skipped_seen=skipped_seen)
            except TypeError:
                progress(message)

    try:
        candidates = iter(iter_google_place_candidates(countries, custom_keywords, page_size=20))
        exhausted = False
        with ThreadPoolExecutor(max_workers=MAX_CRAWL_WORKERS, thread_name_prefix="troutplaces") as executor:
            while saved_companies < target and not exhausted and not _cancelled(cancel_event):
                slots = min(MAX_CRAWL_WORKERS, target - saved_companies)
                batch = []
                while len(batch) < slots and not exhausted:
                    try:
                        country, query, result = next(candidates)
                    except StopIteration:
                        exhausted = True
                        break
                    source_url = result.get("url", "")
                    domain = domain_of(source_url)
                    if not domain or domain in seen_this_run:
                        continue
                    seen_this_run.add(domain)
                    if domain in known_domains:
                        skipped_seen += 1
                        continue
                    known_domains.add(domain)
                    mark_domain_seen(
                        domain, country=country, source_url=source_url, status="inspecting",
                        detail=f"Google Places query: {query}; place_id={result.get('place_id','')}",
                        website=base_url(source_url), discovery_source="google_places", search_query=query,
                        google_place_id=result.get("place_id", ""), search_title=result.get("title", ""),
                    )
                    inspected += 1
                    batch.append((country, query, domain, result))

                if not batch:
                    continue
                report(f"Google Places → analyzing {len(batch)} new websites in parallel · {saved_companies}/{target} saved")
                futures = {
                    executor.submit(analyze_company, result, country, min_score, cancel_event): (country, query, domain, result)
                    for country, query, domain, result in batch
                }
                for future in as_completed(futures):
                    country, query, domain, result = futures[future]
                    try:
                        analysis = future.result()
                        added, email_count = _persist_analysis(
                            result, country, query, analysis, "google_places", "lead_found_google_places"
                        )
                        if added:
                            saved_companies += 1
                            saved_emails += email_count
                    except Exception as exc:
                        mark_domain_seen(
                            domain, country=country, source_url=result.get("url", ""), status="error",
                            detail=str(exc)[:500], discovery_source="google_places", search_query=query,
                            website=base_url(result.get("url", "")), reason=str(exc)[:500],
                            google_place_id=result.get("place_id", ""),
                        )
                        add_log("places_scrape_error", f"{domain}: {exc}")
                    report(f"Google Places → processed {domain} · {saved_companies}/{target} saved · {inspected} inspected")

        if _cancelled(cancel_event):
            cancelled = True
    except Exception as exc:
        add_log("places_scrape_error", str(exc))
        raise

    final_event = "places_scrape_cancelled" if cancelled else "places_scrape_finished"
    add_log(
        final_event,
        f"Saved {saved_companies} new companies / {saved_emails} emails after inspecting {inspected} new domains; "
        f"skipped {skipped_seen} previously seen domains" + ("; stopped by user" if cancelled else ""),
    )
    return {
        "saved": saved_companies, "emails": saved_emails, "inspected": inspected,
        "skipped_seen": skipped_seen, "queries": 0, "cancelled": cancelled,
    }

def scrape_hybrid_leads(
    countries,
    target=50,
    min_score=55,
    custom_keywords="",
    progress=None,
    cancel_event=None,
):
    target = max(1, min(int(target), 200))
    totals = {
        "saved": 0,
        "emails": 0,
        "inspected": 0,
        "skipped_seen": 0,
        "queries": 0,
        "cancelled": False,
    }

    def google_progress(message, current=0, total=0, inspected=0, skipped_seen=0, **_):
        if progress:
            progress(
                f"Hybrid - Google - {message}",
                current=current,
                total=target,
                inspected=inspected,
                skipped_seen=skipped_seen,
            )

    try:
        result = scrape_google_places_leads(
            countries,
            target=target,
            min_score=min_score,
            custom_keywords=custom_keywords,
            progress=google_progress,
            cancel_event=cancel_event,
        )
    except Exception as exc:
        add_log("hybrid_google_fallback", f"Google Places unavailable: {exc}")
        result = {
            "saved": 0,
            "emails": 0,
            "inspected": 0,
            "skipped_seen": 0,
            "queries": 0,
            "cancelled": False,
        }

    for key in ("saved", "emails", "inspected", "skipped_seen", "queries"):
        totals[key] += int(result.get(key, 0) or 0)
    totals["cancelled"] = bool(result.get("cancelled"))

    if totals["cancelled"] or _cancelled(cancel_event) or totals["saved"] >= target:
        return totals

    remaining = target - totals["saved"]
    base_saved = totals["saved"]
    base_inspected = totals["inspected"]
    base_skipped = totals["skipped_seen"]

    def web_progress(message, current=0, total=0, inspected=0, skipped_seen=0, **_):
        if progress:
            progress(
                f"Hybrid - Web - {message}",
                current=base_saved + int(current or 0),
                total=target,
                inspected=base_inspected + int(inspected or 0),
                skipped_seen=base_skipped + int(skipped_seen or 0),
            )

    web = scrape_new_leads(
        countries,
        target=remaining,
        min_score=min_score,
        custom_keywords=custom_keywords,
        progress=web_progress,
        cancel_event=cancel_event,
    )

    for key in ("saved", "emails", "inspected", "skipped_seen", "queries"):
        totals[key] += int(web.get(key, 0) or 0)
    totals["cancelled"] = bool(web.get("cancelled")) or _cancelled(cancel_event)
    return totals
