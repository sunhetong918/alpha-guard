"""Durable, explicitly resumed analysis batches in the existing research database."""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .analysis import AnalysisBudget, _QUOTE_AGE, _context
from .models import EvidenceRecord, ResearchReport, digest, parse_time
from .store import ResearchStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_scan_runs (
 id TEXT PRIMARY KEY, status TEXT NOT NULL, stage TEXT NOT NULL, mode TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, options_json TEXT NOT NULL,
 config_signature TEXT NOT NULL, contexts_json TEXT NOT NULL DEFAULT '{}',
 sources_json TEXT NOT NULL DEFAULT '[]', frozen INTEGER NOT NULL DEFAULT 0,
 model_calls INTEGER NOT NULL DEFAULT 0, max_ai_calls INTEGER NOT NULL,
 detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS research_scan_items (
 run_id TEXT NOT NULL REFERENCES research_scan_runs(id), id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, evidence_id TEXT NOT NULL REFERENCES research_evidence(id),
 evidence_hash TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
 model_calls INTEGER NOT NULL DEFAULT 0, attempt_calls INTEGER NOT NULL DEFAULT 0,
 report_ids_json TEXT NOT NULL DEFAULT '[]', detail TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(run_id,id), UNIQUE(run_id,ordinal)
);
CREATE INDEX IF NOT EXISTS research_scan_run_time ON research_scan_runs(created_at DESC,id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _material_version(material: EvidenceRecord) -> list[Any]:
    # Repeated source content can reveal an older original publication time.
    # That derived clock also affects validity, despite an unchanged body ID.
    value = material.model_dump(mode="json")
    return [value[key] for key in ("id", "content_hash", "origin_published_at", "cluster_first_seen_at", "quality_issues")]


class ResumeError(ValueError):
    def __init__(self, reason: str, *, acknowledgement: bool = False) -> None:
        self.reason = reason
        self.acknowledgement = acknowledgement
        super().__init__(reason)


class ProgressStore(ResearchStore):
    """Only its owner holding ResearchLease may mutate batch execution state."""

    def __init__(self, path: Any) -> None:
        super().__init__(path)
        self.ensure_feature_schema(_SCHEMA, ("research_scan_runs", "research_scan_items"))

    def __enter__(self) -> ProgressStore:
        return self

    def create_run(self, options: Mapping[str, Any], signature: str, mode: str) -> str:
        identity, stamp = uuid.uuid4().hex, _now()
        with self._connection:
            self._connection.execute(
                "INSERT INTO research_scan_runs(id,status,stage,mode,created_at,updated_at,"
                "options_json,config_signature,max_ai_calls) VALUES (?,'running','queued',?,?,?,?,?,?)",
                (identity, mode, stamp, stamp, _json(dict(options)), signature, options["max_ai_calls"]),
            )
        return identity

    def recover(self) -> None:
        """Run only after acquiring the lease, never infer ownership from a clock."""
        with self._connection:
            self._connection.execute(
                "UPDATE research_scan_items SET status=CASE WHEN attempt_calls>0 THEN 'uncertain' ELSE 'pending' END,"
                "detail=CASE WHEN attempt_calls>0 THEN 'result_unconfirmed' ELSE '' END "
                "WHERE status='running' AND run_id IN (SELECT id FROM research_scan_runs WHERE status='running')"
            )
            self._connection.execute(
                "UPDATE research_scan_runs SET status='interrupted',updated_at=?,"
                "detail=CASE WHEN frozen=1 THEN 'process_interrupted' ELSE 'collection_not_resumable' END "
                "WHERE status='running'", (_now(),),
            )

    def stage(self, identity: str, stage: str) -> None:
        with self._connection:
            self._connection.execute("UPDATE research_scan_runs SET stage=?,updated_at=? WHERE id=?",
                                     (stage, _now(), identity))

    def freeze(self, identity: str, evidence: Sequence[EvidenceRecord], contexts: Mapping[str, Any],
               sources: Sequence[Mapping[str, Any]]) -> None:
        safe_contexts = {key: _context(value) for key, value in contexts.items()}
        with self._connection:
            run = self.load_run(identity)
            for index, material in enumerate(evidence):
                item_id = digest([_material_version(material), json.loads(run["options_json"]),
                                  safe_contexts, run["config_signature"]])
                self._connection.execute(
                    "INSERT INTO research_scan_items(run_id,id,ordinal,evidence_id,evidence_hash,title,status) "
                    "VALUES (?,?,?,?,?,?,'pending')",
                    (identity, item_id, index, material.id, material.content_hash, material.title[:300]),
                )
            self._connection.execute(
                "UPDATE research_scan_runs SET frozen=1,stage='analysis',contexts_json=?,sources_json=?,updated_at=? WHERE id=?",
                (_json(safe_contexts), _json(list(sources)), _now(), identity),
            )

    def load_run(self, identity: str) -> dict[str, Any]:
        row = self._connection.execute("SELECT * FROM research_scan_runs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ResumeError("run_not_found")
        return dict(row)

    def items(self, identity: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT * FROM research_scan_items WHERE run_id=? ORDER BY ordinal", (identity,))]

    def incompatibility(self, identity: str, options: Mapping[str, Any], signature: str,
                        *, now: datetime | None = None) -> str | None:
        run = self.load_run(identity)
        timestamp = now or datetime.now(UTC)
        if run["status"] == "running":
            return "run_active"
        if run["mode"] != "once":
            return "mode_not_resumable"
        if not run["frozen"]:
            return "collection_not_resumable"
        items = self.items(identity)
        if not any(item["status"] != "completed" for item in items):
            return "nothing_remaining"
        if _json(dict(options)) != run["options_json"] or signature != run["config_signature"]:
            return "configuration_changed"
        created = parse_time(run["created_at"])
        if created is None or created.date() != timestamp.date():
            return "batch_date_expired"
        contexts = json.loads(run["contexts_json"])
        for item in items:
            material = self.get_evidence(item["evidence_id"])
            if material is None or material.content_hash != item["evidence_hash"]:
                return "material_changed"
            head = self._connection.execute("SELECT evidence_id FROM research_source_heads WHERE source_identity=?",
                                            (material.source_identity,)).fetchone()
            if head is not None and head[0] != material.id:
                return "material_changed"
            if item["id"] != digest([_material_version(material), dict(options), contexts, signature]):
                return "material_changed"
        # An absent quote still produces an observation report; a known expired
        # quote cannot silently become fresh when the batch resumes.
        for context in contexts.values():
            for horizon in options["horizons"]:
                keys: tuple[str, ...] = (("source_as_of", "observed_at", "volume_source_as_of")
                                         if horizon == "short" else ("source_as_of", "observed_at"))
                if horizon == "long":
                    keys += ("fundamentals_observed_at",)
                for key in keys:
                    raw = context.get(key)
                    clock = parse_time(raw)
                    if raw and (clock is None or timestamp < clock or timestamp - clock >= _QUOTE_AGE[horizon]):
                        return "market_snapshot_expired"
        if run["model_calls"] >= run["max_ai_calls"] and options["ai_enabled"]:
            return "model_budget_exhausted"
        return None

    def summary(self, identity: str, *, reason: str | None = None) -> dict[str, Any]:
        run = self.load_run(identity)
        counts = {state: 0 for state in ("completed", "pending", "failed", "uncertain", "running")}
        for row in self._connection.execute(
            "SELECT status,COUNT(*) FROM research_scan_items WHERE run_id=? GROUP BY status", (identity,)):
            counts[row[0]] = row[1]
        return {"run_id": identity, **{key: run[key] for key in (
            "status", "stage", "mode", "created_at", "updated_at", "model_calls", "max_ai_calls", "detail")},
            **counts, "total": sum(counts.values()),
            "resumable": reason is None and run["status"] != "running" and run["frozen"] == 1 and run["mode"] == "once",
            "incompatible_reason": reason}

    def begin_resume(self, identity: str, *, ack_uncertain: bool) -> None:
        if any(item["status"] == "uncertain" for item in self.items(identity)) and not ack_uncertain:
            raise ResumeError("uncertain_model_call", acknowledgement=True)
        with self._connection:
            self._connection.execute("UPDATE research_scan_runs SET status='running',stage='analysis',detail='',updated_at=? WHERE id=?",
                                     (_now(), identity))
            self._connection.execute("UPDATE research_scan_items SET status='pending',attempt_calls=0,detail='' "
                                     "WHERE run_id=? AND status!='completed'", (identity,))

    def begin_item(self, identity: str, item_id: str) -> None:
        with self._connection:
            self._connection.execute("UPDATE research_scan_items SET status='running',attempt_calls=0,detail='' WHERE run_id=? AND id=?",
                                     (identity, item_id))
            self._connection.execute("UPDATE research_scan_runs SET updated_at=? WHERE id=?", (_now(), identity))

    def reserve_call(self, identity: str, item_id: str) -> bool:
        with self._connection:
            changed = self._connection.execute(
                "UPDATE research_scan_runs SET model_calls=model_calls+1,updated_at=? "
                "WHERE id=? AND status='running' AND model_calls<max_ai_calls", (_now(), identity))
            if changed.rowcount != 1:
                return False
            item = self._connection.execute(
                "UPDATE research_scan_items SET model_calls=model_calls+1,attempt_calls=attempt_calls+1 "
                "WHERE run_id=? AND id=? AND status='running'", (identity, item_id))
            if item.rowcount != 1:
                raise RuntimeError("analysis_item_not_running")
        return True

    def finish_item(self, identity: str, item_id: str, reports: Sequence[ResearchReport],
                    status: str, detail: str = "") -> None:
        # Index migration commits independently, before the atomic result write.
        self.ensure_report_overview_index()
        with self._connection:
            for report in reports:
                self._connection.execute("INSERT OR IGNORE INTO research_reports VALUES (?,?,?,?)",
                                         (report.id, report.evidence_id, report.model_dump_json(), report.created_at.isoformat()))
            self._connection.execute(
                "UPDATE research_scan_items SET status=?,detail=?,report_ids_json=? WHERE run_id=? AND id=?",
                (status, detail, _json([report.id for report in reports]), identity, item_id))
            self._connection.execute("UPDATE research_scan_runs SET updated_at=? WHERE id=?", (_now(), identity))

    def finish_run(self, identity: str, status: str, detail: str = "") -> None:
        with self._connection:
            self._connection.execute("UPDATE research_scan_runs SET status=?,detail=?,updated_at=? WHERE id=?",
                                     (status, detail, _now(), identity))


class PersistentBudget(AnalysisBudget):
    """Charge before dispatch; interruption never restores a possibly spent call."""

    def __init__(self, store: ProgressStore, run_id: str) -> None:
        run = store.load_run(run_id)
        super().__init__(run["max_ai_calls"])
        self._used = run["model_calls"]
        self.store, self.run_id = store, run_id
        self.item_id = ""

    def consume(self) -> bool:
        with self._lock:
            if not self.store.reserve_call(self.run_id, self.item_id):
                return False
            self._used += 1
            return True
