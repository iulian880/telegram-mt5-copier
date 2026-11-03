"""Order-leg lifecycle states."""
from __future__ import annotations

import enum


class LegState(str, enum.Enum):
    SUBMITTING = "SUBMITTING"    # persisted, being sent
    UNCERTAIN = "UNCERTAIN"      # send outcome unknown -> must reconcile
    PENDING = "PENDING"          # pending order resting at broker
    OPEN = "OPEN"                # position open
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"        # broker said no (definitive)
    FAILED = "FAILED"            # proven not executed after reconciliation
    CANCELLED = "CANCELLED"      # pending order cancelled / expired
