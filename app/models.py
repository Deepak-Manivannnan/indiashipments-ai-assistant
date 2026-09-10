"""ORM models for the shipment domain.

Person/package details are stored as JSON columns because the agent fills them
in progressively across a conversation; a half-filled draft is normal and must
still persist.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import DOC_PENDING, STATUS_DRAFT
from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Shipment(Base):
    __tablename__ = "shipments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Assigned only at confirm_booking; a draft has no reference yet.
    reference: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), default=STATUS_DRAFT, index=True)

    sender_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recipient_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    package_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    service_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contents: Mapped[str | None] = mapped_column(String(255), nullable=True)
    declared_value: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Guardrail flags. confirm_booking refuses unless these are satisfied.
    validated: Mapped[bool] = mapped_column(Boolean, default=False)
    insurance_ack: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    tracking_events: Mapped[list["TrackingEvent"]] = relationship(
        back_populates="shipment",
        cascade="all, delete-orphan",
        order_by="TrackingEvent.event_time",
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan"
    )


class TrackingEvent(Base):
    __tablename__ = "tracking_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), index=True
    )

    status: Mapped[str] = mapped_column(String(32))
    event_time: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    shipment: Mapped["Shipment"] = relationship(back_populates="tracking_events")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    shipment_id: Mapped[int] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"), index=True
    )

    doc_type: Mapped[str] = mapped_column(String(64))  # e.g. "prescription"
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stored_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=DOC_PENDING)

    # File metadata only -- name, size, type. This build deliberately does not
    # read document contents: review is simulated, so nothing here is ever
    # presented to the user as a verified fact about the document.
    file_metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    requested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    shipment: Mapped["Shipment"] = relationship(back_populates="documents")


class ConversationState(Base):
    __tablename__ = "conversation_state"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    draft_shipment_id: Mapped[int | None] = mapped_column(
        ForeignKey("shipments.id", ondelete="SET NULL"), nullable=True
    )
    # Carried back into the next model turn so the agent can self-correct.
    last_tool_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
