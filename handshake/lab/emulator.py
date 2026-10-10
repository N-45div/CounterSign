"""A Ring Partner API emulator that plays a lab scenario.

It answers the endpoints Handshake uses in the shapes the real API returned on
the Ring Developer Playground (Oct 2026): JSON:API device and history
resources, media downloads that answer 303 and then serve the file, 416
MEDIA_NOT_FOUND when there is no frame, and an empty-bodied 401 for a bad
token. Handshake's RingClient talks to it unchanged; only the base URL
differs.

Recording follows a Ring doorbell: frames exist during motion events (one a
second) and as periodic snapshots, never while the camera is offline, and
never in the future. Playing a scenario in the past makes the whole timeline
available at once; playing it from now lets it unfold in real time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import requests
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

from .render import render
from .scenarios import SCENARIOS, Scenario, Scene

FRAME_TOLERANCE_MS = 2_000
PAGE_SIZE = 10
MAX_UPLOAD_BYTES = 1_500_000


@dataclass(frozen=True)
class LabDevice:
    id: str = "ava1.ring.device.HANDSHAKELAB0DOORBELL"
    name: str = "Lab Doorbell"
    location_id: str = "ava1.ring.location.HANDSHAKELAB"
    account_id: str = "ava1.ring.account.HANDSHAKELAB"


@dataclass
class LabWorld:
    scenario: Scenario
    start_ms: int
    device: LabDevice = field(default_factory=LabDevice)
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    scenes_by_hash: dict[str, Scene] = field(default_factory=dict)  # lets tests know what a frame showed
    uploaded: dict[int, bytes] = field(default_factory=dict)  # frames the 3D lab rendered, by timestamp

    def at(self, seconds: float) -> int:
        return self.start_ms + int(seconds * 1000)

    def seconds(self, ms: int) -> float:
        return (ms - self.start_ms) / 1000

    def online(self) -> bool:
        return not self.scenario.scene_at(self.seconds(self.clock_ms())).offline

    def events(self) -> list[dict]:
        """Motion events so far, oldest first, as (id, subtype, start_ms, end_ms)."""
        now = self.clock_ms()
        spans = [("vehicle", s, e) for s, e in self.scenario.vehicles] + [("human", s, e) for s, e in self.scenario.people]
        out = []
        for i, (subtype, s, e) in enumerate(sorted(spans, key=lambda x: (x[1], x[0]))):
            start = self.at(s)
            if start > now:
                continue
            out.append({"id": f"ava1.ring.history.event.LAB{i:03d}{subtype.upper()}", "subtype": subtype,
                        "start": start, "end": min(self.at(e), now)})
        return out

    def frame_times(self) -> list[int]:
        return self._frame_times()

    def _frame_times(self) -> list[int]:
        now = self.clock_ms()
        ticks = set()
        for ev in self.events():
            ticks.update(range(ev["start"], ev["end"] + 1, 1000))
        step = self.scenario.snapshot_every_s
        t = 0.0
        while t <= self.scenario.duration_s:
            ticks.add(self.at(t))
            t += step
        return sorted(ms for ms in ticks if ms <= now and not self.scenario.scene_at(self.seconds(ms)).offline)

    def frame(self, at_ms: int) -> tuple[int, bytes]:
        """The 3D lab's frame for this moment if it uploaded one, else a drawn one."""
        scene = self.scenario.scene_at(self.seconds(at_ms))
        image = self.uploaded.get(at_ms) or render(scene, at_ms, self.device.name)
        self.scenes_by_hash[hashlib.sha256(image).hexdigest()] = scene
        return at_ms, image

    def upload(self, at_ms: int, image: bytes) -> None:
        if at_ms not in set(self._frame_times()):
            raise ValueError("not a moment this camera records")
        self.uploaded[at_ms] = image

    def timeline(self) -> dict:
        """Everything the 3D lab needs to animate the scenario and render the right frames."""
        sc = self.scenario
        finite = lambda x: None if x == float("inf") else x  # noqa: E731
        return {
            "scenario": sc.name, "title": sc.title, "story": sc.story,
            "expected": {"verdict": sc.expected[0], "reason": sc.expected[1]},
            "start_ms": self.start_ms, "claim_at_ms": self.at(sc.claim_at_s), "duration_s": sc.duration_s,
            "spans": {
                "vehicles": [[s, finite(e)] for s, e in sc.vehicles],
                "people": [[s, finite(e)] for s, e in sc.people],
                "parcels": [[s, finite(e)] for s, e in sc.parcels],
                "notes": [[s, finite(e), list(lines)] for s, e, lines in sc.notes],
                "offline": [[s, finite(e)] for s, e in sc.offline],
            },
            "ticks": [round(self.seconds(ms), 3) for ms in self._frame_times()],
        }

    def frame_near(self, at_ms: int) -> tuple[int, bytes] | None:
        times = [ms for ms in self._frame_times() if abs(ms - at_ms) <= FRAME_TOLERANCE_MS]
        return self.frame(min(times, key=lambda ms: abs(ms - at_ms))) if times else None

    def latest_frame(self, start_ms: int, end_ms: int) -> tuple[int, bytes] | None:
        times = [ms for ms in self._frame_times() if start_ms <= ms <= end_ms]
        return self.frame(times[-1]) if times else None

    def status_changes(self) -> list[tuple[int, bool]]:
        """(at_ms, online) for every offline stretch that has started."""
        now = self.clock_ms()
        changes = []
        for s, e in self.scenario.offline:
            if self.at(s) <= now:
                changes.append((self.at(s), False))
            if self.at(e) <= now:
                changes.append((self.at(e), True))
        return changes


class LabState:
    """The world currently playing; the lab UI swaps scenarios at runtime."""

    def __init__(self, clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.clock_ms = clock_ms
        self.world: LabWorld | None = None

    def play(self, name: str, start_ms: int | None = None) -> LabWorld:
        scenario = SCENARIOS[name]
        if start_ms is None:  # far enough in the past that the claim window and settling time are over
            start_ms = self.clock_ms() - int((scenario.duration_s + 900) * 1000)
        self.world = LabWorld(scenario, start_ms, clock_ms=self.clock_ms)
        return self.world


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _err(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"errors": [{"status": str(status), "code": code, "detail": detail}]}, status_code=status)


def _media_key(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode().rstrip("=")


def _from_key(key: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4)))


def send_status_webhooks(world: LabWorld, url: str, signing_key: str, post=requests.post) -> int:
    """Deliver device_offline/device_online events, signed like Ring's."""
    sent = 0
    for at_ms, online in world.status_changes():
        kind = "device_online" if online else "device_offline"
        body = json.dumps({
            "meta": {"version": "1.1", "time": _iso(at_ms), "request_id": f"lab-{kind}-{at_ms}",
                     "account_id": world.device.account_id},
            "data": {"id": f"{world.device.id}_{kind}_{at_ms}", "type": kind,
                     "attributes": {"source": world.device.id, "source_type": "devices", "timestamp": at_ms}},
        }, separators=(",", ":")).encode()
        sig = "sha256=" + hmac.new(signing_key.encode(), body, hashlib.sha256).hexdigest()
        post(url, data=body, headers={"Content-Type": "application/json", "X-Signature": sig}, timeout=10)
        sent += 1
    return sent


def create_emulator(state: LabState, tokens: set[str] | None = None) -> FastAPI:
    app = FastAPI(title="Handshake Lab: Ring Partner API emulator")

    def denied(authorization: str | None) -> Response | None:
        token = (authorization or "")[7:].strip() if (authorization or "").lower().startswith("bearer ") else ""
        if not token or (tokens is not None and token not in tokens):
            return Response(status_code=401)  # Ring answers a bad token with an empty body
        return None

    def device_or_404(device_id: str):
        world = state.world
        if world is None or device_id != world.device.id:
            return None, _err(404, "NOT_FOUND", f"device {device_id} not found")
        return world, None

    @app.get("/lab/scenarios")
    def scenarios():
        return [{"name": s.name, "title": s.title, "story": s.story, "claim_at_s": s.claim_at_s,
                 "expected": {"verdict": s.expected[0], "reason": s.expected[1]}} for s in SCENARIOS.values()]

    @app.post("/lab/play")
    def play(body: dict):
        if body.get("scenario") not in SCENARIOS:
            return _err(400, "UNKNOWN_SCENARIO", f"choose one of {sorted(SCENARIOS)}")
        world = state.play(body["scenario"], body.get("start_ms"))
        return {"scenario": world.scenario.name, "device_id": world.device.id, "start_ms": world.start_ms,
                "claim_at_ms": world.at(world.scenario.claim_at_s)}

    @app.get("/lab/timeline")
    def timeline():
        if state.world is None:
            return _err(404, "NOT_PLAYING", "play a scenario first")
        return state.world.timeline()

    @app.post("/lab/frames/{at_ms}")
    async def upload_frame(at_ms: int, request: Request):
        world = state.world
        if world is None:
            return _err(404, "NOT_PLAYING", "play a scenario first")
        image = await request.body()
        if len(image) > MAX_UPLOAD_BYTES:
            return _err(413, "TOO_LARGE", "frames are at most 1.5 MB")
        if not (image.startswith(b"\xff\xd8") or image.startswith(b"\x89PNG")):
            return _err(415, "NOT_AN_IMAGE", "send a JPEG or PNG")
        try:
            world.upload(at_ms, image)
        except ValueError as exc:
            return _err(400, "NOT_A_TICK", str(exc))
        return Response(status_code=204)

    @app.get("/v1/users/me")
    def me(authorization: str | None = Header(default=None)):
        if (r := denied(authorization)) is not None:
            return r
        account = state.world.device.account_id if state.world else LabDevice().account_id
        return {"data": {"type": "users", "id": account}}

    @app.get("/v1/devices")
    def devices(authorization: str | None = Header(default=None)):
        if (r := denied(authorization)) is not None:
            return r
        if state.world is None:
            return {"data": []}
        dev = state.world.device
        return {"data": [{"type": "devices", "id": dev.id, "attributes": {"name": dev.name},
                          "relationships": {"location": {"data": {"type": "locations", "id": dev.location_id}}}}],
                "included": [{"type": "locations", "id": dev.location_id,
                              "attributes": {"state": "KA", "country": "IN"}}]}

    @app.get("/v1/devices/{device_id}/status")
    def status(device_id: str, authorization: str | None = Header(default=None)):
        if (r := denied(authorization)) is not None:
            return r
        world, err = device_or_404(device_id)
        if err:
            return err
        return {"meta": {"time": _iso(state.clock_ms())},
                "data": {"type": "device-status", "id": f"{device_id}.status", "attributes": {"online": world.online()}}}

    @app.get("/v1/history/devices/{device_id}/events")
    def history(device_id: str, request: Request, authorization: str | None = Header(default=None)):
        if (r := denied(authorization)) is not None:
            return r
        world, err = device_or_404(device_id)
        if err:
            return err
        wanted = [t.strip() for t in (request.query_params.get("event_types") or "").split(",") if t.strip()]
        events = world.events()
        if wanted:
            events = [e for e in events if "motion" in wanted or f"motion.{e['subtype']}" in wanted]
        events.sort(key=lambda e: e["start"], reverse=True)
        key = request.query_params.get("page[key]")
        if key:
            cutoff = int(datetime.strptime(key, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
            events = [e for e in events if e["start"] < cutoff]
        page, rest = events[:PAGE_SIZE], events[PAGE_SIZE:]
        body = {"data": [{
            "type": "history-events", "id": e["id"],
            "attributes": {"event_type": "motion", "start": e["start"], "end": e["end"], "is_third_party_reviewed": False},
            "relationships": {"source": {"data": {"type": "devices", "id": device_id}}, "cv_detections": {"data": []}},
        } for e in page]}
        if rest:
            query = f"page[key]={_iso(page[-1]['start'])}" + (f"&event_types={','.join(wanted)}" if wanted else "")
            body["links"] = {"next": f"/v1/history/devices/{device_id}/events?{query}"}
        return body

    @app.post("/v1/devices/{device_id}/media/{kind}/download")
    async def download(device_id: str, kind: str, request: Request, authorization: str | None = Header(default=None)):
        if (r := denied(authorization)) is not None:
            return r
        world, err = device_or_404(device_id)
        if err:
            return err
        payload = await request.json()
        # Like the real API: a 303 to the media URL, which then serves the file or a 416.
        base = str(request.base_url).rstrip("/")
        return Response(status_code=303, headers={"Location": f"{base}/lab/media/{_media_key({'kind': kind, **payload})}"})

    @app.api_route("/lab/media/{key}", methods=["GET", "POST"])
    def media(key: str):
        world = state.world
        try:
            req = _from_key(key)
        except (ValueError, json.JSONDecodeError):
            return _err(404, "NOT_FOUND", "unknown media")
        if world is None or req.get("kind") != "image":
            return _err(416, "MEDIA_NOT_FOUND", "The lab records snapshots only")
        if req.get("type") == "at_timestamp":
            found = world.frame_near(int(req["timestamp"]))
        else:
            found = world.latest_frame(int(req["start_timestamp"]), int(req["end_timestamp"]))
        if found is None:
            return _err(416, "MEDIA_NOT_FOUND", "No media found in time range")
        at_ms, image = found
        return Response(content=image, media_type="image/jpeg", headers={"X-Media-Timestamp": str(at_ms)})

    return app
