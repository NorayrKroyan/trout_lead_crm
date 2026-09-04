import time
from urllib.parse import urlparse

import requests

from .settings_store import load_env_file

# Correct UTF-8 search terms. The previous file contained mojibake in several
# languages, which substantially reduced search relevance outside English.
COUNTRY_TERMS = {
    "Netherlands": ["frozen fish importer", "trout wholesaler", "seafood distributor"],
    "Germany": ["Tiefkühlfisch Großhandel", "Fischimporteur", "Forelle Großhandel"],
    "Poland": ["importer mrożonych ryb", "hurtownia pstrąga", "dystrybutor ryb"],
    "Denmark": ["frossen fisk importør", "ørred grossist", "seafood wholesaler"],
    "France": ["importateur poisson surgelé", "grossiste truite", "distributeur produits de la mer"],
    "Belgium": ["frozen fish importer", "grossiste poisson surgelé", "trout wholesaler"],
    "Spain": ["importador pescado congelado", "mayorista trucha", "distribuidor pescado congelado"],
    "Italy": ["importatore pesce congelato", "grossista trota", "distributore prodotti ittici"],
    "Sweden": ["fryst fisk importör", "öring grossist", "seafood distributor"],
    "Czechia": ["dovozce mražených ryb", "velkoobchod pstruh", "distributor ryb"],
    "Lithuania": ["šaldytos žuvies importuotojas", "upėtakio didmenininkas", "žuvies platintojas"],
    "Austria": ["Tiefkühlfisch Großhandel", "Forelle Importeur", "Fischgroßhandel"],
    "Romania": ["importator pește congelat", "distribuitor păstrăv", "depozit pește congelat"],
    "Bulgaria": ["вносител замразена риба", "дистрибутор пъстърва", "търговия риба на едро"],
    "Portugal": ["importador peixe congelado", "grossista truta", "distribuidor pescado"],
    "Greece": ["εισαγωγέας κατεψυγμένων ψαριών", "χονδρέμπορος πέστροφας", "διανομέας ψαριών"],
    "Finland": ["pakastekalan maahantuoja", "taimen tukkumyynti", "kalatukku"],
    "Ireland": ["frozen fish importer", "trout wholesaler", "seafood foodservice supplier"],
    "Croatia": ["uvoznik smrznute ribe", "veleprodaja pastrve", "distributer ribe"],
    "Slovenia": ["uvoznik zamrznjenih rib", "veleprodaja postrvi", "distributer rib"],
}

COUNTRY_CITIES = {
    "Netherlands": ["Rotterdam", "Amsterdam", "IJmuiden", "Urk", "The Hague"],
    "Germany": ["Hamburg", "Bremen", "Berlin", "Munich", "Frankfurt", "Cologne"],
    "Poland": ["Gdansk", "Gdynia", "Warsaw", "Poznan", "Krakow", "Szczecin"],
    "Denmark": ["Copenhagen", "Hirtshals", "Aalborg", "Aarhus", "Esbjerg"],
    "France": ["Boulogne-sur-Mer", "Paris", "Lyon", "Marseille", "Rungis", "Bordeaux"],
    "Belgium": ["Brussels", "Antwerp", "Zeebrugge", "Ghent", "Ostend"],
    "Spain": ["Madrid", "Barcelona", "Vigo", "Valencia", "Bilbao", "A Coruna"],
    "Italy": ["Milan", "Rome", "Genoa", "Bologna", "Venice", "Naples"],
    "Sweden": ["Stockholm", "Gothenburg", "Malmo", "Helsingborg"],
    "Czechia": ["Prague", "Brno", "Ostrava", "Plzen"],
    "Lithuania": ["Vilnius", "Klaipeda", "Kaunas"],
    "Austria": ["Vienna", "Graz", "Linz", "Salzburg"],
    "Romania": ["Bucharest", "Constanta", "Cluj-Napoca", "Timisoara"],
    "Bulgaria": ["Sofia", "Varna", "Burgas", "Plovdiv"],
    "Portugal": ["Lisbon", "Porto", "Aveiro", "Matosinhos"],
    "Greece": ["Athens", "Thessaloniki", "Piraeus", "Patras"],
    "Finland": ["Helsinki", "Turku", "Tampere", "Oulu"],
    "Ireland": ["Dublin", "Cork", "Galway", "Limerick"],
    "Croatia": ["Zagreb", "Split", "Rijeka", "Zadar"],
    "Slovenia": ["Ljubljana", "Koper", "Maribor"],
}

COUNTRY_CODES = {
    "Netherlands": "NL", "Germany": "DE", "Poland": "PL", "Denmark": "DK",
    "France": "FR", "Belgium": "BE", "Spain": "ES", "Italy": "IT",
    "Sweden": "SE", "Czechia": "CZ", "Lithuania": "LT", "Austria": "AT",
    "Romania": "RO", "Bulgaria": "BG", "Portugal": "PT", "Greece": "GR",
    "Finland": "FI", "Ireland": "IE", "Croatia": "HR", "Slovenia": "SI",
}

# DDGS region codes materially improve local-business discovery quality.
COUNTRY_REGIONS = {
    "Netherlands": "nl-nl", "Germany": "de-de", "Poland": "pl-pl", "Denmark": "dk-da",
    "France": "fr-fr", "Belgium": "be-nl", "Spain": "es-es", "Italy": "it-it",
    "Sweden": "se-sv", "Czechia": "cz-cs", "Lithuania": "lt-lt", "Austria": "at-de",
    "Romania": "ro-ro", "Bulgaria": "bg-bg", "Portugal": "pt-pt", "Greece": "gr-el",
    "Finland": "fi-fi", "Ireland": "ie-en", "Croatia": "hr-hr", "Slovenia": "sl-sl",
}

BLOCKED_HOSTS = {
    "facebook.com", "linkedin.com", "instagram.com", "youtube.com",
    "tripadvisor.com", "amazon.com", "x.com", "twitter.com",
    "google.com", "googleusercontent.com", "wikipedia.org",
    "yelp.com", "yellowpages.com", "europages.com", "kompass.com",
    "alibaba.com", "indiamart.com", "tradeindia.com", "crunchbase.com",
    "pinterest.com", "tiktok.com", "zoominfo.com", "dnb.com",
}

COMMON_PREFIXES = {
    "www", "m", "mobile", "en", "de", "fr", "es", "it", "pl", "nl",
    "dk", "se", "sv", "cz", "lt", "pt", "ro", "bg", "fi", "hr", "si",
}

GENERIC_EXPANSION_TERMS = [
    "frozen seafood importer",
    "fish seafood wholesaler",
    "frozen fish distributor",
    "fish processing company",
    "seafood trading company",
    "foodservice seafood supplier",
    "aquaculture fish importer",
    "trout salmon processor",
    "trout distributor",
    "seafood purchasing wholesale",
    "fish import export company",
    "seafood cold store distributor",
    "HORECA fish supplier",
]


def _domain_key(url):
    host = urlparse(url).netloc.lower().split(":")[0].strip(".")
    if not host:
        return ""
    parts = host.split(".")
    while len(parts) > 2 and parts[0] in COMMON_PREFIXES:
        parts.pop(0)
    return ".".join(parts)


def _host_blocked(host):
    host = host.lower().split(":")[0].strip(".")
    return any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_HOSTS)


def _clean_results(items, search_source="web"):
    """Keep one useful result per company domain, not one result per URL."""
    rows = []
    seen_domains = set()
    for item in items or []:
        url = item.get("url") or item.get("href") or ""
        if not url:
            continue
        host = urlparse(url).netloc.lower().split(":")[0]
        if not host or _host_blocked(host):
            continue
        domain = _domain_key(url)
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        rows.append({
            "title": item.get("title", ""),
            "url": url,
            "description": item.get("description") or item.get("body") or "",
            "search_source": search_source,
        })
    return rows


def ddgs_search(query, count=20, page=0, country=""):
    """Free multi-engine search using DDGS with real pages and country-localized ranking."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError(
            "Free DDGS search is not installed. Run: py -m pip install -r requirements.txt"
        ) from exc

    max_results = max(1, min(int(count), 30))
    page_number = max(1, int(page or 0) + 1)  # app uses zero-based variations
    region = COUNTRY_REGIONS.get(country, "wt-wt")

    last_error = None
    # Short retry: DDGS auto already rotates/falls back across free engines.
    for attempt in range(2):
        try:
            engine = DDGS(timeout=8)
            # Keep auto: DDGS can aggregate/fall back across Bing, Brave web,
            # DuckDuckGo, Google, Mojeek, Yandex, Yahoo and others without API keys.
            results = engine.text(
                query,
                region=region,
                safesearch="moderate",
                max_results=max_results,
                page=page_number,
                backend="auto",
            )
            return _clean_results(results, search_source="ddgs")
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.6)
                # Some engines are more reliable with the global region.
                region = "wt-wt"
    raise RuntimeError(f"Free DDGS search failed: {last_error}")


def brave_search(query, count=20, page=0, country=""):
    """Official Brave Web Search API. Optional; DDGS remains the free default."""
    key = load_env_file().get("BRAVE_API_KEY", "")
    if not key:
        raise RuntimeError("Brave Search API key is not configured.")

    params = {
        "q": query,
        "count": min(max(int(count), 1), 20),
        "offset": min(max(int(page or 0), 0), 9),
        "safesearch": "moderate",
        "result_filter": "web",
    }
    country_code = COUNTRY_CODES.get(country)
    if country_code:
        params["country"] = country_code

    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": key,
        },
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    return _clean_results(response.json().get("web", {}).get("results", []), search_source="brave_api")


def search_web(query, count=20, page=0, country=""):
    """Use configured search engine; official Brave failure falls back to free DDGS."""
    cfg = load_env_file()
    provider = (cfg.get("SEARCH_PROVIDER") or "ddgs").strip().lower()
    if provider == "brave":
        try:
            return brave_search(query, count=count, page=page, country=country)
        except Exception:
            return ddgs_search(query, count=count, page=page, country=country)
    return ddgs_search(query, count=count, page=page, country=country)


def _country_queries(country, custom_keywords=""):
    custom = [x.strip() for x in custom_keywords.splitlines() if x.strip()]
    local_terms = custom or list(COUNTRY_TERMS.get(
        country, ["frozen fish importer", "trout wholesaler", "seafood distributor"]
    ))
    terms = list(local_terms)
    if not custom:
        terms.extend(GENERIC_EXPANSION_TERMS)

    unique_terms = []
    seen_terms = set()
    for term in terms:
        key = term.casefold()
        if key not in seen_terms:
            seen_terms.add(key)
            unique_terms.append(term)

    cities = COUNTRY_CITIES.get(country, [])
    rows = []
    for index, term in enumerate(unique_terms):
        rows.append((country, f"{term} {country}"))
        if cities:
            city = cities[index % len(cities)]
            rows.append((country, f"{term} {city} {country}"))
    for city in cities:
        rows.append((country, f"seafood fish company {city} {country} wholesale distributor"))
        rows.append((country, f"frozen fish supplier {city} {country}"))
    return rows


def build_queries(countries, custom_keywords=""):
    """
    Build a deterministic, round-robin country/city query pool.

    Round-robin ordering prevents a run from spending its whole query budget on
    the first selected country while preserving the existing persistent cursor.
    """
    per_country = [_country_queries(country, custom_keywords) for country in countries]
    queries = []
    depth = max((len(rows) for rows in per_country), default=0)
    for index in range(depth):
        for rows in per_country:
            if index < len(rows):
                queries.append(rows[index])

    output = []
    seen = set()
    for country, query in queries:
        key = (country.casefold(), query.casefold())
        if key in seen:
            continue
        seen.add(key)
        output.append((country, query))
    return output
