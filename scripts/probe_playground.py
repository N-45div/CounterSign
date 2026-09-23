"""Probe what the Ring Developer Playground token can actually reach.

Handshake builds a delivery episode out of Ring data: a vehicle arrives, a
person walks up, a package is left, the vehicle leaves. Before building on it,
this script checks which of those pieces the Playground exposes: devices,
event history (per motion subtype), image snapshots and recorded clips.

Usage:
    python scripts/probe_playground.py --token "eyJ..."
    RING_TOKEN="eyJ..." python scripts/probe_playground.py

The token can also be pasted as the whole `export TOKEN="eyJ..."` line the
Playground shows. It is never written to disk or printed.

Raw responses, snapshots and clips land in probe-out/<utc-stamp>/, which is
gitignored.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API = "https://api.amazonvision.com"
TIMEOUT = 30
MOTION_SUBTYPES = ["motion.vehicle", "motion.human", "motion.animal", "motion.other_motion"]
OTHER_TYPES = ["ding", "on_demand"]
MAX_DEVICES = 5
MAX_HISTORY_PAGES = 3


def clean_token(raw: str) -> str:
    raw = raw.strip()
    match = re.search(r"TOKEN=[\"']?([^\"'\s]+)", raw)
    return match.group(1) if match else raw.strip("\"'")


def jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


class Probe:
    def __init__(self, token: str, out_dir: Path):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.out = out_dir
        self.notes: list[str] = []

    def save_json(self, name: str, body) -> None:
        (self.out / f"{name}.json").write_text(json.dumps(body, indent=2), encoding="utf-8")

    def get(self, path: str, params: dict | None = None, name: str | None = None):
        resp = self.session.get(API + path, params=params, timeout=TIMEOUT)
        body = _json_or_text(resp)
        if name:
            self.save_json(name, {"status": resp.status_code, "body": body})
        return resp.status_code, body

    def download(self, device_id: str, kind: str, payload: dict, name: str) -> dict:
        """POST a media download and follow redirects by hand.

        The docs say downloads may answer with a 301. requests would turn a
        redirected POST into a GET and drop the Authorization header on a new
        host, so the redirect is handled explicitly and we record what worked.
        """
        url = f"{API}/v1/devices/{device_id}/media/{kind}/download"
        resp = self.session.post(url, json=payload, timeout=TIMEOUT, allow_redirects=False)
        hops = []
        while resp.is_redirect and len(hops) < 3:
            location = resp.headers["Location"]
            hops.append(resp.status_code)
            plain = requests.get(location, timeout=TIMEOUT)
            resp = plain if plain.ok else self.session.post(
                location, json=payload, timeout=TIMEOUT, allow_redirects=False
            )
        result = {
            "status": resp.status_code,
            "redirects": hops,
            "content_type": resp.headers.get("Content-Type"),
            "media_timestamp": resp.headers.get("X-Media-Timestamp"),
            "media_length_ms": resp.headers.get("X-Media-Length"),
            "bytes": len(resp.content),
            "request": payload,
        }
        if resp.ok and result["content_type"] and "json" not in result["content_type"]:
            ext = {"video/mp4": "mp4", "image/png": "png"}.get(result["content_type"], "jpg")
            (self.out / f"{name}.{ext}").write_bytes(resp.content)
            result["saved"] = f"{name}.{ext}"
        else:
            result["error"] = _json_or_text(resp)
        self.save_json(name, result)
        return result


def _json_or_text(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]


def history(probe: Probe, device_id: str, event_types: str | None, name: str) -> list[dict]:
    events, path, params = [], f"/v1/history/devices/{device_id}/events", {}
    if event_types:
        params["event_types"] = event_types
    for page in range(MAX_HISTORY_PAGES):
        status, body = probe.get(path, params=params, name=f"{name}_p{page}")
        if status != 200 or not isinstance(body, dict):
            probe.notes.append(f"history {event_types or 'all'} on {device_id}: HTTP {status}")
            break
        events.extend(body.get("data", []))
        nxt = (body.get("links") or {}).get("next")
        if not nxt or not body.get("data"):
            break
        path, params = nxt, None  # links.next already carries the filters and cursor
    return events


def summarise_device(probe: Probe, idx: int, device: dict) -> dict:
    device_id = device["id"]
    attrs = device.get("attributes", {})
    tag = f"dev{idx}"
    report = {"id": device_id, "name": attrs.get("name"), "type": attrs.get("type") or attrs.get("kind")}

    _, caps = probe.get(f"/v1/devices/{device_id}/capabilities", name=f"{tag}_capabilities")
    components = (((caps or {}).get("data") or {}).get("attributes") or {}).get("components") if isinstance(caps, dict) else None
    report["camera_modules"] = len((components or {}).get("items", [])) or 1

    all_events = history(probe, device_id, None, f"{tag}_history_all")
    report["events_total"] = len(all_events)
    by_type: dict[str, int] = {}
    for ev in all_events:
        kind = ev.get("attributes", {}).get("event_type", "?")
        by_type[kind] = by_type.get(kind, 0) + 1
    report["events_by_type"] = by_type

    # The history response only says "motion"; the subtype is only visible by filtering.
    report["motion_subtypes"] = {}
    for sub in MOTION_SUBTYPES:
        report["motion_subtypes"][sub] = len(history(probe, device_id, sub, f"{tag}_history_{sub}"))

    now_ms = int(time.time() * 1000)
    report["snapshot_latest_24h"] = probe.download(
        device_id, "image",
        {"type": "latest_in_range", "start_timestamp": now_ms - 86_400_000, "end_timestamp": now_ms},
        f"{tag}_snapshot_latest",
    )

    if all_events:
        latest = all_events[0]["attributes"]
        start = int(latest["start"])
        end = int(latest.get("end") or start + 10_000)
        duration = max(3_000, min(end - start, 15_000))
        report["latest_event"] = {"type": latest.get("event_type"), "start": start, "end": end}
        report["snapshot_at_event"] = probe.download(
            device_id, "image", {"type": "at_timestamp", "timestamp": start}, f"{tag}_snapshot_event"
        )
        report["clip_at_event"] = probe.download(
            device_id, "video", {"timestamp": start, "duration": duration}, f"{tag}_clip_event"
        )
    else:
        # No history: ask for the last 10 seconds instead, to learn whether clips exist at all.
        report["clip_last_10s"] = probe.download(
            device_id, "video", {"timestamp": now_ms - 12_000, "duration": 10_000}, f"{tag}_clip_recent"
        )
    return report


def verdict(reports: list[dict]) -> list[str]:
    def any_of(pred):
        return any(pred(r) for r in reports)

    def ok(r, key):
        return (r.get(key) or {}).get("status") in (200, 206) and (r.get(key) or {}).get("bytes", 0) > 0

    return [
        f"devices visible:            {'YES' if reports else 'NO'} ({len(reports)})",
        f"vehicle motion in history:  {'YES' if any_of(lambda r: r['motion_subtypes'].get('motion.vehicle')) else 'NO'}",
        f"human motion in history:    {'YES' if any_of(lambda r: r['motion_subtypes'].get('motion.human')) else 'NO'}",
        f"snapshots download:         {'YES' if any_of(lambda r: ok(r, 'snapshot_latest_24h') or ok(r, 'snapshot_at_event')) else 'NO'}",
        f"clips download:             {'YES' if any_of(lambda r: ok(r, 'clip_at_event') or ok(r, 'clip_last_10s')) else 'NO'}",
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--token", default=os.environ.get("RING_TOKEN", ""))
    args = parser.parse_args()
    token = clean_token(args.token)
    if not token:
        print("No token. Pass --token or set RING_TOKEN (Playground > Generate Token).")
        return 2
    if not re.fullmatch(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", token):
        print("That doesn't look like a Ring token. Copy it from Playground > Generate Token;")
        print("it starts with eyJ and has two dots. The whole `export TOKEN=\"...\"` line also works.")
        return 2

    claims = jwt_claims(token)
    exp = claims.get("exp")
    if exp and exp < time.time():
        print(f"Token expired at {datetime.fromtimestamp(exp, timezone.utc):%H:%M UTC}. Generate a fresh one.")
        return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(__file__).resolve().parent.parent / "probe-out" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    probe = Probe(token, out_dir)
    probe.save_json("token_claims", {k: v for k, v in claims.items() if k in ("app_id", "exp", "iat", "scope", "iss", "aud")})

    status, me = probe.get("/v1/users/me", name="users_me")
    print(f"users/me: HTTP {status}")
    if status == 401:
        print("401 Unauthorized: the token is invalid or expired.")
        return 1

    status, devices = probe.get(
        "/v1/devices", params={"include": "status,capabilities,location,configurations"}, name="devices"
    )
    print(f"devices:  HTTP {status}")
    device_list = devices.get("data", []) if isinstance(devices, dict) else []

    reports = []
    for idx, device in enumerate(device_list[:MAX_DEVICES]):
        print(f"probing device {idx + 1}/{min(len(device_list), MAX_DEVICES)} ...")
        reports.append(summarise_device(probe, idx, device))

    probe.save_json("summary", {"devices": reports, "notes": probe.notes})
    print()
    for r in reports:
        print(json.dumps({k: v for k, v in r.items() if not isinstance(v, dict) or k in ("events_by_type", "motion_subtypes")}))
    print()
    print("\n".join(verdict(reports)))
    for note in probe.notes:
        print(f"note: {note}")
    print(f"\nEverything saved to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
