"""Seed loads for the simulated TMS.

Pickup dates are generated relative to "now", so the demo never goes stale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.tms.protocol import TmsLoad

CENTRAL = "America/Chicago"
EASTERN = "America/New_York"
INDIANA = "America/Indiana/Indianapolis"
MOUNTAIN = "America/Denver"
ARIZONA = "America/Phoenix"
PACIFIC = "America/Los_Angeles"


@dataclass(frozen=True)
class _Seed:
    load_id: str
    origin: str
    origin_tz: str
    destination: str
    destination_tz: str
    day: int  # days from today
    hour: int  # local pickup hour
    equipment: str
    miles: int
    loadboard: int  # dollars
    max_rate: int  # dollars, internal only
    weight: int
    commodity: str
    pieces: int
    dimensions: str
    notes: str
    status: str = "A"


_SEEDS = (
    # Chicago-area outbound: the main demo lane is Joliet, IL -> Dallas, TX (dry van).
    _Seed("LD-10482", "Joliet, IL", CENTRAL, "Dallas, TX", CENTRAL, 0, 8, "dry_van", 925, 2100, 2400,
          42000, "Paper goods", 24, "48x40x60", "Driver assist not required"),
    _Seed("LD-10499", "Joliet, IL", CENTRAL, "Dallas, TX", CENTRAL, 0, 10, "dry_van", 925, 2050, 2300,
          39000, "Canned goods", 22, "48x40x52", "Already covered", status="B"),
    _Seed("LD-10542", "Chicago, IL", CENTRAL, "Dallas, TX", CENTRAL, 1, 7, "dry_van", 970, 2200, 2500,
          35000, "Household goods", 30, "48x40x48", "Lumper fee reimbursed with receipt"),
    _Seed("LD-10546", "Bolingbrook, IL", CENTRAL, "Houston, TX", CENTRAL, 2, 6, "dry_van", 1080, 2450, 2750,
          28000, "Paper towels", 26, "48x40x72", "Drop and hook at receiver"),
    _Seed("LD-10487", "Joliet, IL", CENTRAL, "Memphis, TN", CENTRAL, 1, 10, "dry_van", 530, 1450, 1650,
          30000, "Consumer electronics", 18, "48x40x60", "High value: no unattended stops"),
    _Seed("LD-10483", "Chicago, IL", CENTRAL, "Atlanta, GA", EASTERN, 0, 14, "reefer", 717, 2150, 2450,
          38000, "Frozen poultry", 20, "48x40x50", "Continuous -10F, seal required"),
    _Seed("LD-10539", "Joliet, IL", CENTRAL, "Dallas, TX", CENTRAL, 2, 9, "reefer", 925, 2450, 2750,
          41000, "Frozen pizza", 22, "48x40x55", "Set reefer to 0F, pulp temps at pickup"),
    _Seed("LD-10491", "Aurora, IL", CENTRAL, "Indianapolis, IN", INDIANA, 1, 7, "flatbed", 205, 850, 975,
          44000, "Steel coils", 6, "60x60x48", "Tarps and chains required"),
    _Seed("LD-10553", "Memphis, TN", CENTRAL, "Chicago, IL", CENTRAL, 1, 11, "dry_van", 530, 1400, 1600,
          31000, "Paper products", 24, "48x40x60", "FCFS dock 06:00-14:00"),
    # Texas
    _Seed("LD-10495", "Dallas, TX", CENTRAL, "Houston, TX", CENTRAL, 0, 13, "dry_van", 240, 750, 850,
          43000, "Bottled beverages", 22, "48x40x52", "Appointment required at receiver"),
    _Seed("LD-10502", "Dallas, TX", CENTRAL, "Phoenix, AZ", ARIZONA, 1, 6, "reefer", 1065, 2900, 3250,
          40000, "Fresh produce", 26, "48x40x60", "Pre-cool to 34F"),
    _Seed("LD-10506", "Fort Worth, TX", CENTRAL, "Oklahoma City, OK", CENTRAL, 1, 8, "flatbed", 200, 800, 900,
          45000, "Lumber", 12, "96x48x36", "Straps and edge protectors required"),
    # Southeast
    _Seed("LD-10511", "Atlanta, GA", EASTERN, "Charlotte, NC", EASTERN, 0, 15, "dry_van", 245, 725, 825,
          18000, "Retail apparel", 30, "48x40x60", "Live unload, 2 hr free time"),
    _Seed("LD-10515", "Atlanta, GA", EASTERN, "Miami, FL", EASTERN, 1, 18, "reefer", 660, 1850, 2100,
          39000, "Dairy products", 22, "48x40x50", "Continuous 34F"),
    _Seed("LD-10531", "Columbus, OH", EASTERN, "Nashville, TN", CENTRAL, 0, 16, "reefer", 380, 1400, 1575,
          15000, "Pharmaceuticals", 12, "48x40x40", "36-46F, temp recorder required"),
    # Northeast
    _Seed("LD-10528", "Newark, NJ", EASTERN, "Boston, MA", EASTERN, 1, 6, "dry_van", 225, 900, 1025,
          36000, "Packaged foods", 24, "48x40x60", "Appointment 06:00, no early arrivals"),
    # West / Mountain
    _Seed("LD-10520", "Los Angeles, CA", PACIFIC, "Phoenix, AZ", ARIZONA, 1, 5, "dry_van", 370, 1150, 1300,
          25000, "Furniture", 40, "blanket wrap", "Load bars required"),
    _Seed("LD-10524", "Ontario, CA", PACIFIC, "Las Vegas, NV", PACIFIC, 2, 7, "flatbed", 230, 950, 1075,
          46000, "Building materials", 10, "96x48x40", "Tarps required"),
    _Seed("LD-10535", "Denver, CO", MOUNTAIN, "Salt Lake City, UT", MOUNTAIN, 2, 8, "dry_van", 520, 1500, 1700,
          33000, "Auto parts", 28, "48x40x60", "Hazmat: no"),
    _Seed("LD-10550", "Kansas City, MO", CENTRAL, "Denver, CO", MOUNTAIN, 1, 7, "flatbed", 605, 1750, 2000,
          40000, "Farm machinery", 4, "240x96x110", "Chains and binders required"),
)


def build_loads(now: datetime | None = None) -> list[TmsLoad]:
    now = now or datetime.now(timezone.utc)
    return [_materialize(seed, now) for seed in _SEEDS]


def _materialize(seed: _Seed, now: datetime) -> TmsLoad:
    pickup = _pickup_time(now.astimezone(ZoneInfo(seed.origin_tz)), seed.day, seed.hour)
    transit = timedelta(hours=math.ceil(seed.miles / 45) + 2 + 10 * (seed.miles // 550))  # driving + HOS rest
    delivery = (pickup + transit).astimezone(ZoneInfo(seed.destination_tz))
    return TmsLoad(
        load_id=seed.load_id,
        status=seed.status,
        origin=seed.origin,
        destination=seed.destination,
        pickup=pickup,
        delivery=delivery,
        equipment_type=seed.equipment,
        loadboard_cents=seed.loadboard * 100,
        max_rate_cents=seed.max_rate * 100,
        weight_lbs=seed.weight,
        commodity=seed.commodity,
        pieces=seed.pieces,
        miles=seed.miles,
        dimensions=seed.dimensions,
        notes=seed.notes,
    )


def _pickup_time(local_now: datetime, day: int, hour: int) -> datetime:
    """Scheduled slot, or the next whole hour at least 2 h out if the slot already passed."""
    target_day = local_now.date() + timedelta(days=day)
    pickup = datetime.combine(target_day, time(hour), tzinfo=local_now.tzinfo)
    earliest = local_now + timedelta(hours=2)
    if pickup >= earliest:
        return pickup
    rounded = (earliest + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    if rounded.date() == target_day and rounded.hour <= 21:
        return rounded
    return datetime.combine(target_day + timedelta(days=1), time(7), tzinfo=local_now.tzinfo)
