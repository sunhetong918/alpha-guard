"""Persistent state and trusted-silence domain contracts."""

from .blindness import (
    BlindnessObservation,
    CoverageEvidence,
    ProtectionSnapshot,
    ProtectionState,
    ProtectionTransition,
    protection_observation_identity,
    transition_protection,
)
from .contract import (
    PROTECTION_CONTRACT_SCHEMA_VERSION,
    protection_contract_version,
)
from .store import (
    CorruptProtectionStateError,
    ProtectionObservationCollisionError,
    StateStore,
    watchdog_scope_generation,
)

__all__ = [
    "BlindnessObservation",
    "CoverageEvidence",
    "ProtectionSnapshot",
    "ProtectionState",
    "ProtectionTransition",
    "PROTECTION_CONTRACT_SCHEMA_VERSION",
    "StateStore",
    "CorruptProtectionStateError",
    "ProtectionObservationCollisionError",
    "protection_observation_identity",
    "protection_contract_version",
    "transition_protection",
    "watchdog_scope_generation",
]
