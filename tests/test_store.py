from handshake.episode import Window
from handshake.store import MemoryStore, StoredReceipt, coverage_from_log

W = Window(1_000, 11_000)


def test_no_history_falls_back_to_status_now_and_says_so():
    assert coverage_from_log([], W, True).source == "status_now"
    assert coverage_from_log([], W, True).complete
    offline = coverage_from_log([], W, False)
    assert not offline.complete and offline.missing_ms == 10_000
    unknown = coverage_from_log([], W, None)
    assert unknown.source == "unknown" and not unknown.complete


def test_offline_stretch_inside_the_window_is_measured():
    log = [(0, True), (3_000, False), (5_000, True)]
    cov = coverage_from_log(log, W, True)
    assert cov.source == "webhooks" and cov.missing_ms == 2_000 and not cov.complete


def test_offline_since_before_the_window_counts_until_it_came_back():
    log = [(500, False), (4_000, True)]
    assert coverage_from_log(log, W, True).missing_ms == 3_000


def test_went_offline_and_never_came_back():
    log = [(0, True), (9_000, False)]
    assert coverage_from_log(log, W, False).missing_ms == 2_000


def test_online_throughout():
    cov = coverage_from_log([(0, True), (20_000, False)], W, True)
    assert cov.complete and cov.missing_ms == 0


def test_receipts_by_order_and_frames_stay_separate():
    store = MemoryStore()
    for i, verdict in enumerate(["inconclusive", "delivered"]):
        store.put_receipt(StoredReceipt(f"r{i}", "ORD-1", "shop", verdict, "x", "jws", 100 + i))
    store.put_receipt(StoredReceipt("r9", "ORD-1", "other-shop", "delivered", "x", "jws", 50))
    assert [r.receipt_id for r in store.receipts_for_order("shop", "ORD-1")] == ["r0", "r1"]
    assert store.recent_receipts(1)[0].receipt_id == "r1"
    store.put_frames("r1", {"before": b"a", "after": b"b"})
    assert store.get_frames("r1")["after"] == b"b" and store.get_frames("r0") == {}
