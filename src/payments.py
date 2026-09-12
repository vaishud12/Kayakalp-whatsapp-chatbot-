"""Post-approval payment flow for Kaya.

Timeline after the doctor approves a booking:

    approval -> form-link message -> [24h / 3d / 7d reminders]
        -> form submitted (Apps Script POST) -> UPI instructions
        -> patient sends payment screenshot -> doctor verifies
            |-- approve -> calendar invite + final confirmation (slot booked)
            |-- reject  -> back to awaiting_payment (patient resends)
        -> if still unpaid after expiry -> doctor alert with patient details

No calendar event is ever created until the doctor confirms the payment:
no payment means no slot.
"""

from __future__ import annotations

import re
from typing import Any

from src.config import settings

# Flow statuses
AWAITING_FORM = "awaiting_form"
AWAITING_PAYMENT = "awaiting_payment"
VERIFYING = "verifying"
PAID = "paid"
EXPIRED = "expired"

# In-memory active flows, keyed by wa_id (for production, use Redis/DB)
_flows: dict[str, dict[str, Any]] = {}

# Cache of already-paid wa_ids (populated by _finalize_paid_booking and
# Cashfree webhook).  Checked by _complete_booking before the Google Sheet
# lookup so that a sheet-access failure never resends the screening form to a
# patient who already paid.
_paid_cache: dict[str, dict[str, Any]] = {}  # wa_id -> {"paid_at": iso, "kind": str}


# ---------------------------------------------------------------------------
# Store helpers
# ---------------------------------------------------------------------------
def start_flow(booking: dict[str, Any], kind: str = "appointment") -> dict[str, Any]:
    """Register a new payment flow. kind="appointment" -> screening-call
    booking (calendar on approval); kind="enrollment" -> weight-loss program
    signup (team outreach on approval, no calendar event)."""
    stored = dict(booking)
    stored.setdefault("flow_kind", kind)
    wa_id = booking["wa_id"]
    flow = {
        "booking": stored,
        "kind": kind,
        "status": AWAITING_FORM,
        "form_submitted": False,
        "payment_id": "",
        "created_at": "",
    }
    _flows[wa_id] = flow
    return flow


def get_flow(wa_id: str) -> dict[str, Any] | None:
    return _flows.get(wa_id)


def set_status(wa_id: str, status: str) -> None:
    flow = _flows.get(wa_id)
    if flow:
        flow["status"] = status


def pop_flow(wa_id: str) -> dict[str, Any] | None:
    return _flows.pop(wa_id, None)


def mark_paid(wa_id: str, kind: str = "appointment") -> None:
    """Record that a patient's payment was finalised.  Checked before the
    sheet lookup so that a sheet-access failure never re-sends the form."""
    from datetime import datetime as _dt, timezone as _tz
    _paid_cache[wa_id] = {
        "paid_at": _dt.now(_tz.utc).isoformat(),
        "kind": kind,
    }


def is_already_paid(wa_id: str) -> bool:
    """True when the patient is known to have completed payment (in-memory cache)."""
    return wa_id in _paid_cache


def set_payment_link(wa_id: str, link_id: str, link_url: str) -> None:
    flow = _flows.get(wa_id)
    if flow:
        flow["payment_link_id"] = link_id
        flow["payment_link_url"] = link_url


def find_wa_id_by_link_id(link_id: str) -> str | None:
    for wa_id, flow in _flows.items():
        if flow.get("payment_link_id") == link_id:
            return wa_id
    return None


def find_wa_id_by_email(email_raw: str) -> str | None:
    """Match a form's email to the email the patient already gave during the
    WhatsApp booking flow (await_email step). Case-insensitive exact match —
    email is unique per patient, so this is more reliable than phone matching."""
    email = (email_raw or "").strip().lower()
    if not email:
        return None
    for wa_id, flow in _flows.items():
        booking_email = str(flow.get("booking", {}).get("email", "") or "").strip().lower()
        if booking_email and booking_email == email:
            return wa_id
    return None


def find_wa_id_by_phone(phone_raw: str) -> str | None:
    """Match a phone number typed into a Google Form to a known wa_id."""
    digits = re.sub(r"\D", "", phone_raw or "")
    if not digits:
        return None
    # Exact match first
    if digits in _flows:
        return digits
    # Match by last 10 digits (patients type +91 / 0 / bare numbers)
    tail = digits[-10:] if len(digits) >= 10 else digits
    for wa_id in _flows:
        if wa_id.endswith(tail):
            return wa_id
    return None


def reminder_schedule_seconds() -> list[float]:
    """Cumulative delays (seconds from approval): 24h, 3d, 7d by default."""
    r1 = max(settings.pay_reminder_1_hours, 0.0) * 3600.0
    r2 = r1 + max(settings.pay_reminder_2_days, 0.0) * 86400.0
    exp = r2 + max(settings.pay_expiry_days - settings.pay_reminder_2_days, 0.0) * 86400.0
    return [r1, r2, exp]


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------
from typing import Any

def patient_form_text(booking: dict[str, Any]) -> str:
    """Sent right after doctor approval: instructions to fill form and pay for
    confirmation. The screening form is attached as a tap-to-open 'Fill Form'
    button by the caller (raw-URL fallback if buttons fail)."""
    return (
        "Your appointment request has been received!\n\n"
        "To officially confirm your "
        f"{booking.get('service', '')} session on {booking.get('appt_date', '')} "
        f"at {booking.get('appt_time', '')}, please complete the following steps:\n\n"
        "1. Fill out the screening form.\n"
        "2. Complete your payment.\n\n"
        "⚠️ Please note: Your appointment will ONLY be confirmed once the form is submitted "
        "and payment is successfully processed. Please ensure your email ID is accurate "
        "so we can send the official meeting invite.\n\n"
        "Tap the button below to open the screening form.\n\n"
        "Looking forward to our session! Please log in on time so we can make the most of our slot."
    )


def doctor_approved_text(booking: dict[str, Any]) -> str:
    """Notice sent to the clinic hotline after approving (slot NOT final yet)."""
    return (
        "Booking approved — awaiting patient form + payment:\n\n"
        f"Name: {booking.get('full_name', '')}\n"
        f"Service: {booking.get('service', '')}\n"
        f"Date: {booking.get('appt_date', '')}\n"
        f"Time: {booking.get('appt_time', '')}\n"
        f"Patient phone: {booking.get('wa_id', '')}\n\n"
        "The calendar invite will only be created after their payment is verified."
    )


def doctor_approved_returning_text(booking: dict[str, Any]) -> str:
    """Doctor notice variant when the patient was found in the payments sheet
    with payment still pending: the form step is skipped and the payment
    request goes out directly."""
    return (
        "Booking approved — RETURNING patient (form skipped, details on file).\n"
        "Payment request sent directly:\n\n"
        f"Name: {booking.get('full_name', '')}\n"
        f"Service: {booking.get('service', '')}\n"
        f"Date: {booking.get('appt_date', '')}\n"
        f"Time: {booking.get('appt_time', '')}\n"
        f"Patient phone: {booking.get('wa_id', '')}\n\n"
        "The calendar invite will only be created after their payment is verified."
    )


def returning_patient_pay_text(booking: dict[str, Any]) -> str:
    """Body for the 'Pay Now' button message when a returning patient's
    details are already in the payments sheet but payment is still pending:
    no Google Form — straight to payment."""
    service_info = (
        f"{booking.get('service', '')} session on {booking.get('appt_date', '')} "
        f"at {booking.get('appt_time', '')}"
    )
    return (
        "Welcome back! We found your details in our records, so there is no need "
        "to fill out the screening form again.\n\n"
        f"To confirm your {service_info}, please complete your payment.\n\n"
        "Tap the button below to open the secure payment page."
    )


def returning_patient_upi_text(booking: dict[str, Any]) -> str:
    """Same as returning_patient_pay_text but for the manual-UPI flow
    (Cashfree not configured): UPI details instead of a hosted link."""
    amount_line = f"\nAmount: Rs. {settings.payment_amount}" if settings.payment_amount else ""
    payee_line = f"\nPayee name: {settings.upi_payee_name}" if settings.upi_payee_name else ""
    service_info = (
        f"{booking.get('service', '')} session on {booking.get('appt_date', '')} "
        f"at {booking.get('appt_time', '')}"
    )
    return (
        "Welcome back! We found your details in our records, so there is no need "
        "to fill out the screening form again.\n\n"
        f"To confirm your {service_info}, please complete the payment:"
        f"{amount_line}\n"
        f"UPI ID: {settings.upi_id}"
        f"{payee_line}\n\n"
        "After paying, send a screenshot of the payment confirmation here in this chat. "
        "Once it is verified, your appointment will be added to the calendar and the "
        "invite will be emailed to you."
    )


def already_booked_patient_text() -> str:
    """Sent when the patient already has a PAID screening call on file: no
    new booking, no payment request."""
    return (
        "It seems you have already booked your screening call with us. 😊\n\n"
        "You can explore our programs to continue your journey, or reach out to "
        "the Kayakalp team here and we will be happy to help you."
    )


def already_booked_doctor_text(booking: dict[str, Any]) -> str:
    """Doctor alert: approval arrived for someone whose screening call is
    already paid — nothing was booked or charged."""
    return (
        "DUPLICATE BOOKING BLOCKED — screening call already Paid on file:\n\n"
        f"Name: {booking.get('full_name', '')}\n"
        f"Service: {booking.get('service', '')}\n"
        f"Date: {booking.get('appt_date', '')}\n"
        f"Time: {booking.get('appt_time', '')}\n"
        f"Patient phone: {booking.get('wa_id', '')}\n\n"
        "No calendar event was created and no payment was requested. The "
        "patient has been informed. Please follow up manually if this needs "
        "an exception."
    )


def payment_instructions_text(booking: dict[str, Any]) -> str:
    """Sent when the Apps Script reports the Google Form was submitted."""
    amount_line = ""
    if settings.payment_amount:
        amount_line = f"\nAmount: Rs. {settings.payment_amount}"
    payee_line = f"\nPayee name: {settings.upi_payee_name}" if settings.upi_payee_name else ""
    email = booking.get("email", "")
    email_line = f" Your calendar invite will be sent to {email}." if email else ""

    return (
        "Thank you! Your form has been received.\n\n"
        "To confirm your slot, please complete the payment:\n"
        f"{amount_line}\n"
        f"UPI ID: {settings.upi_id}"
        f"{payee_line}\n\n"
        "After paying, send a screenshot of the payment confirmation here in this chat."
        " Once it is verified, your appointment will be added to the calendar and the "
        "invite will be emailed to you." + email_line
    )


def payment_link_text(booking: dict[str, Any]) -> str:
    """Body of the screening-appointment payment message. The Cashfree link
    is attached as a tap-to-open 'Pay Now' button by the caller (raw-URL
    fallback if buttons fail)."""
    amount_line = f"\nAmount: Rs. {settings.payment_amount}" if settings.payment_amount else ""
    email = booking.get("email", "")
    email_line = f"\n\nYour calendar invite will be sent to {email}." if email else ""

    return (
        "Thank you! Your form has been received.\n\n"
        f"To confirm your slot, please complete the payment.{amount_line}\n\n"
        "Tap the button below to open the secure payment page. Your appointment "
        "will be added to the calendar automatically as soon as the payment is "
        "confirmed." + email_line
    )


def _is_enrollment(booking: dict[str, Any]) -> bool:
    return booking.get("flow_kind") == "enrollment" or "plan" in booking


def _fmt_amount(value: Any) -> str:
    try:
        amt = float(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    return f"{int(amt):,}" if amt == int(amt) else f"{amt:,.2f}"


def reminder_text(booking: dict[str, Any], step_index: int, link_url: str = "") -> str:
    """Payment reminders at 24h (0), 3d (1), 7d (2). When link_url is given
    the caller attaches it as a tap-to-open 'Pay Now' button."""
    appt = (
        f"{booking.get('service', '')} on {booking.get('appt_date', '')} "
        f"at {booking.get('appt_time', '')}"
        if not _is_enrollment(booking)
        else f"{booking.get('plan', 'your program')} enrollment"
    )
    goal = (
        "lock your screening call slot"
        if not _is_enrollment(booking)
        else "confirm your program enrollment"
    )
    pay_action = (
        "complete the payment using the button below"
        if link_url
        else "fill the form and send your payment screenshot in this chat"
    )
    if step_index == 0:
        body = (
            "Gentle reminder: your request is provisionally held but the payment "
            "is still pending.\n\n"
            f"{appt}\n\n"
            f"Please {pay_action} as soon as possible to {goal}."
        )
    elif step_index == 1:
        body = (
            "Reminder: your payment is still pending.\n\n"
            f"{appt}\n\n"
            f"Your provisional hold will be released if the payment is not completed. "
            f"Please {pay_action}."
        )
    else:
        body = (
            "FINAL reminder: this is the last day of your provisional hold.\n\n"
            f"{appt}\n\n"
            "If we do not receive your payment today, your request "
            "will be marked as pending-cancelled and the hold released."
        )
    return body + "\n\nNeed help? Just reply here and our team will assist."


def expired_doctor_text(booking: dict[str, Any], proof_pending: bool = False) -> str:
    """Doctor alert after the timeline passes with no verified payment."""
    head = (
        "PAYMENT PENDING — timeline expired:\n\n"
        f"Name: {booking.get('full_name', '')}\n"
        f"Service: {booking.get('service', '')}\n"
        f"Date: {booking.get('appt_date', '')}\n"
        f"Time: {booking.get('appt_time', '')}\n"
        f"Patient phone: {booking.get('wa_id', '')}\n"
        f"Email: {booking.get('email', '')}\n\n"
    )
    if proof_pending:
        head += (
            "The patient DID send a payment screenshot but it has not been verified "
            "yet. No calendar event was created."
        )
    elif _is_enrollment(booking):
        head += "No payment received within the allowed timeline. The enrollment was not completed."
    else:
        head += "No payment received within the allowed timeline. No slot was booked."
    return head


def unmatched_form_doctor_text(phone_raw: str, name: str, email: str) -> str:
    """Alert the doctor when a form submission's phone doesn't match any
    open WhatsApp payment flow — the patient will get no automatic reply."""
    return (
        "FORM SUBMITTED — could not match to a WhatsApp chat:\n\n"
        f"Phone entered on form: {phone_raw or '(blank)'}\n"
        f"Name: {name or '(blank)'}\n"
        f"Email: {email or '(blank)'}\n\n"
        "This patient will not receive an automatic payment link. Please "
        "follow up manually — likely cause: they typed a different number "
        "than the one they're messaging from, or their booking already "
        "expired/was completed."
    )


def screenshot_ack_text() -> str:
    return (
        "We have received your payment screenshot and it is being verified by our team. "
        "You will get a confirmation here once it is approved."
    )


def screenshot_reject_text(booking: dict[str, Any]) -> str:
    return (
        "Sorry, we could not verify your payment from that screenshot. "
        "It may be unclear, cropped, or show a failed transaction.\n\n"
        "Please check your UPI app and resend a clear screenshot of the successful "
        "payment confirmation."
    )



def paid_patient_text(booking: dict[str, Any], calendar_ok: bool = True) -> str:
    email = booking.get("email", "")
    service_info = (
        f"{booking.get('service', '')} on {booking.get('appt_date', '')} "
        f"at {booking.get('appt_time', '')}."
    )
    
    if calendar_ok:
        email_line = f" The calendar invite has been sent to {email}." if email else ""
        return (
            "Payment received and verified! Your screening call is scheduled successfully "
            f"with Dr. Lekha.\n\n"
            f"{service_info}{email_line}\n"
            "😊 Please make sure you approve the invite so you don't miss the notification.\n\n"
            "Please log in on time so we can make the most of our session. 👋"
        )
        
    return (
        "Payment received and verified! Your screening call is scheduled successfully "
        f"with Dr. Lekha.\n\n"
        f"{service_info}\n\n"
        "We hit a technical issue sending the calendar invite — our team has "
        "been notified and will send it to you shortly.\n"
        "😊 Once received, please make sure you approve the invite so you don't miss the notification.\n\n"
        "Please log in on time so we can make the most of our session. 👋"
    )



def paid_doctor_text(
    booking: dict[str, Any], calendar_ok: bool = True, invite_emailed: bool = True
) -> str:
    if not calendar_ok:
        status_line = "WARNING: calendar event FAILED to create — invite NOT sent. Please add manually."
    elif not invite_emailed:
        status_line = (
            "Calendar event created (on your calendar), but the email invite "
            "could NOT be sent to the patient automatically — please forward "
            "the details to them directly."
        )
    else:
        status_line = "Calendar event created and invite sent."
    return (
        "Payment verified — appointment confirmed:\n\n"
        f"Name: {booking.get('full_name', '')}\n"
        f"Service: {booking.get('service', '')}\n"
        f"Date: {booking.get('appt_date', '')}\n"
        f"Time: {booking.get('appt_time', '')}\n"
        f"Patient phone: {booking.get('wa_id', '')}\n\n"
        f"{status_line}"
    )


# ---------------------------------------------------------------------------
# Weight-loss program enrollment messages
# ---------------------------------------------------------------------------
def enrollment_form_text(plan_name: str) -> str:
    """Step 1 — sent right after the patient picks a plan: the enrollment
    form only, attached as a tap-to-open 'Fill Form' button by the caller.
    Payment instructions come AFTER the form is submitted."""
    return (
        f"Thank you for enrolling with us! You have selected *{plan_name}*.\n\n"
        "To help us set up your file and ensure we have all your correct details in our system, "
        "please take a few moments to fill out our quick information form.\n\n"
        "Tap the button below to open the form.\n\n"
        "As soon as you submit the form, I will send you the payment link here "
        "to confirm your enrollment."
    )


def enrollment_payment_link_text(booking: dict[str, Any]) -> str:
    """Body of the enrollment payment message. The Cashfree link is attached
    as a tap-to-open 'Pay Now' button by the caller (raw-URL fallback if
    buttons fail)."""
    return (
        f"Thank you! Your form has been received.\n\n"
        f"To complete your enrollment payment for *{booking.get('plan', '')}:*\n"
        f"Amount: Rs. {_fmt_amount(booking.get('amount'))}\n\n"
        "Tap the button below to open the secure payment page.\n\n"
        "Once the payment is confirmed, our Kayakalp team will shortly reach out to you "
        "to get your program started."
    )


def enrollment_upi_text(booking: dict[str, Any]) -> str:
    """Sent after form submission when Cashfree is NOT configured: UPI details
    + screenshot verification instead of a hosted link."""
    payee_line = f"\nPayee name: {settings.upi_payee_name}" if settings.upi_payee_name else ""
    return (
        "Thank you! Your form has been received.\n\n"
        f"To complete your enrollment payment for *{booking.get('plan', '')}:*\n"
        f"Amount: Rs. {_fmt_amount(booking.get('amount'))}\n"
        f"UPI ID: {settings.upi_id}"
        f"{payee_line}\n\n"
        "After paying, send a screenshot of the payment confirmation here in this chat. "
        "Once it is verified, our Kayakalp team will shortly reach out to you to get "
        "your program started."
    )


def enrollment_paid_patient_text(booking: dict[str, Any]) -> str:
    return (
        "Payment received and verified — your enrollment is confirmed!\n\n"
        f"Program: {booking.get('plan', '')}\n\n"
        "Our Kayakalp team will shortly reach out to you to set up your file, "
        "schedule your consultations and get your program started."
    )


def enrollment_paid_doctor_text(booking: dict[str, Any]) -> str:
    return (
        "Enrollment payment verified — program confirmed:\n\n"
        f"Plan: {booking.get('plan', '')}\n"
        f"Amount: Rs. {_fmt_amount(booking.get('amount'))}\n"
        f"Patient phone: {booking.get('wa_id', '')}\n"
        f"Name/Email: {booking.get('full_name', '(via form)')} / {booking.get('email', '(via form)')}\n\n"
        "Details have been logged to the sheet. Please have the team contact the patient to onboard them."
    )
