"""Client for the Ring Partner API (api.amazonvision.com).

Covers what Handshake reads from a house: devices and their camera modules,
event history, image snapshots and recorded clips. Everything is read-only.

Two quirks of the API shape this client:

* Event history reports motion events only as "motion". The detection subtype
  (human, vehicle, animal, other_motion) is only visible by filtering, so
  `motion_events` queries once per subtype and tags each event with what it
  was found under.
* Media downloads can answer with a redirect. A redirected POST must not be
  replayed as a bare GET with our bearer token on another host, so redirects
  are followed by hand.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

import requests

API = "https://api.amazonvision.com"
MOTION_SUBTYPES = ("human", "vehicle", "animal", "other_motion")
TIMEOUT = 30


class RingError(Exception):
    def __init__(self, status: int, title: str = "", detail: str = "", code: str | None = None):
        self.status = status
        self.title = title
        self.detail = detail
        self.code = code
        super().__init__(f"Ring API {status}: {title or detail or 'error'}")


class RingAuthError(RingError):
    """The access token is missing, invalid or expired."""


@dataclass(frozen=True)
class Device:
    id: str
    name: str | None
    kind: str | None


@dataclass(frozen=True)
class HistoryEvent:
    id: str
    device_id: str
    event_type: str  # motion, ding or on_demand
    start_ms: int
    end_ms: int
    subtypes: frozenset[str] = frozenset()  # only set for motion events

    def is_(self, subtype: str) -> bool:
        return subtype in self.subtypes


@dataclass(frozen=True)
class Media:
    content: bytes
    content_type: str
    timestamp_ms: int | None
    length_ms: int | None
    partial: bool  # 206: part of the requested range was not recorded


class RingClient:
    def __init__(
        self,
        token: str | Callable[[], str],
        session: requests.Session | None = None,
        base_url: str = API,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._token = token if callable(token) else (lambda: token)
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._sleep = sleep

    # -- transport -----------------------------------------------------------

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _request(self, method: str, path: str, *, params=None, json=None, redirects=True) -> requests.Response:
        url = path if path.startswith("http") else self.base_url + path
        for attempt in range(self.max_retries + 1):
            resp = self.session.request(
                method, url, params=params, json=json, headers=self._auth(),
                timeout=TIMEOUT, allow_redirects=redirects,
            )
            if resp.status_code != 429 or attempt == self.max_retries:
                break
            retry_after = resp.headers.get("Retry-After")
            self._sleep(float(retry_after) if retry_after else 2 ** attempt)
        if resp.status_code >= 400:
            raise _error_from(resp)
        return resp

    def _get_json(self, path: str, params=None) -> dict:
        return self._request("GET", path, params=params).json()

    # -- devices -------------------------------------------------------------

    def devices(self) -> list[Device]:
        body = self._get_json("/v1/devices")
        out = []
        for item in body.get("data", []):
            attrs = item.get("attributes", {})
            out.append(Device(
                id=item["id"],
                name=attrs.get("name") or attrs.get("description"),
                kind=attrs.get("type") or attrs.get("kind") or attrs.get("device_type"),
            ))
        return out

    def online(self, device_id: str) -> bool | None:
        """Whether the device is connected right now (None if Ring does not say)."""
        attrs = self._get_json(f"/v1/devices/{device_id}/status").get("data", {}).get("attributes", {})
        value = attrs.get("online")
        return value if isinstance(value, bool) else None

    def capabilities(self, device_id: str) -> dict:
        return self._get_json(f"/v1/devices/{device_id}/capabilities")

    def components(self, device_id: str) -> list[str]:
        """Camera module ids for a multi-camera device, or [] for a single camera."""
        attrs = self.capabilities(device_id).get("data", {}).get("attributes", {})
        items = (attrs.get("components") or {}).get("items", [])
        return [str(c["component_id"]) for c in items]

    # -- history -------------------------------------------------------------

    def history(
        self,
        device_id: str,
        event_types: Iterable[str] | None = None,
        *,
        since_ms: int | None = None,
        max_pages: int = 10,
    ) -> list[HistoryEvent]:
        """Events newest first. Stops paging once events are older than since_ms."""
        path = f"/v1/history/devices/{device_id}/events"
        params = {"event_types": ",".join(event_types)} if event_types else None
        events: list[HistoryEvent] = []
        for _ in range(max_pages):
            body = self._get_json(path, params)
            page = [_parse_event(item, device_id) for item in body.get("data", [])]
            events.extend(page)
            next_link = (body.get("links") or {}).get("next")
            reached_start = since_ms is not None and page and page[-1].start_ms < since_ms
            if not page or not next_link or reached_start:
                break
            path, params = next_link, None  # links.next already carries filters and cursor
        if since_ms is not None:
            events = [e for e in events if e.end_ms >= since_ms]
        return events

    def motion_events(self, device_id: str, since_ms: int, until_ms: int | None = None) -> list[HistoryEvent]:
        """Motion events in [since_ms, until_ms], oldest first, tagged with every subtype they matched.

        Ring does not always apply the filter: the Developer Playground answers a
        motion.vehicle query with its live-view (on_demand) events too. Anything
        that is not a motion event is dropped, so a live view never counts as a
        person or a vehicle at the door.
        """
        found: dict[str, HistoryEvent] = {}
        for subtype in MOTION_SUBTYPES:
            for event in self.history(device_id, [f"motion.{subtype}"], since_ms=since_ms):
                if event.event_type != "motion":
                    continue
                if until_ms is not None and event.start_ms > until_ms:
                    continue
                prior = found.get(event.id)
                tags = (prior.subtypes if prior else frozenset()) | {subtype}
                found[event.id] = HistoryEvent(
                    event.id, event.device_id, event.event_type, event.start_ms, event.end_ms, tags
                )
        return sorted(found.values(), key=lambda e: (e.start_ms, e.id))

    # -- media ---------------------------------------------------------------

    def snapshot_at(self, device_id: str, at_ms: int, component_id: str | None = None) -> Media:
        return self._download(device_id, "image", {"type": "at_timestamp", "timestamp": at_ms}, component_id)

    def snapshot_latest(self, device_id: str, start_ms: int, end_ms: int, component_id: str | None = None) -> Media:
        payload = {"type": "latest_in_range", "start_timestamp": start_ms, "end_timestamp": end_ms}
        return self._download(device_id, "image", payload, component_id)

    def clip(self, device_id: str, start_ms: int, duration_ms: int, component_id: str | None = None) -> Media:
        if not 0 < duration_ms <= 900_000:
            raise ValueError("Ring clips are 1 ms to 15 minutes long")
        return self._download(device_id, "video", {"timestamp": start_ms, "duration": duration_ms}, component_id)

    def _download(self, device_id: str, kind: str, payload: dict, component_id: str | None) -> Media:
        if component_id is not None:
            payload = {**payload, "components": [{"component_id": str(component_id)}]}
        path = f"/v1/devices/{device_id}/media/{kind}/download"
        resp = self._request("POST", path, json=payload, redirects=False)
        hops = 0
        while resp.is_redirect and hops < 3:
            hops += 1
            location = resp.headers["Location"]
            # A pre-signed media URL takes no auth; anything else gets the POST replayed.
            plain = self.session.get(location, timeout=TIMEOUT)
            resp = plain if plain.ok else self._request("POST", location, json=payload, redirects=False)
        if resp.status_code >= 400:
            raise _error_from(resp)
        return Media(
            content=resp.content,
            content_type=resp.headers.get("Content-Type", "application/octet-stream"),
            timestamp_ms=_int_or_none(resp.headers.get("X-Media-Timestamp")),
            length_ms=_int_or_none(resp.headers.get("X-Media-Length")),
            partial=resp.status_code == 206,
        )


def _parse_event(item: dict, device_id: str) -> HistoryEvent:
    attrs = item.get("attributes", {})
    source = ((item.get("relationships") or {}).get("source") or {}).get("data") or {}
    start = int(float(attrs["start"]))
    end = int(float(attrs.get("end") or start))
    return HistoryEvent(
        id=item["id"],
        device_id=source.get("id", device_id),
        event_type=attrs.get("event_type", "unknown"),
        start_ms=start,
        end_ms=max(end, start),
    )


def _error_from(resp: requests.Response) -> RingError:
    title = detail = ""
    code = None
    try:
        err = (resp.json().get("errors") or [{}])[0]
        title, detail, code = err.get("title", ""), err.get("detail", ""), err.get("code")
    except (ValueError, AttributeError):
        detail = resp.text[:500]
    cls = RingAuthError if resp.status_code == 401 else RingError
    return cls(resp.status_code, title, detail, code)


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
