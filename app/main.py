"""FastAPI application.

Phase 1 exposes read-only endpoints so the seeded data is verifiable before any
agent, tools or LLM exist. Booking happens through the agent in later phases.
"""

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Shipment
from app.schemas import ShipmentDetailOut, ShipmentSummaryOut, TrackingOut
from app.tools import build_tracking_response

app = FastAPI(
    title="IndiaShipments Agent API",
    version="0.1.0",
    description="Backend for the IndiaShipments conversational shipment agent.",
)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    """Liveness plus a real database round trip."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception as exc:  # surfaced honestly rather than reported as healthy
        raise HTTPException(
            status_code=503, detail=f"database unavailable: {exc}"
        ) from exc


@app.get("/shipments", response_model=list[ShipmentSummaryOut])
def list_shipments(db: Session = Depends(get_db)):
    return db.scalars(select(Shipment).order_by(Shipment.id)).all()


def _get_by_reference(db: Session, reference: str) -> Shipment:
    shipment = db.scalars(
        select(Shipment).where(Shipment.reference == reference)
    ).one_or_none()
    if shipment is None:
        raise HTTPException(status_code=404, detail=f"no shipment {reference}")
    return shipment


@app.get("/shipments/{reference}", response_model=ShipmentDetailOut)
def get_shipment(reference: str, db: Session = Depends(get_db)):
    return _get_by_reference(db, reference)


@app.get("/shipments/{reference}/tracking", response_model=TrackingOut)
def get_shipment_tracking(reference: str, db: Session = Depends(get_db)):
    """Tracking view, built only from stored events.

    Shares its wording with the `get_tracking` tool so the API and the agent can
    never drift apart.
    """
    shipment = _get_by_reference(db, reference)
    return TrackingOut(**build_tracking_response(shipment))
