"""In-process backend, used when there is no API server to talk to.

The deployed build runs the interface and the application in a single process,
because free hosting for a separate API service sleeps between requests and a
reviewer's first message would sit there for a minute.

Nothing is reimplemented here. Every function calls the same tools, rules and
database as the HTTP API in app/main.py, which remains the way the application
runs locally. Only the transport differs: a function call instead of a request.
"""

from pathlib import Path

from sqlalchemy import text

from app import tools
from app.agent.loop import reset_conversation as _reset
from app.agent.loop import run_turn
from app.auth import authenticate, create_customer, get_customer, list_demo_accounts
from app.db import SessionLocal

UPLOAD_DIR = Path("uploads")


def health() -> tuple[bool, dict]:
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return True, {"status": "ok", "database": "connected"}
    except Exception as exc:
        return False, {"detail": f"database unavailable: {exc}"}
    finally:
        db.close()


# --- accounts ---

def sign_in(email: str, password: str) -> tuple[bool, dict]:
    result = authenticate(email, password)
    return (True, result) if result["ok"] else (False, {"detail": result["error"]})


def sign_up(**fields) -> tuple[bool, dict]:
    result = create_customer(**fields)
    return (True, result) if result["ok"] else (False, {"detail": result["error"]})


def demo_accounts() -> list[dict]:
    return list_demo_accounts()


# --- conversation ---

def bind_session(session_id: str, customer_id: int) -> bool:
    return bool(tools.bind_session(session_id, customer_id).get("ok"))


def session_customer(session_id: str) -> dict | None:
    customer_id = tools.session_customer_id(session_id)
    return get_customer(customer_id) if customer_id else None


def send_message(session_id: str, message: str, customer_id: int | None) -> dict:
    if customer_id is not None:
        tools.bind_session(session_id, customer_id)
    return run_turn(session_id, message)


def upload_document(session_id: str, doc_type: str, file) -> dict:
    payload = file.getvalue()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored = UPLOAD_DIR / f"{session_id}-{doc_type}-{file.name}"
    stored.write_bytes(payload)

    result = tools.submit_document(
        session_id=session_id, doc_type=doc_type, filename=file.name,
        stored_path=str(stored), size_bytes=len(payload), content_type=file.type,
    )
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "The upload failed."))

    return run_turn(
        session_id,
        f"[The user has just attached a file named '{file.name}' for the "
        f"{doc_type} requirement. It has been received and recorded, and the "
        f"{doc_type} requirement is now satisfied. Acknowledge that it has been "
        "received and recorded -- not verified, checked or approved, and say "
        "nothing about what it contains, because nothing has read it. Do NOT "
        f"ask for the {doc_type} again. Then continue with the next detail "
        "the shipment still needs.]",
    )


def reset_conversation(session_id: str) -> None:
    _reset(session_id)


# --- shipments ---

def my_shipments(session_id: str) -> list[dict]:
    result = tools.list_my_shipments(session_id)
    return result.get("shipments", []) if result.get("ok") else []


def tracking(reference: str) -> tuple[bool, dict]:
    result = tools.get_tracking(reference)
    if not result.get("ok"):
        return False, {"detail": result.get("error", "not found")}
    return True, result
