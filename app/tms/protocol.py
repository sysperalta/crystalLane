"""Wire format of Crystal Lane's legacy TMS (simulated by tms_sim/).

Line-oriented ASCII over TCP, CRLF-terminated, one short-lived connection per request:

    C: AUTH <token>                                   S: OK | ERR AUTH
    C: LIST <DV|RF|FB|*>                              S: OK <n>, n LOAD records, END
    C: BOOK <load_id> <mc> <rate_cents> <idem_key>    S: OK BOOKED <ref> | OK UNAVAILABLE | ERR NOT_FOUND
    C: PING                                           S: PONG
    C: RESET   (simulator only)                       S: OK
    C: QUIT

A LOAD record is exactly RECORD_LENGTH characters, fields in FIELDS order, text
left-aligned and space-padded, numbers zero-padded. Money is integer cents; datetimes are
YYYYMMDDHHMM+HHMM in the stop's local time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

FIELDS: tuple[tuple[str, int], ...] = (
    ("load_id", 10),
    ("status", 1),
    ("origin", 30),
    ("destination", 30),
    ("pickup", 17),
    ("delivery", 17),
    ("equipment", 2),
    ("loadboard_cents", 9),
    ("max_rate_cents", 9),
    ("weight_lbs", 7),
    ("commodity", 20),
    ("pieces", 4),
    ("miles", 5),
    ("dimensions", 12),
    ("notes", 40),
)
RECORD_LENGTH = sum(width for _, width in FIELDS)

CODE_TO_EQUIPMENT = {"DV": "dry_van", "RF": "reefer", "FB": "flatbed"}
EQUIPMENT_TO_CODE = {name: code for code, name in CODE_TO_EQUIPMENT.items()}
STATUS_CODES = {"A": "available", "B": "booked", "H": "on_hold"}
DATETIME_FORMAT = "%Y%m%d%H%M%z"
LOAD_ID_RE = re.compile(r"^LD-[0-9]{3,7}$")
BOOKING_REF_RE = re.compile(r"^BK-[0-9]{3,9}$")

_PLACE_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]*, [A-Z]{2}$")
_COUNT_FIELDS = ("weight_lbs", "pieces", "miles")


class MalformedRecord(ValueError):
    """A record failed validation. Messages never contain field values."""


@dataclass(frozen=True, slots=True)
class TmsLoad:
    load_id: str
    status: str
    origin: str
    destination: str
    pickup: datetime
    delivery: datetime
    equipment_type: str
    loadboard_cents: int
    # Internal ceiling for negotiation. Kept out of repr so it can't leak into logs.
    max_rate_cents: int = field(repr=False)
    weight_lbs: int
    commodity: str
    pieces: int
    miles: int
    dimensions: str
    notes: str


def encode_record(load: TmsLoad) -> str:
    values: dict[str, str | int] = {
        "load_id": load.load_id,
        "status": load.status,
        "origin": load.origin,
        "destination": load.destination,
        "pickup": load.pickup.strftime(DATETIME_FORMAT),
        "delivery": load.delivery.strftime(DATETIME_FORMAT),
        "equipment": EQUIPMENT_TO_CODE[load.equipment_type],
        "loadboard_cents": load.loadboard_cents,
        "max_rate_cents": load.max_rate_cents,
        "weight_lbs": load.weight_lbs,
        "commodity": load.commodity,
        "pieces": load.pieces,
        "miles": load.miles,
        "dimensions": load.dimensions,
        "notes": load.notes,
    }
    parts = []
    for name, width in FIELDS:
        value = values[name]
        text = str(value).zfill(width) if isinstance(value, int) else str(value).ljust(width)
        if len(text) != width:
            raise ValueError(f"{name} does not fit in {width} characters")
        parts.append(text)
    return "".join(parts)


def parse_record(line: str) -> TmsLoad:
    """Validate every fixed-width field; raise MalformedRecord on the first bad one."""
    if len(line) != RECORD_LENGTH:
        raise MalformedRecord("wrong record length")
    if not (line.isascii() and line.isprintable()):
        raise MalformedRecord("non-printable characters")

    raw: dict[str, str] = {}
    position = 0
    for name, width in FIELDS:
        raw[name] = line[position : position + width]
        position += width

    load_id = raw["load_id"].strip()
    if not LOAD_ID_RE.match(load_id):
        raise MalformedRecord("bad load_id")
    if raw["status"] not in STATUS_CODES:
        raise MalformedRecord("bad status")
    origin, destination = raw["origin"].strip(), raw["destination"].strip()
    if not (_PLACE_RE.match(origin) and _PLACE_RE.match(destination)):
        raise MalformedRecord("bad origin or destination")
    try:
        pickup = datetime.strptime(raw["pickup"], DATETIME_FORMAT)
        delivery = datetime.strptime(raw["delivery"], DATETIME_FORMAT)
    except ValueError:
        raise MalformedRecord("bad datetime") from None
    if delivery <= pickup:
        raise MalformedRecord("delivery not after pickup")
    equipment = CODE_TO_EQUIPMENT.get(raw["equipment"])
    if equipment is None:
        raise MalformedRecord("bad equipment code")

    if not (raw["loadboard_cents"].isdigit() and raw["max_rate_cents"].isdigit()):
        raise MalformedRecord("rate field not numeric")
    loadboard, ceiling = int(raw["loadboard_cents"]), int(raw["max_rate_cents"])
    if loadboard <= 0 or ceiling < loadboard:
        raise MalformedRecord("rate fields inconsistent")
    counts: dict[str, int] = {}
    for name in _COUNT_FIELDS:
        if not raw[name].isdigit():
            raise MalformedRecord(f"{name} not numeric")
        counts[name] = int(raw[name])
    if counts["weight_lbs"] <= 0 or counts["miles"] <= 0:
        raise MalformedRecord("weight or miles not positive")

    return TmsLoad(
        load_id=load_id,
        status=raw["status"],
        origin=origin,
        destination=destination,
        pickup=pickup,
        delivery=delivery,
        equipment_type=equipment,
        loadboard_cents=loadboard,
        max_rate_cents=ceiling,
        weight_lbs=counts["weight_lbs"],
        commodity=raw["commodity"].strip(),
        pieces=counts["pieces"],
        miles=counts["miles"],
        dimensions=raw["dimensions"].strip(),
        notes=raw["notes"].strip(),
    )
