"""Seed the three demonstrable tracking scenarios required by the brief.

    python -m scripts.seed

Re-running replaces the seeded shipments (matched by reference) so the demo
data stays predictable. Shipments booked through the agent are left alone.

Scenarios:
  RI-1001  Delivered            -- full, clean event history
  RI-1042  Out for delivery     -- in progress
  RI-1077  Delivery failed      -- the problem case, with a delay before it
"""

from datetime import datetime, timedelta, timezone

from app.constants import (
    SERVICE_EXPRESS,
    SERVICE_STANDARD,
    STATUS_BOOKED,
    STATUS_DELIVERED,
    STATUS_DELIVERY_FAILED,
    STATUS_IN_TRANSIT,
    STATUS_OUT_FOR_DELIVERY,
    STATUS_PICKED_UP,
)
from app.auth import hash_password
from app.db import SessionLocal
from app.models import Customer, Shipment, TrackingEvent

NOW = datetime.now(timezone.utc).replace(microsecond=0)

# Demo logins shown on the sign-in page, so a reviewer never has to register.
# Each owns one of the seeded scenarios, which is what makes their Orders page
# show something different from each other.
DEMO_PASSWORD = "demo1234"

DEMO_CUSTOMERS = [
    {
        "email": "rahul@example.com",
        "name": "Rahul Menon",
        "phone": "9847012345",
        "address": "12 Marine Drive",
        "city": "Kochi",
        "state": "Kerala",
        "pin": "682031",
        "owns": "RI-1001",
        "shows": "a delivered shipment",
    },
    {
        "email": "priya@example.com",
        "name": "Priya Raghavan",
        "phone": "9840055667",
        "address": "8 Anna Salai",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "pin": "600002",
        "owns": "RI-1042",
        "shows": "a shipment out for delivery",
    },
    {
        "email": "imran@example.com",
        "name": "Imran Shaikh",
        "phone": "9820011223",
        "address": "5 Linking Road",
        "city": "Mumbai",
        "state": "Maharashtra",
        "pin": "400050",
        "owns": "RI-1077",
        "shows": "a failed delivery",
    },
]


def ago(days: float = 0, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days, hours=hours)


def person(name, phone, address, city, state, pin):
    return {
        "name": name,
        "phone": phone,
        "address": address,
        "city": city,
        "state": state,
        "pin": pin,
    }


def package(weight_g, length_mm, width_mm, height_mm):
    return {
        "weight_g": weight_g,
        "length_mm": length_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
    }


SEEDS = [
    {
        "reference": "RI-1001",
        "status": STATUS_DELIVERED,
        "sender_json": person(
            "Rahul Menon", "9847012345", "12 Marine Drive", "Kochi", "Kerala", "682031"
        ),
        "recipient_json": person(
            "Anil Kumar", "9880123456", "44 MG Road", "Bengaluru", "Karnataka", "560001"
        ),
        "package_json": package(2000, 300, 200, 150),
        "service_type": SERVICE_STANDARD,
        "contents": "Documents",
        "declared_value": 1500.00,
        "events": [
            (STATUS_BOOKED, ago(days=6), "Kochi", "Booking confirmed and payment recorded."),
            (STATUS_PICKED_UP, ago(days=5, hours=20), "Kochi", "Parcel collected from the sender."),
            (STATUS_IN_TRANSIT, ago(days=5), "Kochi hub", "Departed origin facility."),
            (STATUS_IN_TRANSIT, ago(days=4), "Salem hub", "Processed at transit facility."),
            (STATUS_OUT_FOR_DELIVERY, ago(days=3, hours=6), "Bengaluru", "Out with the delivery agent."),
            (STATUS_DELIVERED, ago(days=3), "Bengaluru", "Delivered and signed for by Anil Kumar."),
        ],
    },
    {
        "reference": "RI-1042",
        "status": STATUS_OUT_FOR_DELIVERY,
        "sender_json": person(
            "Priya Raghavan", "9840055667", "8 Anna Salai", "Chennai", "Tamil Nadu", "600002"
        ),
        "recipient_json": person(
            "Sneha Reddy", "9701122334", "21 Banjara Hills", "Hyderabad", "Telangana", "500034"
        ),
        "package_json": package(1200, 250, 180, 120),
        "service_type": SERVICE_EXPRESS,
        "contents": "Books",
        "declared_value": 3200.00,
        "events": [
            (STATUS_BOOKED, ago(days=2), "Chennai", "Booking confirmed."),
            (STATUS_PICKED_UP, ago(days=1, hours=20), "Chennai", "Parcel collected from the sender."),
            (STATUS_IN_TRANSIT, ago(days=1, hours=10), "Chennai hub", "Departed origin facility."),
            (STATUS_IN_TRANSIT, ago(hours=14), "Hyderabad hub", "Arrived at destination facility."),
            (STATUS_OUT_FOR_DELIVERY, ago(hours=4), "Hyderabad", "Out with the delivery agent."),
        ],
    },
    {
        "reference": "RI-1077",
        "status": STATUS_DELIVERY_FAILED,
        "sender_json": person(
            "Imran Shaikh", "9820011223", "5 Linking Road", "Mumbai", "Maharashtra", "400050"
        ),
        "recipient_json": person(
            "Kavita Sharma", "9414099887", "17 Civil Lines", "Jaipur", "Rajasthan", "302006"
        ),
        "package_json": package(4500, 400, 300, 200),
        "service_type": SERVICE_STANDARD,
        "contents": "Kitchenware (fragile)",
        "declared_value": 8900.00,
        "events": [
            (STATUS_BOOKED, ago(days=8), "Mumbai", "Booking confirmed."),
            (STATUS_PICKED_UP, ago(days=7, hours=18), "Mumbai", "Parcel collected from the sender."),
            (STATUS_IN_TRANSIT, ago(days=7), "Mumbai hub", "Departed origin facility."),
            (STATUS_IN_TRANSIT, ago(days=5), "Ahmedabad hub", "Held at transit facility; onward transport delayed."),
            (STATUS_IN_TRANSIT, ago(days=3), "Jaipur hub", "Arrived at destination facility."),
            (STATUS_OUT_FOR_DELIVERY, ago(days=2, hours=6), "Jaipur", "Out with the delivery agent."),
            (STATUS_DELIVERY_FAILED, ago(days=2), "Jaipur", "Delivery attempted; recipient not available at the address."),
        ],
    },
]


def seed_customers(db) -> dict:
    """Create the demo accounts. Returns reference -> customer id."""
    owners = {}
    for spec in DEMO_CUSTOMERS:
        customer = (
            db.query(Customer).filter_by(email=spec["email"]).one_or_none()
        )
        if customer is None:
            customer = Customer(
                email=spec["email"],
                password_hash=hash_password(DEMO_PASSWORD),
                name=spec["name"],
                phone=spec["phone"],
                address=spec["address"],
                city=spec["city"],
                state=spec["state"],
                pin=spec["pin"],
                is_demo=True,
            )
            db.add(customer)
            db.flush()
        owners[spec["owns"]] = customer.id
        print(f"demo account {spec['email']:<20} {spec['shows']}")
    return owners


def main() -> None:
    db = SessionLocal()
    try:
        owners = seed_customers(db)
        print()
        for spec in SEEDS:
            events = spec["events"]
            fields = {k: v for k, v in spec.items() if k != "events"}
            existing = (
                db.query(Shipment).filter_by(reference=spec["reference"]).one_or_none()
            )
            if existing:
                db.delete(existing)  # cascades to its tracking events
                db.flush()

            shipment = Shipment(
                **fields,
                validated=True,
                customer_id=owners.get(spec["reference"]),
            )
            shipment.tracking_events = [
                TrackingEvent(status=s, event_time=t, location=loc, note=note)
                for s, t, loc, note in events
            ]
            db.add(shipment)
            print(
                f"seeded {spec['reference']:<8} {spec['status']:<16} "
                f"({len(events)} events)"
            )
        db.commit()
        print(f"\nseed complete -- password for every demo account: {DEMO_PASSWORD}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
