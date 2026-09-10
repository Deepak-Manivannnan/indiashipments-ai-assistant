"""FastAPI application.

`/chat` runs one turn of the agent loop and returns everything the UI needs:
the reply, any selectable options, and the live state of the draft. The
read-only shipment endpoints remain, so the persisted data can be inspected
independently of the conversation.
"""

from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agent.loop import reset_conversation, run_turn
from app.auth import authenticate, create_customer, list_demo_accounts
from app.db import get_db
from app.models import Shipment
from app.schemas import (
    ChatRequest,
    ChatResponse,
    ResetRequest,
    SignInRequest,
    SignUpRequest,
    ShipmentDetailOut,
    ShipmentSummaryOut,
    TrackingOut,
)
from app.tools import (
    bind_session,
    build_tracking_response,
    list_my_shipments,
    submit_document,
)

# Uploaded documents are kept out of the repository.
UPLOAD_DIR = Path("uploads")

app = FastAPI(
    title="IndiaShipments Agent API",
    version="0.1.0",
    description="Backend for the IndiaShipments conversational shipment agent.",
)


@app.post("/auth/signup")
def signup(request: SignUpRequest) -> dict:
    result = create_customer(**request.model_dump())
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/auth/signin")
def signin(request: SignInRequest) -> dict:
    result = authenticate(request.email, request.password)
    if not result["ok"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result


@app.get("/auth/demo-accounts")
def demo_accounts() -> dict:
    """Shown on the sign-in page so a reviewer can get in with one click."""
    return {"accounts": list_demo_accounts()}


@app.get("/me/shipments")
def my_shipments(session_id: str) -> dict:
    result = list_my_shipments(session_id)
    if not result["ok"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """One conversational turn.

    The agent loop runs here; the UI stays a thin client that renders whatever
    this returns, including the selectable options and the live draft state.
    """
    if request.customer_id is not None:
        bind_session(request.session_id, request.customer_id)
    try:
        result = run_turn(request.session_id, request.message)
    except RuntimeError as exc:  # missing API key, misconfiguration
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ChatResponse(**result)


@app.post("/chat/upload", response_model=ChatResponse)
async def chat_upload(
    session_id: str = Form(...),
    doc_type: str = Form(...),
    file: UploadFile = File(...),
) -> ChatResponse:
    """Attach a supporting document to the conversation's draft.

    The file arrives here rather than through the model: a model cannot be
    handed a file and asked to vouch for it. The tool records it, and the agent
    is then told what happened so it can respond in the conversation.
    """
    payload = await file.read()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_path = UPLOAD_DIR / f"{session_id}-{doc_type}-{file.filename}"
    stored_path.write_bytes(payload)

    result = submit_document(
        session_id=session_id,
        doc_type=doc_type,
        filename=file.filename,
        stored_path=str(stored_path),
        size_bytes=len(payload),
        content_type=file.content_type,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))

    return ChatResponse(
        **run_turn(
            session_id,
            f"[The user has attached a file named '{file.filename}' for the "
            f"{doc_type} requirement. It has been received and recorded.]",
        )
    )


@app.post("/chat/reset")
def chat_reset(request: ResetRequest) -> dict:
    """Start the conversation over, discarding its transcript and draft."""
    reset_conversation(request.session_id)
    return {"ok": True, "session_id": request.session_id}


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
