"""Sealed-evidence verification: reproducibility, late intakes, corrections.

The verification endpoint recomputes a published report using only the
evidence sealed at trace acceptance and retained (possibly superseded) batch
versions. It must never fall back to the current ledger.
"""
from __future__ import annotations

import json
import sqlite3

from conftest import batch, ingest, trace

from app import verification
from app.main import create_app


def seed(client):
    ingest(
        client,
        sources=["S1"],
        samples=[
            {"sample_id": "S1", "batch_nos": ["B1"]},
            {"sample_id": "S2", "batch_nos": ["B2"]},
            {"sample_id": "S3", "batch_nos": ["B3"]},
        ],
        batches=[
            batch("B1", "lab", "hub", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            batch("B2", "hub", "ref", "2026-01-01T02:00:00Z", "2026-01-01T03:00:00Z"),
            batch("B3", "ref", "arch", "2026-01-01T04:00:00Z", "2026-01-01T05:00:00Z"),
        ],
    )


def verify(client, trace_id):
    return client.post(f"/traces/{trace_id}/verification")


def test_published_trace_is_verifiable_and_reproduced(client):
    seed(client)
    published = trace(client).json()
    response = verify(client, published["trace_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verifiable"] is True
    assert body["reproduced"] is True
    assert body["invalidated"] is False
    assert body["reasons"] == []
    evidence = body["sealed_evidence"]
    assert evidence["batch_versions"] == {"B1": 1, "B2": 1, "B3": 1}
    assert evidence["source_sample_ids"] == ["S1"]
    assert evidence["sample_ids"] == ["S1", "S2", "S3"]
    # Recomputation uses the original propagation rules, item by item.
    recomputed = {a["sample_id"]: a for a in body["recomputed"]["affected_samples"]}
    original = {a["sample_id"]: a for a in published["affected_samples"]}
    assert set(recomputed) == set(original)
    for sample_id, item in original.items():
        assert recomputed[sample_id] == item
    comparison = body["comparison"]
    assert comparison["published_affected_count"] == 2
    assert comparison["recomputed_affected_count"] == 2
    assert (
        comparison["samples_added"]
        == comparison["samples_removed"]
        == comparison["samples_changed"]
        == []
    )


def test_late_intake_cannot_change_verification_of_old_report(client):
    seed(client)
    old = trace(client).json()
    trace_id = old["trace_id"]

    # A sample registered later rides an old batch in the *current* ledger...
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S-LATE", "batch_nos": ["B1"]}],
        batches=[],
    )
    # ...but the sealed evidence keeps the original associations.
    body = verify(client, trace_id).json()
    assert body["verifiable"] is True
    assert body["reproduced"] is True
    assert "S-LATE" not in body["sealed_evidence"]["sample_ids"]
    assert all(
        "S-LATE" not in riders
        for riders in body["sealed_evidence"]["sample_batch_links"].values()
    )
    assert body["comparison"]["recomputed_affected_count"] == 2

    # A fresh trace naturally sees the current ledger.
    fresh = trace(client).json()
    fresh_body = verify(client, fresh["trace_id"]).json()
    assert "S-LATE" in fresh_body["sealed_evidence"]["sample_ids"]
    assert fresh_body["recomputed"]["affected_samples"] == fresh["affected_samples"]


def test_corrected_old_report_still_verifies_but_is_marked_invalidated(client):
    seed(client)
    old = trace(client).json()
    trace_id = old["trace_id"]

    correction = client.post(
        "/batches/B2/corrections",
        json={
            "seen_version": 1,
            "origin": "hub",
            "destination": "ref",
            # Past the 24h window from B1's 01:00 arrival: chain breaks.
            "departed_at": "2026-01-02T02:00:00Z",
            "arrived_at": "2026-01-02T03:00:00Z",
        },
    )
    assert correction.status_code == 200

    body = verify(client, trace_id).json()
    assert body["verifiable"] is True
    assert body["reproduced"] is True
    assert body["invalidated"] is True
    assert len(body["invalidation_records"]) == 1
    record = body["invalidation_records"][0]
    assert record["batch_no"] == "B2"
    assert record["version"] == 1
    # The recomputation pinned the retained v1 row, not the live v2 row.
    legs = [
        leg
        for sample in body["recomputed"]["affected_samples"]
        for leg in sample["path"]
        if leg["batch_no"] == "B2"
    ]
    assert legs and all(leg["version"] == 1 for leg in legs)
    assert [a["sample_id"] for a in body["recomputed"]["affected_samples"]] == [
        "S2",
        "S3",
    ]

    # Meanwhile the ordinary read still refuses to serve the stale report.
    assert client.get(f"/traces/{trace_id}").status_code == 409


def test_verification_after_interleaved_corrections_and_intakes(client):
    seed(client)
    first = trace(client).json()

    # Interleave an intake and a correction, then another intake.
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S4", "batch_nos": ["B3"]}],
        batches=[],
    )
    client.post(
        "/batches/B3/corrections",
        json={
            "seen_version": 1,
            "origin": "ref",
            "destination": "deep-archive",
            "departed_at": "2026-01-01T04:30:00Z",
            "arrived_at": "2026-01-01T05:30:00Z",
        },
    )
    ingest(
        client,
        sources=[],
        samples=[{"sample_id": "S5", "batch_nos": ["B2"]}],
        batches=[],
    )

    # Old report: sealed against v1, verifiable historically, marked invalid.
    old_body = verify(client, first["trace_id"]).json()
    assert old_body["verifiable"] is True
    assert old_body["reproduced"] is True
    assert old_body["invalidated"] is True
    assert set(old_body["sealed_evidence"]["sample_ids"]) == {"S1", "S2", "S3"}

    # New report: pins v2 and includes the late samples.
    fresh = trace(client).json()
    fresh_body = verify(client, fresh["trace_id"]).json()
    assert fresh_body["verifiable"] is True
    assert fresh_body["reproduced"] is True
    assert fresh_body["invalidated"] is False
    assert fresh_body["sealed_evidence"]["batch_versions"]["B3"] == 2
    assert set(fresh_body["sealed_evidence"]["sample_ids"]) == {
        "S1",
        "S2",
        "S3",
        "S4",
        "S5",
    }
    assert fresh_body["comparison"]["published_affected_count"] == fresh[
        "affected_count"
    ]


def test_failed_trace_is_explicitly_not_verifiable(client, monkeypatch):
    seed(client)

    def boom(**kwargs):
        raise RuntimeError("simulated computation failure")

    monkeypatch.setattr("app.tracing.compute", boom)
    failed = trace(client)
    assert failed.status_code == 500
    trace_id = failed.json()["details"]["trace_id"]
    monkeypatch.undo()

    body = verify(client, trace_id).json()
    assert body["verifiable"] is False
    assert body["reproduced"] is None
    assert body["sealed_evidence"] is None
    assert body["reasons"] == [
        {"code": verification.REASON_FAILED, "message": "RuntimeError: simulated computation failure"}
    ]
    # No current-ledger backfill happened.
    assert body["recomputed"] is None
    assert body["comparison"] is None


def test_legacy_report_without_sealed_evidence_is_not_verifiable(client):
    seed(client)
    published = trace(client).json()
    trace_id = published["trace_id"]

    # Simulate a database created before evidence sealing existed.
    conn = sqlite3.connect(client.app.state.store.path)
    conn.execute("DELETE FROM trace_evidence WHERE trace_id=?", (trace_id,))
    conn.commit()
    conn.close()

    body = verify(client, trace_id).json()
    assert body["verifiable"] is False
    assert body["reproduced"] is None
    assert [reason["code"] for reason in body["reasons"]] == [
        verification.REASON_NO_EVIDENCE
    ]
    assert body["recomputed"] is None


def test_missing_pinned_batch_version_is_not_verifiable(client):
    seed(client)
    published = trace(client).json()
    trace_id = published["trace_id"]

    # Retained batch versions are the only batch evidence verification may use;
    # physically removing one must make the report unverifiable.
    conn = sqlite3.connect(client.app.state.store.path)
    conn.execute(
        "DELETE FROM batches WHERE batch_no=? AND version=1", ("B2",)
    )
    conn.commit()
    conn.close()

    body = verify(client, trace_id).json()
    assert body["verifiable"] is False
    assert body["reproduced"] is None
    assert [reason["code"] for reason in body["reasons"]] == [
        verification.REASON_MISSING_BATCH_VERSION
    ]
    # The sealed evidence itself is still presented for the auditor.
    assert body["sealed_evidence"]["batch_versions"]["B2"] == 1


def test_unknown_trace_returns_404(client):
    seed(client)
    assert verify(client, 9999).status_code == 404


def test_pending_trace_is_not_verifiable(client, monkeypatch):
    seed(client)
    from app import store as store_module

    held = __import__("threading").Event()

    def stall(**kwargs):
        held.wait(timeout=5)
        return kwargs  # never published; unreachable but keeps the trace pending

    monkeypatch.setattr("app.tracing.compute", stall)
    import threading

    thread = threading.Thread(target=lambda: trace(client))
    thread.start()
    # Wait until the pending trace exists.
    import time

    deadline = time.monotonic() + 5
    trace_id = None
    while time.monotonic() < deadline:
        rows = client.get("/traces").json()["traces"]
        if rows and rows[0]["status"] == "pending":
            trace_id = rows[0]["trace_id"]
            break
        time.sleep(0.02)
    assert trace_id is not None
    body = verify(client, trace_id).json()
    assert body["verifiable"] is False
    assert [reason["code"] for reason in body["reasons"]] == [
        verification.REASON_PENDING
    ]
    held.set()
    thread.join(timeout=5)
    monkeypatch.undo()


def test_correction_that_changes_history_is_recomputed_on_old_versions(client):
    """A v2 batch would remove an affected sample; verification on v1 keeps it."""
    seed(client)
    old = trace(client).json()
    assert [a["sample_id"] for a in old["affected_samples"]] == ["S2", "S3"]

    # Break the hub -> ref link by pushing B2 outside the 24h window.
    client.post(
        "/batches/B2/corrections",
        json={
            "seen_version": 1,
            "origin": "hub",
            "destination": "ref",
            "departed_at": "2026-01-03T02:00:00Z",
            "arrived_at": "2026-01-03T03:00:00Z",
        },
    )
    current = trace(client).json()
    assert current["affected_samples"] == []  # live ledger no longer reaches S2/S3

    body = verify(client, old["trace_id"]).json()
    assert body["verifiable"] is True
    assert body["reproduced"] is True
    assert body["invalidated"] is True
    # Historical recomputation still reports S2 and S3 from retained v1 rows.
    assert [
        a["sample_id"] for a in body["recomputed"]["affected_samples"]
    ] == ["S2", "S3"]
    # And it differs from what a current-ledger computation would produce.
    assert body["comparison"]["recomputed_affected_count"] == 2


def test_differences_are_reported_item_by_item(client, monkeypatch):
    seed(client)
    published = trace(client).json()
    trace_id = published["trace_id"]
    real_compute = __import__("app.tracing", fromlist=["compute"]).compute

    def tampered(**kwargs):
        outcome = real_compute(**kwargs)
        # Drop S3 and shift S2's arrival/deadline and path identity.
        outcome["affected_samples"] = [
            row for row in outcome["affected_samples"] if row["sample_id"] != "S3"
        ]
        s2 = next(
            row for row in outcome["affected_samples"] if row["sample_id"] == "S2"
        )
        s2["first_arrival_at"] = s2["first_arrival_at"].replace(year=2027)
        s2["quarantine_until"] = s2["quarantine_until"].replace(year=2027)
        s2["path"] = s2["path"][:1]
        return outcome

    monkeypatch.setattr("app.tracing.compute", tampered)
    # Recompute through the verifier with a tampered algorithm; the stored
    # report itself is untouched, so the comparison surfaces every drift.
    from app import verification as verification_module

    original_verify_compute = verification_module.tracing.compute
    verification_module.tracing.compute = tampered
    try:
        body = verify(client, trace_id).json()
    finally:
        verification_module.tracing.compute = original_verify_compute
    monkeypatch.undo()

    assert body["verifiable"] is True
    assert body["reproduced"] is False
    comparison = body["comparison"]
    assert comparison["samples_removed"] == ["S3"]
    assert comparison["samples_added"] == []
    changed = comparison["samples_changed"]
    assert len(changed) == 1
    assert changed[0]["sample_id"] == "S2"
    fields = {diff["field"] for diff in changed[0]["field_differences"]}
    assert fields == {"first_arrival_at", "quarantine_until", "path"}
    # Both sides of the drift are included.
    assert changed[0]["published"]["sample_id"] == "S2"
    assert changed[0]["recomputed"]["sample_id"] == "S2"


def test_evidence_is_sealed_in_the_acceptance_transaction(client):
    seed(client)
    body = trace(client).json()
    store = client.app.state.store
    with store.connection() as conn:
        evidence = conn.execute(
            "SELECT * FROM trace_evidence WHERE trace_id=?",
            (body["trace_id"],),
        ).fetchone()
    assert evidence is not None
    assert json.loads(evidence["pinned_json"]) == {"B1": 1, "B2": 1, "B3": 1}
    links = json.loads(evidence["links_json"])
    assert links["B1"] == ["S1"]
    assert links["B2"] == ["S2"]
    assert json.loads(evidence["sources_json"]) == ["S1"]


def test_verification_endpoint_is_documented_in_openapi(client):
    schema = client.get("/openapi.json").json()
    path = schema["paths"]["/traces/{trace_id}/verification"]
    assert "post" in path
    assert "verification" in path["post"]["tags"]
    assert path["post"]["responses"]["404"]


def test_no_sources_trace_verifies_with_empty_affected_set(client):
    ingest(
        client,
        sources=[],
        samples=[
            {"sample_id": "S1", "batch_nos": ["B1"]},
            {"sample_id": "S2", "batch_nos": ["B2"]},
        ],
        batches=[
            batch("B1", "a", "b", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"),
            batch("B2", "b", "c", "2026-01-01T02:00:00Z", "2026-01-01T03:00:00Z"),
        ],
    )
    published = trace(client).json()
    assert published["affected_count"] == 0
    body = verify(client, published["trace_id"]).json()
    assert body["verifiable"] is True
    assert body["reproduced"] is True
    assert body["recomputed"]["has_sources"] is False
    assert body["comparison"]["recomputed_affected_count"] == 0


def test_app_constructs_with_verification_router(tmp_path):
    app = create_app(str(tmp_path / "fresh.db"))
    routes = {route.path for route in app.routes}
    assert "/traces/{trace_id}/verification" in routes
