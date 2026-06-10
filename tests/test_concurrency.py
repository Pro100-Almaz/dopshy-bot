"""Concurrency proof: the EXCLUDE constraint lets exactly one of N simultaneous
request_payment calls win the same slot."""

import threading
import uuid

from integrations import booking_service as svc
from integrations.repo import postgres

N = 50


def _make_draft_for_same_slot() -> tuple[int, str]:
    token = str(uuid.uuid4())
    bid = postgres.create_draft(
        "chat", "7700", client_token=token,
        date="2026-08-01", time_start="18:00", time_end="19:00",
        field=1, format="5x5", players=8, customer_name="Race",
    )["data"]["booking_id"]
    return bid, token


def test_exactly_one_wins_the_slot():
    drafts = [_make_draft_for_same_slot() for _ in range(N)]

    results: list[dict] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(N)

    def worker(bid: int, token: str):
        barrier.wait()  # release all threads at once for maximum contention
        res = svc.request_payment(bid, token)
        with results_lock:
            results.append(res)

    threads = [threading.Thread(target=worker, args=(bid, tok)) for bid, tok in drafts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wins = [r for r in results if r["ok"]]
    taken = [r for r in results if not r["ok"] and r["code"] == "SLOT_TAKEN"]

    assert len(results) == N
    assert len(wins) == 1, f"expected exactly 1 winner, got {len(wins)}"
    assert len(taken) == N - 1, f"expected {N - 1} SLOT_TAKEN, got {len(taken)}"
