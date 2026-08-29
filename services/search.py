import time
from urllib.parse import urlparse

import requests

from .settings_store import load_env_file

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
    "Austria": ["Tiefkühlfisch Großhandel", "Forelle Importeur"],
    "Romania": ["importator peste congelat", "distribuitor pastrav"],
    "Bulgaria": ["вносител замразена риба", "дистрибутор пъстърва"],
    "Portugal": ["importador peixe congelado", "grossista truta"],
    "Greece": ["εισαγωγέας κατεψυγμένων ψαριών", "χονδρέμπορος πέστροφας"],
    "Finland": ["pakastekalan maahantuoja", "taimen tukkumyynti"],
    "Ireland": ["frozen fish importer", "trout wholesaler"],
    "Croatia": ["uvoznik smrznute ribe", "veleprodaja pastrve"],
    "Slovenia": ["uvoznik zamrznjenih rib", "veleprodaja postrvi"],
}

BLOCKED_HOSTS = {
    "facebook.com", "www.facebook.com", "linkedin.com", "www.linkedin.com",
    "instagram.com", "www.instagram.com", "youtube.com", "www.youtube.com",
    "tripadvisor.com", "www.tripadvisor.com", "amazon.com", "www.amazon.com",
    "x.com", "www.x.com", "twitter.com", "www.twitter.com",
}


def _clean_results(items):
    rows = []
    seen = set()
    for item in items or []:
        url = item.get("url") or item.get("href") or ""
        if not url:
            continue
        host = urlparse(url).netloc.lower()
        if not host or host in BLOCKED_HOSTS:
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "title": item.get("title", ""),
            "url": url,
            "description": item.get("description") or item.get("body") or "",
        })
    return rows


def ddgs_search(query, count=20):
    """Free metasearch. No API key is required."""
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError(
            "Free DDGS search is not installed. Run: py -m pip install -r requirements.txt"
        ) from exc

    max_results = max(1, min(int(count), 30))
    last_error = None
    # A retry makes the free backend much less brittle when a public backend rate-limits briefly.
    for attempt in range(3):
        try:
            results = DDGS(timeout=10).text(
                query,
                region="wt-wt",
                safesearch="moderate",
                max_results=max_results,
                backend="auto",
            )
            return _clean_results(results)
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Free DDGS search failed: {last_error}")


def brave_search(query, count=20):
    key = load_env_file().get("BRAVE_API_KEY", "")
    if not key:
        raise RuntimeError("Brave Search API key is not configured.")
    response = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"Accept": "application/json", "X-Subscription-Token": key},
        params={"q": query, "count": min(int(count), 20), "safesearch": "moderate"},
        timeout=25,
    )
    response.raise_for_status()
    return _clean_results(response.json().get("web", {}).get("results", []))


def search_web(query, count=20):
    cfg = load_env_file()
    provider = (cfg.get("SEARCH_PROVIDER") or "ddgs").strip().lower()
    if provider == "brave":
        return brave_search(query, count=count)
    return ddgs_search(query, count=count)


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
]


def build_queries(countries, custom_keywords=""):
    """Build a broad query pool so later runs can move beyond previously seen domains."""
    queries = []
    custom = [x.strip() for x in custom_keywords.splitlines() if x.strip()]
    for country in countries:
        if custom:
            terms = custom
        else:
            terms = list(COUNTRY_TERMS.get(country, ["frozen fish importer", "trout wholesaler"]))
            terms.extend(GENERIC_EXPANSION_TERMS)
        seen_terms = set()
        for term in terms:
            key = term.casefold()
            if key in seen_terms:
                continue
            seen_terms.add(key)
            queries.append((country, f'{term} {country}'))
    return queries
