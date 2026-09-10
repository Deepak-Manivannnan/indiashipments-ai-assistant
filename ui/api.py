"""Thin client for the FastAPI backend.

The interface holds no business logic of its own. Every rule, every status and
every decision shown on screen came from the backend, which is what keeps the
UI honest: if it renders something, a tool returned it.
"""

import os

import requests

BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
TIMEOUT_SECONDS = float(os.getenv("API_TIMEOUT_SECONDS", "120"))


class BackendUnavailable(Exception):
    """The API could not be reached. Shown to the user, never swallowed."""


def _request(method: str, path: str, **kwargs) -> tuple[bool, dict]:
    """Returns (ok, payload). A 4xx carries a readable `detail` to display."""
    try:
        response = requests.request(
            method, f"{BASE_URL}{path}", timeout=TIMEOUT_SECONDS, **kwargs
        )
    except requests.RequestException as exc:
        raise BackendUnavailable(
            f"Could not reach the IndiaShipments API at {BASE_URL}. "
            "Is the backend running?"
        ) from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"detail": response.text or "The server returned no content."}

    return response.ok, payload


def health() -> tuple[bool, dict]:
    return _request("GET", "/health")


# --- accounts ---

def sign_in(email: str, password: str) -> tuple[bool, dict]:
    return _request("POST", "/auth/signin", json={"email": email, "password": password})


def sign_up(**fields) -> tuple[bool, dict]:
    return _request("POST", "/auth/signup", json=fields)


def demo_accounts() -> list[dict]:
    ok, payload = _request("GET", "/auth/demo-accounts")
    return payload.get("accounts", []) if ok else []


# --- conversation ---

def bind_session(session_id: str, customer_id: int) -> bool:
    ok, _ = _request(
        "POST",
        "/chat/bind",
        json={"session_id": session_id, "message": "", "customer_id": customer_id},
    )
    return ok


def send_message(session_id: str, message: str, customer_id: int | None) -> dict:
    ok, payload = _request(
        "POST",
        "/chat",
        json={
            "session_id": session_id,
            "message": message,
            "customer_id": customer_id,
        },
    )
    if not ok:
        raise BackendUnavailable(payload.get("detail", "The assistant is unavailable."))
    return payload


def upload_document(session_id: str, doc_type: str, file) -> dict:
    ok, payload = _request(
        "POST",
        "/chat/upload",
        data={"session_id": session_id, "doc_type": doc_type},
        files={"file": (file.name, file.getvalue(), file.type)},
    )
    if not ok:
        raise BackendUnavailable(payload.get("detail", "The upload failed."))
    return payload


def reset_conversation(session_id: str) -> None:
    _request("POST", "/chat/reset", json={"session_id": session_id})


# --- shipments ---

def my_shipments(session_id: str) -> list[dict]:
    ok, payload = _request("GET", "/me/shipments", params={"session_id": session_id})
    return payload.get("shipments", []) if ok else []


def tracking(reference: str) -> tuple[bool, dict]:
    return _request("GET", f"/shipments/{reference.strip().upper()}/tracking")
