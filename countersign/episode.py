"""Turn Ring motion events into delivery visits around a claimed delivery time.

A delivery at a door usually looks like: a vehicle event, one or more human
events while someone walks up and back, then another vehicle event. Ring has
no "package" event, so this module only finds the visit and picks the two
moments worth photographing: just before anyone arrived, and just after they
left. Whether a package appeared between those two frames is the witness's
job (see witness.py).

Vehicle events are supporting evidence, not a requirement: many deliveries in
India come by scooter or on foot from the building's gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .ring import HistoryEvent

MINUTE = 60_000

# Couriers usually tap "delivered" after the drop, sometimes a little before.
LOOK_BEFORE_CLAIM_MS = 20 * MINUTE
LOOK_AFTER_CLAIM_MS = 10 * MINUTE
# Human events closer together than this are one visit (walk up, pause, walk back).
MERGE_GAP_MS = 90_000
# How far a vehicle event may sit from the visit and still count as the courier's.
VEHICLE_REACH_MS = 3 * MINUTE
# Offset of the before/after snapshots from the visit edges.
SNAPSHOT_MARGIN_MS = 5_000


@dataclass(frozen=True)
class Claim:
    order_id: str
    merchant: str
    claimed_at_ms: int
    carrier: str | None = None


@dataclass(frozen=True)
class Window:
    start_ms: int
    end_ms: int

    def overlaps(self, start_ms: int, end_ms: int) -> bool:
        return start_ms <= self.end_ms and end_ms >= self.start_ms


def claim_window(claim: Claim) -> Window:
    return Window(claim.claimed_at_ms - LOOK_BEFORE_CLAIM_MS, claim.claimed_at_ms + LOOK_AFTER_CLAIM_MS)


@dataclass(frozen=True)
class Episode:
    visit: tuple[HistoryEvent, ...]  # merged human events, oldest first
    arrival: HistoryEvent | None
    departure: HistoryEvent | None
    before_ms: int  # snapshot: the doorstep before this visit
    after_ms: int  # snapshot: the doorstep after it, before anyone else came

    @property
    def start_ms(self) -> int:
        return self.visit[0].start_ms

    @property
    def end_ms(self) -> int:
        return max(e.end_ms for e in self.visit)

    @property
    def event_ids(self) -> tuple[str, ...]:
        ids = [e.id for e in self.visit]
        for extra in (self.arrival, self.departure):
            if extra is not None and extra.id not in ids:
                ids.append(extra.id)
        return tuple(ids)

    def distance_ms(self, claimed_at_ms: int) -> float:
        """How far the claim sits from the visit. Visits after the claim cost more."""
        if self.start_ms <= claimed_at_ms <= self.end_ms:
            return 0
        if claimed_at_ms > self.end_ms:
            return claimed_at_ms - self.end_ms
        return (self.start_ms - claimed_at_ms) * 1.5


@dataclass(frozen=True)
class Observation:
    claim: Claim
    window: Window
    episodes: tuple[Episode, ...]  # best first
    other_activity: tuple[HistoryEvent, ...]  # motion in the window with no human in it

    @property
    def best(self) -> Episode | None:
        return self.episodes[0] if self.episodes else None


def _visits(humans: list[HistoryEvent]) -> list[list[HistoryEvent]]:
    visits: list[list[HistoryEvent]] = []
    for event in humans:
        if visits and event.start_ms - max(e.end_ms for e in visits[-1]) <= MERGE_GAP_MS:
            visits[-1].append(event)
        else:
            visits.append([event])
    return visits


def _arrival(vehicles: list[HistoryEvent], start_ms: int) -> HistoryEvent | None:
    near = [v for v in vehicles if v.start_ms <= start_ms and v.end_ms >= start_ms - VEHICLE_REACH_MS]
    return max(near, key=lambda v: v.start_ms, default=None)


def _departure(vehicles: list[HistoryEvent], end_ms: int) -> HistoryEvent | None:
    near = [v for v in vehicles if v.end_ms >= end_ms and v.start_ms <= end_ms + VEHICLE_REACH_MS]
    return min(near, key=lambda v: v.start_ms, default=None)


def observe(claim: Claim, events: list[HistoryEvent]) -> Observation:
    """Find the visits that could be this delivery.

    `events` should cover a little more than the claim window so the
    neighbouring visits are known: the after-snapshot must be taken before the
    next person (often the resident, picking the parcel up) reaches the door.
    """
    window = claim_window(claim)
    ordered = sorted(events, key=lambda e: (e.start_ms, e.id))
    humans = [e for e in ordered if e.is_("human")]
    vehicles = [e for e in ordered if e.is_("vehicle")]
    visits = _visits(humans)

    episodes = []
    for i, visit in enumerate(visits):
        start, end = visit[0].start_ms, max(e.end_ms for e in visit)
        if not window.overlaps(start, end):
            continue
        arrival = _arrival(vehicles, start)
        departure = _departure(vehicles, end)
        first_seen = min(start, arrival.start_ms) if arrival else start
        prev_end = max(e.end_ms for e in visits[i - 1]) if i > 0 else None
        next_start = visits[i + 1][0].start_ms if i + 1 < len(visits) else None

        # The after-frame waits for the courier's vehicle to leave too (up to a cap):
        # a van marked "PARCELS" in the frame was read as a parcel at the door.
        last_seen = min(max(end, departure.end_ms), end + VEHICLE_REACH_MS) if departure else end

        before = first_seen - SNAPSHOT_MARGIN_MS
        if prev_end is not None:
            before = max(before, prev_end + 1)
        after = last_seen + SNAPSHOT_MARGIN_MS
        if next_start is not None:
            after = min(after, next_start - 1)
        episodes.append(Episode(tuple(visit), arrival, departure, before, max(after, end)))

    episodes.sort(key=lambda ep: (ep.distance_ms(claim.claimed_at_ms), ep.start_ms))
    in_episode = {eid for ep in episodes for eid in ep.event_ids}
    other = tuple(
        e for e in ordered
        if e.event_type == "motion" and e.id not in in_episode and not e.is_("human")
        and window.overlaps(e.start_ms, e.end_ms)
    )
    return Observation(claim, window, tuple(episodes), other)
