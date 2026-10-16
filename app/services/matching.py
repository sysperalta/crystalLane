"""Load matching: turns what a carrier says ("I'm empty near Chicago, heading to Texas")
into a ranked list of available loads.

Origin must match (same city, within 150 miles, or same state). Destination and pickup
date improve the ranking. Best match first, at most 3.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache

from app.tms.protocol import TmsLoad

MAX_RESULTS = 3

_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_STATE_CODES = set(_STATES.values())

# Freight hubs and every city used by the simulated TMS: (city, state) -> (lat, lon).
_CITIES: dict[tuple[str, str], tuple[float, float]] = {
    ("joliet", "IL"): (41.525, -88.082), ("chicago", "IL"): (41.878, -87.630),
    ("aurora", "IL"): (41.761, -88.320), ("bolingbrook", "IL"): (41.699, -88.068),
    ("naperville", "IL"): (41.786, -88.147), ("elgin", "IL"): (42.035, -88.282),
    ("rockford", "IL"): (42.271, -89.094), ("peoria", "IL"): (40.694, -89.589),
    ("springfield", "IL"): (39.781, -89.650), ("gary", "IN"): (41.593, -87.346),
    ("indianapolis", "IN"): (39.768, -86.158), ("fort wayne", "IN"): (41.079, -85.139),
    ("milwaukee", "WI"): (43.039, -87.906), ("madison", "WI"): (43.073, -89.401),
    ("green bay", "WI"): (44.513, -88.013), ("minneapolis", "MN"): (44.978, -93.265),
    ("des moines", "IA"): (41.587, -93.625), ("omaha", "NE"): (41.257, -95.935),
    ("kansas city", "MO"): (39.100, -94.579), ("st louis", "MO"): (38.627, -90.199),
    ("springfield", "MO"): (37.209, -93.292), ("wichita", "KS"): (37.687, -97.330),
    ("detroit", "MI"): (42.331, -83.046), ("grand rapids", "MI"): (42.963, -85.668),
    ("columbus", "OH"): (39.961, -82.999), ("cleveland", "OH"): (41.499, -81.694),
    ("cincinnati", "OH"): (39.103, -84.512), ("toledo", "OH"): (41.654, -83.537),
    ("louisville", "KY"): (38.253, -85.759), ("nashville", "TN"): (36.163, -86.781),
    ("memphis", "TN"): (35.149, -90.049), ("knoxville", "TN"): (35.961, -83.921),
    ("dallas", "TX"): (32.777, -96.797), ("fort worth", "TX"): (32.755, -97.331),
    ("arlington", "TX"): (32.736, -97.108), ("houston", "TX"): (29.760, -95.370),
    ("san antonio", "TX"): (29.424, -98.494), ("austin", "TX"): (30.267, -97.743),
    ("el paso", "TX"): (31.762, -106.485), ("laredo", "TX"): (27.506, -99.507),
    ("waco", "TX"): (31.549, -97.147), ("lubbock", "TX"): (33.578, -101.855),
    ("amarillo", "TX"): (35.222, -101.831), ("corpus christi", "TX"): (27.801, -97.396),
    ("oklahoma city", "OK"): (35.468, -97.516), ("tulsa", "OK"): (36.154, -95.993),
    ("little rock", "AR"): (34.746, -92.290), ("shreveport", "LA"): (32.525, -93.750),
    ("new orleans", "LA"): (29.951, -90.072), ("jackson", "MS"): (32.299, -90.185),
    ("birmingham", "AL"): (33.519, -86.810), ("mobile", "AL"): (30.695, -88.040),
    ("atlanta", "GA"): (33.749, -84.388), ("savannah", "GA"): (32.081, -81.091),
    ("charlotte", "NC"): (35.227, -80.843), ("raleigh", "NC"): (35.780, -78.638),
    ("greenville", "SC"): (34.852, -82.394), ("jacksonville", "FL"): (30.332, -81.656),
    ("orlando", "FL"): (28.538, -81.379), ("tampa", "FL"): (27.951, -82.457),
    ("miami", "FL"): (25.762, -80.192), ("richmond", "VA"): (37.541, -77.436),
    ("baltimore", "MD"): (39.290, -76.612), ("philadelphia", "PA"): (39.953, -75.165),
    ("pittsburgh", "PA"): (40.441, -79.996), ("harrisburg", "PA"): (40.273, -76.887),
    ("allentown", "PA"): (40.608, -75.490), ("newark", "NJ"): (40.736, -74.172),
    ("jersey city", "NJ"): (40.728, -74.078), ("new york", "NY"): (40.713, -74.006),
    ("buffalo", "NY"): (42.886, -78.878), ("albany", "NY"): (42.653, -73.756),
    ("hartford", "CT"): (41.764, -72.685), ("boston", "MA"): (42.360, -71.058),
    ("denver", "CO"): (39.739, -104.990), ("salt lake city", "UT"): (40.761, -111.891),
    ("albuquerque", "NM"): (35.084, -106.650), ("phoenix", "AZ"): (33.448, -112.074),
    ("tucson", "AZ"): (32.222, -110.975), ("las vegas", "NV"): (36.170, -115.140),
    ("reno", "NV"): (39.530, -119.814), ("los angeles", "CA"): (34.052, -118.244),
    ("long beach", "CA"): (33.770, -118.194), ("ontario", "CA"): (34.063, -117.651),
    ("riverside", "CA"): (33.953, -117.396), ("san bernardino", "CA"): (34.108, -117.290),
    ("fresno", "CA"): (36.738, -119.787), ("sacramento", "CA"): (38.582, -121.494),
    ("oakland", "CA"): (37.804, -122.271), ("boise", "ID"): (43.615, -116.202),
    ("portland", "OR"): (45.515, -122.679), ("seattle", "WA"): (47.606, -122.332),
}
_CITIES_BY_NAME: dict[str, list[str]] = {}
for _city, _state in _CITIES:
    _CITIES_BY_NAME.setdefault(_city, []).append(_state)

_ALIASES = {
    "dfw": "dallas, tx", "dallas fort worth": "dallas, tx", "nyc": "new york, ny",
    "la": "los angeles, ca", "slc": "salt lake city, ut", "okc": "oklahoma city, ok",
    "kc": "kansas city, mo", "chi town": "chicago, il", "chitown": "chicago, il",
    "atl": "atlanta, ga", "philly": "philadelphia, pa", "vegas": "las vegas, nv",
    "nola": "new orleans, la", "inland empire": "ontario, ca",
}
_FILLER = re.compile(r"\b(?:the|greater|metro|metropolitan|area|region|vicinity|near|around|outside|close to)\b")
_ABBREVIATIONS = ((re.compile(r"\bsaint\b"), "st"), (re.compile(r"\bft\b"), "fort"), (re.compile(r"\bmt\b"), "mount"))


@dataclass(frozen=True)
class Place:
    city: str | None
    state: str | None
    coords: tuple[float, float] | None


@lru_cache(maxsize=2048)
def parse_place(text: str) -> Place:
    t = re.sub(r"[^a-z0-9,\s'-]", " ", text.lower())
    t = t.replace("-", " ")
    for pattern, replacement in _ABBREVIATIONS:
        t = pattern.sub(replacement, t)
    t = _FILLER.sub(" ", t)
    t = re.sub(r"^\s*(?:in|from|to|of)\s+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,")
    t = _ALIASES.get(t.replace(",", "").strip(), t)

    words = t.replace(",", " ").split()
    city_words, state = words, None
    for size in (2, 1):  # "new york", "north carolina" before single words
        if len(words) >= size:
            tail = " ".join(words[-size:])
            code = _STATES.get(tail) or (tail.upper() if size == 1 and tail.upper() in _STATE_CODES else None)
            if code:
                city_words, state = words[:-size], code
                break
    city = " ".join(city_words) or None

    if city is None and state is not None and t in _CITIES_BY_NAME:  # "new york" alone = the city
        city = t
    if city is not None and state is None:
        states = _CITIES_BY_NAME.get(city, [])
        if len(states) == 1:
            state = states[0]
    coords = _CITIES.get((city, state)) if city and state else None
    return Place(city, state, coords)


def _miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def location_score(query: Place, candidate: Place) -> float:
    if query.coords and candidate.coords:
        distance = _miles(query.coords, candidate.coords)
        if distance <= 10:
            return 1.0
        if distance <= 75:
            return 0.85
        if distance <= 150:
            return 0.6
    if query.city and query.city == candidate.city and query.state in (None, candidate.state):
        return 1.0
    if query.state and query.state == candidate.state:
        return 0.6 if query.city is None else 0.3
    return 0.0


def rank_loads(
    loads: list[TmsLoad],
    *,
    origin: str,
    destination: str | None,
    pickup_date: date | None,
    now: datetime,
    limit: int = MAX_RESULTS,
) -> list[TmsLoad]:
    wanted_origin = parse_place(origin)
    wanted_destination = parse_place(destination) if destination else None
    ranked: list[tuple[float, datetime, str, TmsLoad]] = []
    for load in loads:
        if load.status != "A" or load.pickup < now - timedelta(hours=1):
            continue
        origin_score = location_score(wanted_origin, parse_place(load.origin))
        if origin_score == 0:
            continue
        if pickup_date is not None:
            days_off = abs((load.pickup.date() - pickup_date).days)
            if days_off > 2:
                continue
            date_score = (1.0, 0.6, 0.3)[days_off]
        else:
            days_out = max(0, (load.pickup.date() - now.astimezone(load.pickup.tzinfo).date()).days)
            date_score = max(0.0, 1.0 - 0.15 * days_out)
        destination_score = (
            location_score(wanted_destination, parse_place(load.destination)) if wanted_destination else 0.0
        )
        score = 0.55 * origin_score + 0.30 * destination_score + 0.15 * date_score
        ranked.append((score, load.pickup, load.load_id, load))
    ranked.sort(key=lambda r: (-r[0], r[1], r[2]))
    return [r[3] for r in ranked[:limit]]
