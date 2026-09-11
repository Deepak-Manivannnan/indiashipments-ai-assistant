"""Shared vocabulary for the shipment domain.

Statuses are stored as plain strings so the lifecycle can be extended without a
schema migration; this module is the single place that defines the legal set.
"""

# --- Lifecycle (brief: Draft -> Booked -> Picked up -> In transit ->
#     Out for delivery -> Delivered, plus exceptional states) ---
STATUS_DRAFT = "Draft"
STATUS_BOOKED = "Booked"
STATUS_PICKED_UP = "Picked up"
STATUS_IN_TRANSIT = "In transit"
STATUS_OUT_FOR_DELIVERY = "Out for delivery"
STATUS_DELIVERED = "Delivered"
STATUS_DELIVERY_FAILED = "Delivery failed"
STATUS_RETURNED = "Returned"
STATUS_CANCELLED = "Cancelled"

NORMAL_LIFECYCLE = [
    STATUS_DRAFT,
    STATUS_BOOKED,
    STATUS_PICKED_UP,
    STATUS_IN_TRANSIT,
    STATUS_OUT_FOR_DELIVERY,
    STATUS_DELIVERED,
]

EXCEPTIONAL_STATUSES = [
    STATUS_DELIVERY_FAILED,
    STATUS_RETURNED,
    STATUS_CANCELLED,
]

ALL_STATUSES = NORMAL_LIFECYCLE + EXCEPTIONAL_STATUSES

# --- Service types ---
SERVICE_STANDARD = "Standard"
SERVICE_EXPRESS = "Express"
SERVICE_TYPES = [SERVICE_STANDARD, SERVICE_EXPRESS]

# --- Document review states ---
DOC_PENDING = "pending"
DOC_ACCEPTED = "accepted"
DOC_REJECTED = "rejected"
DOC_STATUSES = [DOC_PENDING, DOC_ACCEPTED, DOC_REJECTED]

# Reference format for shipments created by this application.
# Deliberately NOT presented to the user as a carrier AWB.
REFERENCE_PREFIX = "RI-"
