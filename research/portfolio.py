"""Local full-position snapshots; no broker, rule mutation, pricing or network."""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Self

from .models import digest
from .store import ResearchStore
from .watchlist import _instrument

MAX_BYTES = 256_000
MAX_ROWS = 500
FIELDS = ("market", "ticker", "quantity", "unit_cost", "cost_currency")
CURRENCIES = frozenset({"USD", "HKD", "CNY", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "SGD"})
SCHEMA = """
CREATE TABLE IF NOT EXISTS research_portfolio_accounts (
 account_key TEXT PRIMARY KEY, alias TEXT NOT NULL, current_id TEXT, revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS research_portfolio_snapshots (
 id TEXT PRIMARY KEY, account_key TEXT NOT NULL, snapshot_date TEXT NOT NULL,
 fingerprint TEXT NOT NULL, previous_id TEXT, created_at TEXT NOT NULL,
 source_label TEXT NOT NULL, positions_json TEXT NOT NULL,
 FOREIGN KEY(account_key) REFERENCES research_portfolio_accounts(account_key)
);
CREATE INDEX IF NOT EXISTS research_portfolio_history
 ON research_portfolio_snapshots(account_key,created_at DESC);
"""


class PortfolioConflictError(ValueError):
    pass


def _account(value: Any) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ValueError("portfolio_account_required")
    alias = unicodedata.normalize("NFKC", value).strip()
    if not 1 <= len(alias) <= 80 or any(ord(c) < 32 for c in alias):
        raise ValueError("portfolio_account_invalid")
    return digest(alias.casefold()), alias


def _number(value: str) -> str:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,8})?", value):
        raise ValueError("invalid_nonnegative_decimal")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid_nonnegative_decimal") from None
    return format(number.normalize(), "f")


def parse_positions(csv_text: Any, mapping: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(csv_text, str) or len(csv_text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("portfolio_file_too_large")
    if not isinstance(mapping, dict) or set(mapping) != set(FIELDS):
        raise ValueError("portfolio_mapping_invalid")
    if not all(isinstance(v, str) for v in mapping.values()) or any(not mapping[k] for k in FIELDS[:3]):
        raise ValueError("portfolio_mapping_required")
    selected = [v for v in mapping.values() if v]
    if len(selected) != len(set(selected)):
        raise ValueError("portfolio_mapping_duplicate")
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")), strict=True)
    try:
        headers = reader.fieldnames or []
        if not headers or len(headers) > 40 or len(set(headers)) != len(headers) or any(len(h) > 100 for h in headers):
            raise ValueError("portfolio_headers_invalid")
        if not set(selected) <= set(headers):
            raise ValueError("portfolio_mapping_unknown_column")
        positions, errors, seen = [], [], set()
        for index, row in enumerate(reader):
            if index >= MAX_ROWS:
                raise ValueError("portfolio_too_many_rows")
            line = reader.line_num
            if None in row or any(v is None for v in row.values()):
                errors.append({"line": line, "code": "column_count"})
                continue
            try:
                market, ticker = (str(row[mapping[k]]).strip() for k in FIELDS[:2])
                instrument = _instrument(market, ticker)
            except ValueError:
                errors.append({"line": line, "code": "instrument"})
                continue
            if instrument.key in seen:
                errors.append({"line": line, "code": "duplicate_instrument", "instrument": instrument.key})
                continue
            seen.add(instrument.key)
            try:
                quantity = _number(str(row[mapping["quantity"]]).strip())
            except ValueError:
                errors.append({"line": line, "code": "quantity", "instrument": instrument.key})
                continue
            cost = str(row[mapping["unit_cost"]]).strip() if mapping["unit_cost"] else ""
            currency = str(row[mapping["cost_currency"]]).strip().upper() if mapping["cost_currency"] else ""
            try:
                if bool(cost) != bool(currency) or (currency and currency not in CURRENCIES):
                    raise ValueError("cost_currency")
                cost = _number(cost) if cost else ""
            except ValueError:
                errors.append({"line": line, "code": "unit_cost_or_currency", "instrument": instrument.key})
                continue
            positions.append({"instrument": instrument.key, "quantity": quantity,
                              "unit_cost": cost or None, "cost_currency": currency or None, "line": line})
    except csv.Error:
        raise ValueError("portfolio_csv_invalid") from None
    return positions, errors


class PortfolioStore(ResearchStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self.ensure_feature_schema(SCHEMA, ("research_portfolio_accounts", "research_portfolio_snapshots"))

    def __enter__(self) -> Self:
        return self

    def view(self, alias: str) -> dict[str, Any]:
        key, label = _account(alias)
        account = self._connection.execute("SELECT * FROM research_portfolio_accounts WHERE account_key=?", (key,)).fetchone()
        current = self._connection.execute("SELECT * FROM research_portfolio_snapshots WHERE id=?",
                                           (account["current_id"],)).fetchone() if account else None
        return {"account": account["alias"] if account else label, "revision": account["revision"] if account else 0,
                "snapshot_id": current["id"] if current else None,
                "snapshot_date": current["snapshot_date"] if current else None,
                "source_label": current["source_label"] if current else None,
                "created_at": current["created_at"] if current else None,
                "positions": json.loads(current["positions_json"]) if current else [],
                "can_undo": current is not None, "fingerprint": current["fingerprint"] if current else None}

    def _known(self) -> set[str]:
        known = set()
        for value in (self.load_settings().get("instruments") or []):
            try:
                known.add(_instrument(value["market"], value["ticker"]).key)
            except (ValueError, KeyError, TypeError):
                continue
        if self._connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_watchlist'").fetchone():
            known.update(r[0] for r in self._connection.execute("SELECT instrument_key FROM research_watchlist"))
        return known

    def preview(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        _, alias = _account(fields["account"])
        raw_date = fields["snapshot_date"]
        if not isinstance(raw_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            raise ValueError("portfolio_snapshot_date_invalid")
        day = date.fromisoformat(raw_date)
        if not date(1900, 1, 1) <= day <= date.today():
            raise ValueError("portfolio_snapshot_date_invalid")
        if fields.get("full_snapshot") is not True:
            raise ValueError("portfolio_full_snapshot_confirmation_required")
        label = fields["source_label"]
        if not isinstance(label, str) or not 1 <= len(label) <= 200:
            raise ValueError("portfolio_source_label_invalid")
        # A base name only; do not persist the user's directory or arbitrary paths.
        label = re.sub(r"[^\w. -]", "_", label.replace("\\", "/").rsplit("/", 1)[-1])[:80] or "positions.csv"
        rows, errors = parse_positions(fields["csv_text"], fields["mapping"])
        confirmed = fields.get("confirmed_instruments", [])
        if not isinstance(confirmed, list) or len(confirmed) > MAX_ROWS or not all(isinstance(v, str) and len(v) <= 40 for v in confirmed):
            raise ValueError("portfolio_confirmation_invalid")
        current = self.view(alias)
        known = self._known() | {row["instrument"] for row in current["positions"]}
        unknown = [{"line": row["line"], "instrument": row["instrument"]} for row in rows
                   if row["instrument"] not in known and row["instrument"] not in confirmed]
        positions = sorted([{key: value for key, value in row.items() if key != "line"}
                            for row in rows if Decimal(row["quantity"]) != 0], key=lambda v: v["instrument"])
        fingerprint = digest([1, _account(alias)[0], raw_date, positions])
        old, new = ({r["instrument"]: r for r in values} for values in (current["positions"], positions))
        changes = [{"instrument": key, "change": "removed" if key not in new else "added" if key not in old else "changed",
                    "before": old.get(key), "after": new.get(key)}
                   for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)]
        duplicate = current["fingerprint"] == fingerprint
        token = digest([fingerprint, current["revision"], label, sorted(set(confirmed))])
        return {"account": current["account"], "revision": current["revision"], "snapshot_date": raw_date,
                "source_label": label, "fingerprint": fingerprint, "preview_token": token,
                "positions": positions, "errors": errors, "unconfirmed": unknown, "changes": changes,
                "can_commit": not errors and not unknown and not duplicate,
                "duplicate": duplicate, "full_snapshot": True}

    def commit(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                preview = self.preview(fields)
                if type(fields["expected_revision"]) is not int or fields["expected_revision"] != preview["revision"]:
                    raise PortfolioConflictError("portfolio_changed")
                if fields["preview_token"] != preview["preview_token"]:
                    raise PortfolioConflictError("portfolio_preview_changed")
                if preview["errors"] or preview["unconfirmed"]:
                    raise ValueError("portfolio_rows_not_confirmed")
                if preview["duplicate"]:
                    self._connection.commit()
                    return {"status": "duplicate", **self.view(fields["account"])}
                current = self.view(fields["account"])
                key, alias = _account(fields["account"])
                identity = uuid.uuid4().hex
                self._connection.execute("INSERT OR IGNORE INTO research_portfolio_accounts VALUES (?,?,NULL,0)", (key, alias))
                self._connection.execute("INSERT INTO research_portfolio_snapshots VALUES (?,?,?,?,?,?,?,?)", (
                    identity, key, preview["snapshot_date"], preview["fingerprint"], current["snapshot_id"],
                    datetime.now(UTC).isoformat(), preview["source_label"], json.dumps(preview["positions"], ensure_ascii=False)))
                self._connection.execute("UPDATE research_portfolio_accounts SET current_id=?,revision=revision+1 WHERE account_key=?", (identity, key))
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return {"status": "saved", **self.view(fields["account"])}

    def undo(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                current = self.view(fields["account"])
                if type(fields["expected_revision"]) is not int or fields["expected_revision"] != current["revision"]:
                    raise PortfolioConflictError("portfolio_changed")
                if not current["snapshot_id"]:
                    raise ValueError("portfolio_nothing_to_undo")
                previous = self._connection.execute("SELECT previous_id FROM research_portfolio_snapshots WHERE id=?", (current["snapshot_id"],)).fetchone()[0]
                self._connection.execute("UPDATE research_portfolio_accounts SET current_id=?,revision=revision+1 WHERE account_key=?", (previous, _account(fields["account"])[0]))
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return {"status": "restored", **self.view(fields["account"])}


def execute_portfolio(database: Path, command: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    base = {"account", "snapshot_date", "source_label", "csv_text", "mapping", "full_snapshot"}
    shapes = {"portfolio_get": ({"account"}, set()), "portfolio_preview": (base, {"confirmed_instruments"}),
              "portfolio_commit": (base | {"preview_token", "expected_revision"}, {"confirmed_instruments"}),
              "portfolio_undo": ({"account", "expected_revision"}, set())}
    if command not in shapes:
        raise ValueError("portfolio_command_invalid")
    required, optional = shapes[command]
    if not required <= fields.keys() or fields.keys() - required - optional:
        raise ValueError("portfolio_fields_invalid")
    with PortfolioStore(database) as store:
        if command == "portfolio_get":
            return store.view(fields["account"])
        if command == "portfolio_preview":
            return store.preview(fields)
        if command == "portfolio_commit":
            return store.commit(fields)
        return store.undo(fields)
