"""Response models for the read-only endpoints."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TrackingEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    status: str
    event_time: datetime
    location: str | None = None
    note: str | None = None


class ShipmentSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reference: str | None
    status: str
    service_type: str | None
    contents: str | None
    declared_value: float | None
    created_at: datetime
    updated_at: datetime


class ShipmentDetailOut(ShipmentSummaryOut):
    sender_json: dict | None = None
    recipient_json: dict | None = None
    package_json: dict | None = None
    validated: bool
    insurance_ack: bool
    tracking_events: list[TrackingEventOut] = []


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ChatResponse(BaseModel):
    """What one agent turn returns.

    `options` drives the selectable buttons in the UI and is always drawn from
    the application's own lists; `allow_free_text` stays true so the user can
    answer with something not on the list.
    """

    reply: str
    options: list[str] = []
    expects: str | None = None
    allow_free_text: bool = True
    state: dict = {}
    tool_calls: list[dict] = []
    degraded: bool = False


class ResetRequest(BaseModel):
    session_id: str


class TrackingOut(BaseModel):
    """Shaped after the brief: latest status, latest event time, plain-language line."""

    reference: str
    status: str
    last_event_time: datetime | None
    last_location: str | None
    explanation: str
    events: list[TrackingEventOut]
