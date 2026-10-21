"""Local watchlist and rule editing, preserving the existing two sources of truth."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

import config
from config import InstrumentConfig, RulesConfig
from .locking import ResearchLease
from .models import ResearchInstrument
from .store import ResearchStore

MAX_RULES_BYTES = 2_000_000
MAX_RULES_PER_SIDE = 100
_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_watchlist (
 instrument_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL
);
"""


class WatchlistConflictError(ValueError):
    """The saved configuration no longer matches the reviewed revision."""


class WatchlistStore(ResearchStore):
    """Persist only list membership/identity; existing settings and YAML own switches."""

    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self.ensure_feature_schema(_SCHEMA, ("research_watchlist",))

    def __enter__(self) -> WatchlistStore:
        return self

    def identities(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM research_watchlist ORDER BY instrument_key"
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_identity_and_settings(self, instrument: ResearchInstrument, settings: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO research_watchlist VALUES (?, ?) "
                "ON CONFLICT(instrument_key) DO UPDATE SET payload_json=excluded.payload_json",
                (instrument.key, instrument.model_dump_json()),
            )
            # Both writes are pending on the same connection when the existing
            # settings method validates and commits. Failure rolls back membership.
            self.save_settings(settings)


class _UniqueLoader(yaml.SafeLoader):
    """Do not silently discard duplicate securities or rule fields in YAML."""

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        values: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in values:
                raise ValueError("duplicate YAML configuration key")
            values[key] = self.construct_object(value_node, deep=deep)
        return values


def _instrument(market: Any, ticker: Any, name: Any = "") -> ResearchInstrument:
    if not isinstance(market, str) or not isinstance(ticker, str) or not isinstance(name, str):
        raise ValueError("market, ticker and name must be text")
    market, ticker, name = market.strip().upper(), ticker.strip().upper(), name.strip()
    if market == "HK":
        if not re.fullmatch(r"[0-9]{1,5}", ticker):
            raise ValueError("Hong Kong ticker must contain 1 to 5 digits")
        ticker = ticker.zfill(5)
    elif market == "CN" and not re.fullmatch(r"[0-9]{6}", ticker):
        raise ValueError("China ticker must contain 6 digits")
    return ResearchInstrument(market=market, ticker=ticker, name=name)


def _rules(path: Path) -> tuple[bytes, dict[str, Any], RulesConfig]:
    if path.is_symlink():
        raise ValueError("rule configuration must be a regular local file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_RULES_BYTES + 1)
    if len(raw) > MAX_RULES_BYTES:
        raise ValueError("rule configuration is too large")
    values = yaml.load(raw, Loader=_UniqueLoader)
    if not isinstance(values, dict):
        raise ValueError("rule configuration must be a mapping")
    parsed = RulesConfig.model_validate(values)
    seen: set[str] = set()
    for ticker, instrument in parsed.watchlist.items():
        identity = _instrument(instrument.market, ticker).key
        if identity in seen:
            raise ValueError("duplicate normalized rule instrument")
        seen.add(identity)
    return raw, values, parsed


def _revision(raw: bytes, settings: Mapping[str, Any], identities: list[dict[str, Any]]) -> str:
    public = json.dumps([settings, identities], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw + b"\0" + public).hexdigest()


def _view(raw: bytes, parsed: RulesConfig, settings: dict[str, Any], identities: list[dict[str, Any]], *,
          offset: int = 0, query: str = "", key: str | None = None) -> dict[str, Any]:
    instruments: dict[str, dict[str, Any]] = {}
    for value in identities:
        instrument = _instrument(value["market"], value["ticker"], value.get("name", ""))
        instruments[instrument.key] = {"key": instrument.key, "market": instrument.market,
            "ticker": instrument.ticker, "name": instrument.name, "research_enabled": False,
            "monitoring_enabled": False, "rule_count": 0, "configuration": None}
    research_seen: set[str] = set()
    for value in settings["instruments"]:
        instrument = _instrument(value["market"], value["ticker"], value.get("name", ""))
        if instrument.key in research_seen:
            raise ValueError("duplicate normalized research instrument")
        research_seen.add(instrument.key)
        instruments[instrument.key] = {"key": instrument.key, "market": instrument.market,
            "ticker": instrument.ticker, "name": instrument.name, "research_enabled": True,
            "monitoring_enabled": False, "rule_count": 0, "configuration": None}
    for ticker, rule_config in parsed.watchlist.items():
        instrument = _instrument(rule_config.market, ticker, rule_config.name)
        row = instruments.setdefault(instrument.key, {"key": instrument.key, "market": instrument.market,
            "ticker": instrument.ticker, "name": instrument.name, "research_enabled": False})
        row.update(monitoring_enabled=rule_config.enabled,
                   rule_count=len(rule_config.buy_rules) + len(rule_config.sell_rules),
                   configuration=rule_config.model_dump(mode="json"))
        if not row["name"]:
            row["name"] = instrument.name
    revision = _revision(raw, settings, identities)
    if key is not None:
        if key not in instruments:
            raise ValueError("watchlist instrument not found")
        item = dict(instruments[key])
        configuration = item.get("configuration")
        if configuration is not None and any(
            len(configuration[side]) > MAX_RULES_PER_SIDE for side in ("buy_rules", "sell_rules")
        ):
            # Legacy YAML has no rule-count limit. Never transmit an incomplete
            # configuration that a later save could mistake for all old rules.
            item.pop("configuration")
            item["rules_editable"] = False
            item["rules_edit_message"] = "此证券规则数超出向导上限（每个方向 100 条），请使用原配置文件；不会保存不完整规则。"
        else:
            item["rules_editable"] = True
        return {"revision": revision, "item": item}
    matching = [instruments[identity] for identity in sorted(instruments)
                if query.casefold() in f"{identity} {instruments[identity]['name']}".casefold()]
    items = [{field: value for field, value in item.items() if field != "configuration"}
             for item in matching[offset:offset + 20]]
    return {"revision": revision, "items": items, "offset": offset, "query": query,
            "next_offset": offset + 20 if offset + 20 < len(matching) else None,
            "total": len(matching)}


def _expect(fields: Mapping[str, Any], required: set[str]) -> None:
    if not isinstance(fields, Mapping) or set(fields) != required:
        raise ValueError("invalid watchlist command fields")


def _expected(value: Any, revision: str) -> None:
    if not isinstance(value, str) or value != revision:
        raise WatchlistConflictError("configuration changed; refresh and preview again")


def _candidate(values: dict[str, Any], parsed: RulesConfig, instrument: ResearchInstrument,
               configuration: Any, *, assign_ids: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    if instrument.market == "CN":
        raise ValueError("China securities currently support research only")
    if not isinstance(configuration, dict):
        raise ValueError("rule configuration must be an object")
    supplied = dict(configuration)
    if supplied.get("market") != instrument.market:
        raise ValueError("rule market must match selected instrument")
    for side in ("buy_rules", "sell_rules"):
        rules = supplied.get(side, [])
        if not isinstance(rules, list) or len(rules) > MAX_RULES_PER_SIDE:
            raise ValueError("rule list is invalid or too large")
        copied = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("each rule must be an object")
            copy = dict(rule)
            if not copy.get("id"):
                if not assign_ids:
                    raise ValueError("save requires previewed stable rule ids")
                copy["id"] = f"{instrument.market.lower()}-{instrument.ticker.lower()}-{uuid.uuid4().hex[:16]}"
            copied.append(copy)
        supplied[side] = copied
    item = InstrumentConfig.model_validate(supplied)
    if item.enabled and not item.buy_rules and not item.sell_rules:
        raise ValueError("enabled monitoring requires at least one rule")
    watchlist = dict(values.get("watchlist") or {})
    for key, existing in parsed.watchlist.items():
        if _instrument(existing.market, key).key == instrument.key:
            if any(len(getattr(existing, side)) > MAX_RULES_PER_SIDE for side in ("buy_rules", "sell_rules")):
                raise ValueError("existing instrument exceeds the rule editor limit; use the original configuration")
            del watchlist[key]
    if instrument.ticker in watchlist:
        raise ValueError("ticker is already assigned to another market")
    normalized = item.model_dump(mode="json")
    watchlist[instrument.ticker] = normalized
    candidate = {**values, "watchlist": watchlist}
    RulesConfig.model_validate(candidate)  # Includes global rule-id uniqueness.
    return candidate, normalized


def _preview_token(revision: str, instrument: ResearchInstrument, configuration: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([revision, instrument.key, configuration],
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _atomic_rules(path: Path, previous: bytes, candidate: dict[str, Any]) -> None:
    encoded = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode()
    if len(encoded) > MAX_RULES_BYTES:
        raise ValueError("updated rule configuration is too large")
    # Backup exact original bytes before the replacement, including comments.
    backups = path.parent / "backups"
    backups.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backups / f"{path.name}.{stamp}.{hashlib.sha256(previous).hexdigest()[:12]}.bak"
    with backup.open("xb") as stream:
        os.chmod(backup, 0o600)
        stream.write(previous)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        # Editors outside the application do not participate in our lease.
        # Recheck immediately before replacement instead of overwriting an edit.
        if path.is_symlink() or path.read_bytes() != previous:
            raise WatchlistConflictError("configuration changed; refresh and preview again")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def execute_watchlist(database: Path, command: str, fields: Mapping[str, Any], *,
                      rules_path: Path | None = None) -> dict[str, Any]:
    """Execute an authenticated local command; never fetch data or start scanning."""
    shapes = {
        "watchlist_get": set(),
        "watchlist_save": {"market", "ticker", "name", "research_enabled", "expected_revision"},
        "rules_preview": {"market", "ticker", "configuration", "expected_revision"},
        "rules_save": {"market", "ticker", "configuration", "expected_revision", "preview_token"},
    }
    if command not in shapes:
        raise ValueError("unsupported watchlist command")
    if not isinstance(fields, Mapping):
        raise ValueError("invalid watchlist command fields")
    if command == "watchlist_get":
        if set(fields) - {"offset", "query", "key"} or ("key" in fields and set(fields) != {"key"}):
            raise ValueError("invalid watchlist query fields")
        offset, query, key = fields.get("offset", 0), fields.get("query", ""), fields.get("key")
        if type(offset) is not int or offset < 0 or offset > 1_000_000 or not isinstance(query, str) or len(query) > 200:
            raise ValueError("invalid watchlist page")
        if "key" in fields:
            if not isinstance(key, str) or ":" not in key:
                raise ValueError("invalid watchlist instrument key")
            market, ticker = key.split(":", 1)
            key = _instrument(market, ticker).key
    else:
        _expect(fields, shapes[command])
    path = (rules_path if rules_path is not None else config.RULES_CONFIG_PATH).expanduser().absolute()
    if command == "watchlist_get":
        raw, _, parsed = _rules(path)
        with WatchlistStore(database) as store:
            return _view(raw, parsed, store.load_settings(), store.identities(), offset=offset, query=query, key=key)
    # The database lease excludes research and digest workers. The path lease
    # additionally serializes clients with different databases sharing rules.
    with ResearchLease(database), ResearchLease(path), WatchlistStore(database) as store:
        raw, values, parsed = _rules(path)
        settings = store.load_settings()
        revision = _revision(raw, settings, store.identities())
        _expected(fields["expected_revision"], revision)
        instrument = _instrument(fields["market"], fields["ticker"], fields.get("name", ""))
        if command == "watchlist_save":
            enabled = fields["research_enabled"]
            if type(enabled) is not bool:
                raise ValueError("research_enabled must be boolean")
            # The form owns name and switches, not exchange metadata supplied
            # by existing research configuration. Retain it even across disable.
            previous = next((value for value in [*settings["instruments"], *store.identities()]
                if _instrument(value["market"], value["ticker"]).key == instrument.key), None)
            if previous is not None:
                instrument = ResearchInstrument.model_validate({**instrument.model_dump(mode="json"),
                    "exchange": previous.get("exchange", "")})
            instruments = [value for value in settings["instruments"]
                           if _instrument(value["market"], value["ticker"]).key != instrument.key]
            if enabled:
                instruments.append(instrument.model_dump(mode="json"))
                if instrument.market not in settings["markets"]:
                    settings["markets"] = [*settings["markets"], instrument.market]
            if len(instruments) > 100:
                raise ValueError("at most 100 research securities are supported")
            settings["instruments"] = instruments
            store.save_identity_and_settings(instrument, settings)
            return {**_view(raw, parsed, store.load_settings(), store.identities()), "saved_key": instrument.key}
        candidate, configuration = _candidate(values, parsed, instrument, fields["configuration"],
                                               assign_ids=command == "rules_preview")
        token = _preview_token(revision, instrument, configuration)
        if command == "rules_preview":
            return {"revision": revision, "key": instrument.key, "configuration": configuration,
                    "preview_token": token, "summary": {
                        "monitoring_enabled": configuration["enabled"],
                        "currency": configuration["currency"],
                        "rule_count": len(configuration["buy_rules"]) + len(configuration["sell_rules"]),
                        "alert_cooldown_hours": configuration["alert_cooldown_hours"],
                        "research_unchanged": True}}
        if not isinstance(fields["preview_token"], str) or fields["preview_token"] != token:
            raise ValueError("rules must match the confirmed preview")
        _atomic_rules(path, raw, candidate)
        updated_raw, _, updated_parsed = _rules(path)
        return {**_view(updated_raw, updated_parsed, settings, store.identities()), "saved_key": instrument.key}


__all__ = ["WatchlistConflictError", "execute_watchlist"]
