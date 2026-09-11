"""Customer accounts.

Password hashing uses PBKDF2-HMAC-SHA256 from the standard library rather than
bcrypt, to avoid a compiled dependency for what this challenge needs. It is a
real salted KDF, not a plain hash -- but this is not a production auth stack:
there are no sessions, tokens, lockouts or password resets, and that is stated
as a known limitation.
"""

import hashlib
import hmac
import secrets

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Customer
from app.rules import PHONE_DIGITS, is_valid_phone, is_valid_pin

PBKDF2_ITERATIONS = 200_000
SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # Constant-time comparison, so a wrong password cannot be narrowed down by
    # timing how long the check took.
    return hmac.compare_digest(computed.hex(), digest_hex)


def _as_public(customer: Customer) -> dict:
    """A customer without the password hash. Never return the hash anywhere."""
    return {
        "id": customer.id,
        "email": customer.email,
        "name": customer.name,
        "phone": customer.phone,
        "address": customer.address,
        "city": customer.city,
        "state": customer.state,
        "pin": customer.pin,
        "is_demo": customer.is_demo,
    }


def create_customer(
    email: str,
    password: str,
    name: str,
    phone: str | None = None,
    address: str | None = None,
    city: str | None = None,
    state: str | None = None,
    pin: str | None = None,
    is_demo: bool = False,
) -> dict:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "error": "Please enter a valid email address."}
    if len(password or "") < 8:
        return {"ok": False, "error": "Password must be at least 8 characters."}
    if not (name or "").strip():
        return {"ok": False, "error": "Please enter your name."}

    # The profile is the source of the pre-filled sender block, so anything
    # accepted here is something a shipment will later be validated against.
    # Checking it at the form, with the same rules, means the customer hears
    # about a mistyped PIN while they are looking at the field -- not three
    # questions into booking a parcel. These fields are also narrower in the
    # database than a form will happily submit, and an over-long one is a
    # database error rather than a sentence unless it is caught here.
    name = name.strip()
    phone = (phone or "").strip() or None
    address = (address or "").strip() or None
    city = (city or "").strip() or None
    state = (state or "").strip() or None
    pin = (pin or "").strip() or None

    for label, value, limit in [
        ("name", name, 120), ("address", address, 255),
        ("city", city, 120), ("state", state, 120), ("phone", phone, 20),
    ]:
        if value and len(value) > limit:
            return {
                "ok": False,
                "error": f"That {label} is too long -- please keep it under "
                         f"{limit} characters.",
            }

    if phone and not is_valid_phone(phone):
        return {
            "ok": False,
            "error": f"Please enter a valid {PHONE_DIGITS}-digit phone number.",
        }
    if pin and not is_valid_pin(pin):
        return {
            "ok": False,
            "error": "Please enter a valid six-digit PIN code.",
        }

    db = SessionLocal()
    try:
        existing = db.scalars(
            select(Customer).where(Customer.email == email)
        ).one_or_none()
        if existing is not None:
            return {
                "ok": False,
                "error": "An account with that email already exists. Please sign in.",
            }

        customer = Customer(
            email=email,
            password_hash=hash_password(password),
            name=name.strip(),
            phone=phone,
            address=address,
            city=city,
            state=state,
            pin=pin,
            is_demo=is_demo,
        )
        db.add(customer)
        db.commit()
        return {"ok": True, "customer": _as_public(customer)}
    finally:
        db.close()


def authenticate(email: str, password: str) -> dict:
    db = SessionLocal()
    try:
        customer = db.scalars(
            select(Customer).where(Customer.email == (email or "").strip().lower())
        ).one_or_none()
        # The same message for an unknown email and a wrong password, so the
        # form cannot be used to discover which addresses have accounts.
        if customer is None or not verify_password(password, customer.password_hash):
            return {"ok": False, "error": "Email or password is incorrect."}
        return {"ok": True, "customer": _as_public(customer)}
    finally:
        db.close()


def get_customer(customer_id: int) -> dict | None:
    db = SessionLocal()
    try:
        customer = db.get(Customer, customer_id)
        return _as_public(customer) if customer else None
    finally:
        db.close()


def list_demo_accounts() -> list[dict]:
    """Demo logins, surfaced on the sign-in page so a reviewer never types."""
    db = SessionLocal()
    try:
        return [
            _as_public(c)
            for c in db.scalars(
                select(Customer).where(Customer.is_demo.is_(True)).order_by(Customer.id)
            )
        ]
    finally:
        db.close()
