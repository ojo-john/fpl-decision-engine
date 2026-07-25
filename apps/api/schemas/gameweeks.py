"""Pydantic response schemas for the /gameweeks endpoints."""

from datetime import datetime

from pydantic import BaseModel


class NextDeadlineOut(BaseModel):
    """The next unfinished gameweek's deadline, plus the real optimization-gate verdict for it --
    for a Dashboard countdown. Not an approximation of gate status from ``is_current`` alone;
    ``gate_allowed``/``gate_reason_code`` come straight from
    ``app.optimization.gate.check_optimization_gate``, the same authoritative check every gated
    endpoint (``SQUAD_OPTIMIZATION``/``LINEUP_OPTIMIZATION``/``TRANSFER_OPTIMIZATION``/
    ``CHIP_SIMULATION``) already uses.
    """

    fpl_event_id: int
    name: str
    deadline_time: datetime
    gate_allowed: bool
    gate_reason_code: str | None
