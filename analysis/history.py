"""Persistent score history for change detection and Telegram digests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT

SCORES_HISTORY_PATH = PROJECT_ROOT / "state" / "scores.json"


def load_scores(path: Path | None = None) -> dict[str, Any]:
    """Load the previous score map; missing or corrupt files start fresh."""

    resolved = path or SCORES_HISTORY_PATH
    if not resolved.exists():
        return {}
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_scores(
    scores: dict[str, Any], path: Path | None = None
) -> None:
    """Persist the current score map atomically enough for single-writer use."""

    resolved = path or SCORES_HISTORY_PATH
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        ticker: {
            "total_score": entry.get("total_score"),
            "as_of": entry.get("as_of"),
        }
        for ticker, entry in scores.items()
    }
    resolved.write_text(
        json.dumps(
            {"updated_at": datetime.now(UTC).isoformat(), "scores": payload},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def score_changes(
    previous: dict[str, Any],
    ranked: list[dict[str, Any]],
    *,
    threshold: float = 5.0,
) -> list[dict[str, Any]]:
    """Diff a fresh ranking against history; |delta| >= threshold is a change."""

    changes: list[dict[str, Any]] = []
    for item in ranked:
        ticker = str(item.get("ticker"))
        new_score = item.get("total_score")
        if new_score is None:
            continue
        old_entry = previous.get("scores", previous).get(ticker)
        old_score = old_entry.get("total_score") if isinstance(old_entry, dict) else None
        if old_score is None:
            continue  # first observation is baseline, not a change event
        delta = float(new_score) - float(old_score)
        if abs(delta) >= threshold:
            changes.append(
                {
                    "ticker": ticker,
                    "name": item.get("name"),
                    "old_score": old_score,
                    "new_score": new_score,
                    "delta": round(delta, 1),
                }
            )
    return sorted(changes, key=lambda item: abs(item["delta"]), reverse=True)


def render_score_changes(changes: list[dict[str, Any]]) -> str:
    """Bounded plain-text digest for Telegram."""

    lines = ["Alpha Guard 评分变化", ""]
    for change in changes[:20]:
        arrow = "↑" if change["delta"] > 0 else "↓"
        lines.append(
            f"{arrow} {change['ticker']} {change['name'] or ''}".strip()
            + f"：{change['old_score']} → {change['new_score']}"
            + f"（{change['delta']:+.1f}）"
        )
    lines += [
        "",
        "⚠️ 描述性研究评分变化，不构成投资建议；请核对数据覆盖后再解读。",
    ]
    return "\n".join(lines)
