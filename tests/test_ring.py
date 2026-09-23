import pytest

from handshake.ring import RingAuthError, RingClient, RingError

from .fakes import FakeSession, response

DEV = "ava1.ring.device.FRONT"


def event(event_id: str, start: int, end: int, kind: str = "motion") -> dict:
    return {
        "type": "history-events",
        "id": event_id,
        "attributes": {"event_type": kind, "is_third_party_reviewed": False, "start": start, "end": end},
        "relationships": {"source": {"data": {"type": "devices", "id": DEV}}},
    }


def client(session: FakeSession, **kwargs) -> RingClient:
    return RingClient("tok", session=session, sleep=lambda s: None, **kwargs)


def test_sends_bearer_token_and_parses_devices():
    s = FakeSession().on("GET", "/v1/devices", lambda c: response(200, {
        "data": [{"type": "devices", "id": DEV, "attributes": {"name": "Front Door", "type": "doorbell"}}]
    }))
    devices = client(s).devices()
    assert devices[0].id == DEV and devices[0].name == "Front Door" and devices[0].kind == "doorbell"
    assert s.calls[0]["headers"]["Authorization"] == "Bearer tok"


def test_history_follows_next_link_and_stops_at_since():
    path = f"/v1/history/devices/{DEV}/events"
    pages = [
        response(200, {"data": [event("e3", 3000, 3500), event("e2", 2000, 2500)],
                       "links": {"next": f"{path}?page[key]=2026-09-01T00:00:00Z"}}),
        response(200, {"data": [event("e1", 1000, 1500)],
                       "links": {"next": f"{path}?page[key]=2026-08-31T00:00:00Z"}}),
        response(200, {"data": [event("e0", 10, 20)]}),
    ]
    s = FakeSession().on("GET", path, pages)
    events = client(s).history(DEV, since_ms=1200)
    assert [e.id for e in events] == ["e3", "e2", "e1"]  # e1 ends after since, so it is kept
    assert len(s.calls) == 2  # stopped once a page reached back past since_ms
    assert s.calls[1]["params"] == {"page[key]": "2026-09-01T00:00:00Z"}


def test_motion_events_tags_subtypes_from_filtered_queries():
    path = f"/v1/history/devices/{DEV}/events"
    by_filter = {
        "motion.vehicle": [event("v1", 1000, 1900), event("both", 2000, 2600)],
        "motion.human": [event("h1", 2100, 2500), event("both", 2000, 2600)],
        "motion.animal": [],
        "motion.other_motion": [event("late", 9000, 9100)],
    }
    s = FakeSession().on("GET", path, lambda c: response(200, {"data": list(reversed(by_filter[c["params"]["event_types"]]))}))
    events = client(s).motion_events(DEV, since_ms=500, until_ms=5000)
    assert [e.id for e in events] == ["v1", "both", "h1"]  # oldest first, "late" is past until_ms
    both = next(e for e in events if e.id == "both")
    assert both.subtypes == {"vehicle", "human"}
    assert events[0].is_("vehicle") and not events[0].is_("human")


def test_retries_429_using_retry_after():
    waits = []
    s = FakeSession().on("GET", "/v1/devices", [
        response(429, {"errors": [{"status": "429", "title": "Too Many Requests"}]}, headers={"Retry-After": "2"}),
        response(200, {"data": []}),
    ])
    RingClient("tok", session=s, sleep=waits.append).devices()
    assert waits == [2.0]


def test_401_raises_auth_error_with_json_api_detail():
    s = FakeSession().on("GET", "/v1/devices", lambda c: response(401, {
        "errors": [{"status": "401", "title": "Unauthorized", "detail": "Token expired"}]
    }))
    with pytest.raises(RingAuthError) as err:
        client(s).devices()
    assert err.value.detail == "Token expired"


def test_clip_reports_partial_content_and_media_headers():
    s = FakeSession().on("POST", f"/v1/devices/{DEV}/media/video/download", lambda c: response(
        206, content=b"mp4", headers={"Content-Type": "video/mp4", "X-Media-Timestamp": "1500", "X-Media-Length": "3000"}
    ))
    media = client(s).clip(DEV, 1000, 5000, component_id="2")
    assert media.partial and media.timestamp_ms == 1500 and media.length_ms == 3000
    assert s.calls[0]["json"] == {"timestamp": 1000, "duration": 5000, "components": [{"component_id": "2"}]}


def test_download_follows_redirect_to_presigned_url_without_bearer():
    presigned = "https://media.example.com/clip.jpg?sig=abc"
    s = (FakeSession()
         .on("POST", f"/v1/devices/{DEV}/media/image/download",
             lambda c: response(301, headers={"Location": presigned}))
         .on("GET", presigned, lambda c: response(200, content=b"jpeg", headers={"Content-Type": "image/jpeg"})))
    media = client(s).snapshot_at(DEV, 1234)
    assert media.content == b"jpeg"
    assert "Authorization" not in s.calls[1]["headers"]


def test_clip_rejects_durations_ring_does_not_allow():
    with pytest.raises(ValueError):
        client(FakeSession()).clip(DEV, 0, 900_001)


def test_other_errors_raise_ring_error():
    s = FakeSession().on("GET", "/v1/devices", lambda c: response(503, {"errors": [{"title": "Service Unavailable"}]}))
    with pytest.raises(RingError) as err:
        client(s).devices()
    assert err.value.status == 503 and not isinstance(err.value, RingAuthError)
