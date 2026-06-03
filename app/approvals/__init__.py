"""Shared approval helpers used by both the assistant tool layer and
the UI POST handlers. Kept thin — anything that touches the state
machine or per-asset gating logic lives here so the two callsites can
share it.
"""

from app.approvals.auto_advance import try_advance_after_approval

__all__ = ["try_advance_after_approval"]
