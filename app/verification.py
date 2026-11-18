"""Trace verification against sealed evidence.

A published contamination report must be reproducible from the ledger as it
existed when the trace was accepted. At acceptance the service seals the
samples, their sample-batch links, the contamination sources and the pinned
batch versions (``trace_evidence``); batch versions themselves are retained,
including superseded rows, in the append-only ``batches`` table.

Verification rebuilds the trace input from that evidence and the retained
pinned batch records only — the current live ledger is never consulted, so a
later sample intake cannot change the verdict — reruns the original
propagation rules in :mod:`app.tracing`, and compares the recomputed result
item by item with the stored report (affected samples, earliest arrival,
quarantine deadline, path).

Reports without sealed evidence (published before sealing existed), failed or
unpublished traces, and traces whose pinned batch version is no longer
retained are reported as *not verifiable* with an explicit reason; they are
never silently reconstructed from current data.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import tracing
from .store import parse_iso

#: Machine-readable reason codes for ``verifiable = false`` responses.
REASON_NO_EVIDENCE = "sealed_evidence_missing"
REASON_FAILED = "trace_failed"
REASON_PENDING = "trace_not_published"
REASON_MISSING_BATCH_VERSION = "pinned_batch_version_not_retained"
REASON_EVIDENCE_INCONSISTENT = "sealed_evidence_inconsistent"


def _published_map(rows: Any) -> dict[str, dict[str, Any]]:
    """Key the stored result rows by sample id, parsing their paths."""
    published: dict[str, dict[str, Any]] = {}
    for row in rows:
        published[row["sample_id"]] = {
            "sample_id": row["sample_id"],
            "first_arrival_at": parse_iso(row["first_arrival"]),
            "quarantine_until": parse_iso(row["quarantine_until"]),
            "path": json.loads(row["path_json"]),
        }
    return published


def _recomputed_map(outcome: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["sample_id"]: {
            "sample_id": item["sample_id"],
            "first_arrival_at": item["first_arrival_at"],
            "quarantine_until": item["quarantine_until"],
            "path": [
                {
                    "batch_no": leg["batch_no"],
                    "origin": leg["origin"],
                    "destination": leg["destination"],
                    "departed_at": leg["departed_at"],
                    "arrived_at": leg["arrived_at"],
                    "version": int(leg["version"]),
                }
                for leg in item["path"]
            ],
        }
        for item in outcome["affected_samples"]
    }


def _jsonable_leg(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_no": leg["batch_no"],
        "version": int(leg["version"]),
        "origin": leg["origin"],
        "destination": leg["destination"],
        "departed_at": leg["departed_at"].isoformat()
        if isinstance(leg["departed_at"], datetime)
        else leg["departed_at"],
        "arrived_at": leg["arrived_at"].isoformat()
        if isinstance(leg["arrived_at"], datetime)
        else leg["arrived_at"],
    }


def _leg_key(leg: dict[str, Any]) -> tuple[str, int, str, str, datetime, datetime]:
    """Format-independent identity of one path leg (datetimes parsed)."""
    return (
        leg["batch_no"],
        int(leg["version"]),
        leg["origin"],
        leg["destination"],
        parse_iso(leg["departed_at"])
        if isinstance(leg["departed_at"], str)
        else leg["departed_at"],
        parse_iso(leg["arrived_at"])
        if isinstance(leg["arrived_at"], str)
        else leg["arrived_at"],
    )


def _path_key(
    path: list[dict[str, Any]],
) -> list[tuple[str, int, str, str, datetime, datetime]]:
    """Order-sensitive, serialisation-independent representation of a path."""
    return [_leg_key(leg) for leg in path]


def _field_differences(
    published: dict[str, Any], recomputed: dict[str, Any]
) -> list[dict[str, str]]:
    """Per-field discrepancies for a sample present in both result sets."""
    differences: list[dict[str, str]] = []
    for field in ("first_arrival_at", "quarantine_until"):
        if published[field] != recomputed[field]:
            differences.append(
                {
                    "field": field,
                    "published": published[field].isoformat(),
                    "recomputed": recomputed[field].isoformat(),
                }
            )
    if _path_key(published["path"]) != _path_key(recomputed["path"]):
        differences.append(
            {
                "field": "path",
                "published": json.dumps(
                    [_jsonable_leg(leg) for leg in published["path"]],
                    sort_keys=True,
                ),
                "recomputed": json.dumps(
                    [_jsonable_leg(leg) for leg in recomputed["path"]],
                    sort_keys=True,
                ),
            }
        )
    return differences


def verify(bundle: dict[str, Any]) -> dict[str, Any]:
    """Verify one trace from its sealed evidence and retained batch versions.

    Args:
        bundle: the dictionary returned by
            :meth:`app.store.Store.verification_bundle`.

    Returns:
        A response-shaped dictionary. When the trace cannot be verified,
        ``verifiable`` is false and ``reasons`` explains why; no current-ledger
        data is used to fill the gap.
    """
    trace = bundle["trace"]
    invalidations = list(bundle["invalidations"])
    invalidated = bool(invalidations)

    base: dict[str, Any] = {
        "trace_id": int(trace["id"]),
        "status": trace["status"],
        "verifiable": False,
        "reasons": [],
        "invalidated": invalidated,
        "invalidation_records": [
            {
                "batch_no": row["batch_no"],
                "version": int(row["version"]),
                "invalidated_at": parse_iso(row["invalidated_at"]),
                "reason": row["reason"],
            }
            for row in invalidations
        ],
        "sealed_evidence": None,
        "recomputed": None,
        "comparison": None,
        "reproduced": None,
    }

    evidence_row = bundle["evidence"]
    if trace["status"] == "failed":
        base["reasons"] = [
            {"code": REASON_FAILED, "message": trace["error"] or "trace failed"}
        ]
        return base
    if trace["status"] != "complete":
        base["reasons"] = [
            {
                "code": REASON_PENDING,
                "message": (
                    f"trace is {trace['status']} and never published a result"
                ),
            }
        ]
        return base
    if evidence_row is None:
        # A report accepted before evidence sealing existed: do not reconstruct
        # it from the current ledger.
        base["reasons"] = [
            {
                "code": REASON_NO_EVIDENCE,
                "message": (
                    "this report was published without sealed evidence and"
                    " cannot be re-derived from the current ledger"
                ),
            }
        ]
        return base

    sources = json.loads(evidence_row["sources_json"])
    sample_ids = json.loads(evidence_row["samples_json"])
    links = json.loads(evidence_row["links_json"])
    pinned = json.loads(evidence_row["pinned_json"])

    base["sealed_evidence"] = {
        "sealed_at": parse_iso(evidence_row["sealed_at"]),
        "source_sample_ids": sources,
        "sample_ids": sample_ids,
        "sample_batch_links": links,
        "batch_versions": pinned,
    }

    # The recomputation may only use batch versions retained at the pinned
    # numbers; live (possibly newer) rows are deliberately ignored.
    retained = {(row["batch_no"], int(row["version"])): row for row in bundle["batches"]}
    missing = sorted(
        batch_no
        for batch_no, version in pinned.items()
        if (batch_no, int(version)) not in retained
    )
    if missing:
        base["reasons"] = [
            {
                "code": REASON_MISSING_BATCH_VERSION,
                "message": (
                    "the sealed evidence references batch versions that are no"
                    " longer retained: " + ", ".join(missing)
                ),
            }
        ]
        return base

    # Defensive: every batch referenced by a sealed link must be in the pin.
    linked_batches = set(links)
    if set(pinned) != linked_batches:
        base["reasons"] = [
            {
                "code": REASON_EVIDENCE_INCONSISTENT,
                "message": (
                    "the sealed batch pins do not match the sealed sample-batch"
                    " links"
                ),
            }
        ]
        return base

    batches = [
        {
            "batch_no": batch_no,
            "version": int(version),
            "origin": retained[(batch_no, int(version))]["origin"],
            "destination": retained[(batch_no, int(version))]["destination"],
            "departed_at": parse_iso(
                retained[(batch_no, int(version))]["departed_at"]
            ),
            "arrived_at": parse_iso(
                retained[(batch_no, int(version))]["arrived_at"]
            ),
            "samples": tuple(sorted(links.get(batch_no, []))),
        }
        for batch_no, version in sorted(pinned.items())
    ]

    try:
        outcome = tracing.compute(
            samples=[{"sample_id": sid} for sid in sample_ids],
            batches=batches,
            source_sample_ids=sources,
        )
    except tracing.TraceFailure as exc:
        base["reasons"] = [
            {
                "code": REASON_EVIDENCE_INCONSISTENT,
                "message": f"recomputation failed on the sealed evidence: {exc}",
            }
        ]
        return base

    published = _published_map(bundle["results"])
    recomputed = _recomputed_map(outcome)

    added = sorted(set(recomputed) - set(published))
    removed = sorted(set(published) - set(recomputed))
    changed: list[dict[str, Any]] = []
    for sample_id in sorted(set(published) & set(recomputed)):
        differences = _field_differences(published[sample_id], recomputed[sample_id])
        if differences:
            changed.append(
                {
                    "sample_id": sample_id,
                    "field_differences": differences,
                    "published": published[sample_id],
                    "recomputed": recomputed[sample_id],
                }
            )

    comparison = {
        "published_affected_count": len(published),
        "recomputed_affected_count": len(recomputed),
        "samples_added": added,
        "samples_removed": removed,
        "samples_changed": changed,
    }
    base["verifiable"] = True
    base["recomputed"] = {
        "has_sources": bool(outcome["source_sample_ids"]),
        "source_sample_ids": outcome["source_sample_ids"],
        "affected_samples": [
            {
                "sample_id": item["sample_id"],
                "first_arrival_at": item["first_arrival_at"],
                "quarantine_until": item["quarantine_until"],
                "path": item["path"],
            }
            for item in outcome["affected_samples"]
        ],
    }
    base["comparison"] = comparison
    base["reproduced"] = not (added or removed or changed)
    return base
