import time

import requests

from .search import COUNTRY_TERMS
from .settings_store import load_env_file

TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,"
    "places.displayName,"
    "places.websiteUri,"
    "places.formattedAddress,"
    "places.businessStatus,"
    "nextPageToken"
)

DEFAULT_TERMS = (
    "frozen fish wholesaler",
    "seafood importer",
    "fish importer",
    "seafood distributor",
    "frozen seafood wholesaler",
    "trout wholesaler",
    "fish wholesaler",
    "frozen fish distributor",
    "seafood trading company",
    "fish distributor",
)

COUNTRY_CODES = {
    "Netherlands": "NL", "Germany": "DE", "Poland": "PL", "Denmark": "DK",
    "France": "FR", "Belgium": "BE", "Spain": "ES", "Italy": "IT",
    "Sweden": "SE", "Czechia": "CZ", "Lithuania": "LT", "Austria": "AT",
    "Romania": "RO", "Bulgaria": "BG", "Portugal": "PT", "Greece": "GR",
    "Finland": "FI", "Ireland": "IE", "Croatia": "HR", "Slovenia": "SI",
}

COUNTRY_CITIES = {
    "Netherlands": ("Rotterdam", "Amsterdam", "IJmuiden", "Urk", "The Hague"),
    "Germany": ("Hamburg", "Bremen", "Berlin", "Munich", "Cologne", "Dusseldorf"),
    "Poland": ("Gdansk", "Gdynia", "Warsaw", "Krakow", "Poznan"),
    "Denmark": ("Copenhagen", "Hirtshals", "Skagen", "Aarhus", "Esbjerg"),
    "France": ("Boulogne-sur-Mer", "Paris", "Marseille", "Lyon", "Lorient"),
    "Belgium": ("Brussels", "Antwerp", "Zeebrugge", "Ostend"),
    "Spain": ("Madrid", "Barcelona", "Vigo", "Valencia", "Bilbao"),
    "Italy": ("Milan", "Rome", "Genoa", "Venice", "Bologna"),
    "Sweden": ("Gothenburg", "Stockholm", "Malmo"),
    "Czechia": ("Prague", "Brno", "Ostrava"),
    "Lithuania": ("Klaipeda", "Vilnius", "Kaunas"),
    "Austria": ("Vienna", "Salzburg", "Graz", "Linz"),
    "Romania": ("Bucharest", "Constanta", "Cluj-Napoca"),
    "Bulgaria": ("Sofia", "Varna", "Burgas"),
    "Portugal": ("Lisbon", "Porto", "Aveiro"),
    "Greece": ("Athens", "Thessaloniki", "Piraeus"),
    "Finland": ("Helsinki", "Turku", "Tampere"),
    "Ireland": ("Dublin", "Cork", "Galway"),
    "Croatia": ("Zagreb", "Split", "Rijeka"),
    "Slovenia": ("Ljubljana", "Koper", "Maribor"),
}

TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}


def _api_key():
    key = (load_env_file().get("GOOGLE_PLACES_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "Google Places API key is not configured. Add it in Settings -> Company search."
        )
    return key


def _dedupe(values):
    output = []
    seen = set()
    for value in values:
        value = (value or "").strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
    return output


def _terms_for_country(country, custom_keywords=""):
    custom = [line.strip() for line in (custom_keywords or "").splitlines() if line.strip()]
    if custom:
        return _dedupe(custom)
    return _dedupe(list(COUNTRY_TERMS.get(country, ())) + list(DEFAULT_TERMS))


def _query_plan(countries, custom_keywords=""):
    per_country = []
    for country in countries:
        terms = _terms_for_country(country, custom_keywords)
        rows = [f"{term} in {country}" for term in terms]
        for city in COUNTRY_CITIES.get(country, ()):
            rows.extend(f"{term} in {city}, {country}" for term in terms)
        per_country.append((country, rows))

    depth = max((len(rows) for _, rows in per_country), default=0)
    for index in range(depth):
        for country, rows in per_country:
            if index < len(rows):
                yield country, rows[index]


def _search_page(session, key, country, query, page_size, page_token=""):
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    payload = {"textQuery": query, "pageSize": page_size}
    code = (COUNTRY_CODES.get(country) or "").strip()
    if code:
        payload["regionCode"] = code
    if page_token:
        payload["pageToken"] = page_token

    last_detail = ""
    for attempt in range(3):
        response = session.post(
            TEXT_SEARCH_URL,
            headers=headers,
            json=payload,
            timeout=(5, 25),
        )
        if response.status_code < 400:
            return response.json()
        last_detail = response.text[:500]
        if response.status_code not in TRANSIENT_HTTP_CODES or attempt == 2:
            raise RuntimeError(
                f"Google Places search failed ({response.status_code}) for {query}: {last_detail}"
            )
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Google Places search failed for {query}: {last_detail}")


def iter_google_place_candidates(countries, custom_keywords="", page_size=20):
    key = _api_key()
    page_size = max(1, min(int(page_size or 20), 20))
    seen_place_ids = set()

    with requests.Session() as session:
        for country, query in _query_plan(countries, custom_keywords):
            page_token = ""
            for _page in range(3):
                data = _search_page(session, key, country, query, page_size, page_token)
                for place in data.get("places", []):
                    place_id = (place.get("id") or "").strip()
                    website = (place.get("websiteUri") or "").strip()
                    status = (place.get("businessStatus") or "").strip()
                    display_name = ((place.get("displayName") or {}).get("text") or "").strip()
                    address = (place.get("formattedAddress") or "").strip()
                    if not place_id or place_id in seen_place_ids or not website:
                        continue
                    if status and status != "OPERATIONAL":
                        continue
                    seen_place_ids.add(place_id)
                    yield country, query, {
                        "url": website,
                        "title": display_name,
                        "description": address,
                        "place_id": place_id,
                        "business_status": status or "OPERATIONAL",
                        "discovery_source": "google_places",
                    }
                page_token = (data.get("nextPageToken") or "").strip()
                if not page_token:
                    break
