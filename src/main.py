"""FastAPI webhook server for Kaya — Kayakalp WhatsApp Assistant.

Mirrors the n8n workflow:
  WhatsApp Trigger -> Route By Type
    |-- text/interactive -> Get Session -> Get Bookings -> Get FAQ -> Booking Brain
    |     |-- bot flow -> Upsert Session -> Send Reply -> (if confirmed) Doctor Approval -> Wait -> Calendar -> Confirm
    |     |-- faq flow -> AI Agent (RAG) -> Send Reply
    |-- image -> Download -> Gemini Vision -> Send Reply
    |-- document -> Download -> Gemini Vision -> Send Reply

Doctor approval flow (mirrors n8n Wait For Approval node):
  1. Send approval request to doctor (917666320828)
  2. Wait up to 10 minutes for manual approval via webhook
  3. If no response, auto-approve
  4. Create calendar event
  5. Send patient confirmation
  6. Send doctor confirmation
  7. Submit to Google Apps Script
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

import httpx

from src.config import settings
from src.rag import local_rag
from src.booking import process_message
from src.whatsapp import whatsapp_client
from src.image_analyzer import image_analyzer
from src import payments
from src import cashfree_client
from src.sheets_client import sheets_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kaya")

app = FastAPI(title="Kaya — Kayakalp WhatsApp Assistant", version="0.3.0")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse({"status": "error", "detail": "Internal server error"}, status_code=500)

# ---------------------------------------------------------------------------
# In-memory stores (for production, use Redis / database)
# ---------------------------------------------------------------------------
def _is_clinic_hotline(wa_id: str) -> bool:
    """True if an incoming message is from the doctor's own WhatsApp number
    (matched by last-10-digits, tolerant of +91/0/spacing differences)."""
    digits = "".join(c for c in (wa_id or "") if c.isdigit())
    hotline_digits = "".join(c for c in (settings.clinic_hotline or "") if c.isdigit())
    if not digits or not hotline_digits:
        return False
    return digits[-10:] == hotline_digits[-10:]


_sessions: dict[str, dict[str, Any]] = {}
_bookings: dict[str, list[dict[str, Any]]] = {}
_pending_approvals: dict[str, dict[str, Any]] = {}
_pending_enrollment_approvals: dict[str, dict[str, Any]] = {}
_auto_approve_tasks: dict[str, asyncio.Task] = {}
_payment_tasks: dict[str, asyncio.Task] = {}
_screenshot_approvals: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Webhook verification (GET) — mirrors n8n WhatsApp Trigger
# ---------------------------------------------------------------------------
@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request) -> Response:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == settings.whatsapp_verify_token:
        log.info("Webhook verified")
        return Response(content=challenge, media_type="text/plain")

    return Response(content="Verification failed", status_code=403)


# ---------------------------------------------------------------------------
# Doctor approval webhook (GET) — mirrors n8n Wait For Approval webhook
# ---------------------------------------------------------------------------
@app.get("/webhook/approve/{booking_id}")
async def approve_booking_get(booking_id: str) -> Response:
    """Doctor taps the approval link from WhatsApp message."""
    return await _process_approval(booking_id)


@app.get("/webhook/approve/{booking_id}/{action}")
async def approve_booking_action(booking_id: str, action: str) -> Response:
    """Doctor replies with approve/reject keyword."""
    if action in ("approve", "confirmed", "yes"):
        return await _process_approval(booking_id)
    elif action in ("reject", "cancel", "no"):
        return await _reject_booking(booking_id)
    return Response(content="Unknown action", status_code=400)


@app.get("/webhook/enrollment/approve/{booking_id}")
async def approve_enrollment_get(booking_id: str) -> Response:
    """Doctor taps the enrollment approval link (fallback when buttons fail)."""
    await _process_enrollment_approval(booking_id)
    return Response(content="Enrollment approved.")


@app.get("/webhook/enrollment/reject/{booking_id}")
async def reject_enrollment_get(booking_id: str) -> Response:
    """Doctor taps the enrollment rejection link (fallback when buttons fail)."""
    await _reject_enrollment(booking_id)
    return Response(content="Enrollment rejected.")


async def _process_approval(booking_id: str) -> Response:
    pending = _pending_approvals.get(booking_id)
    if not pending:
        return Response(content="Booking not found or already processed.", status_code=404)

    # Cancel auto-approve timer if still pending
    task = _auto_approve_tasks.pop(booking_id, None)
    if task and not task.done():
        task.cancel()

    del _pending_approvals[booking_id]
    try:
        await _complete_booking(pending)
    except Exception as e:
        log.error(f"_complete_booking crashed for {booking_id}: {e}", exc_info=True)
        # Still inform the doctor even if the flow failed
        try:
            wa_id = pending["booking"]["wa_id"]
            await whatsapp_client.send_text(
                wa_id,
                "There was a technical issue processing your booking. "
                "Please try again or contact the clinic directly.",
            )
        except Exception:
            pass
    return Response(content="Thank you, the appointment has been approved and scheduled.")


async def _reject_booking(booking_id: str) -> Response:
    pending = _pending_approvals.pop(booking_id, None)
    task = _auto_approve_tasks.pop(booking_id, None)
    if task and not task.done():
        task.cancel()

    if pending:
        wa_id = pending["booking"]["wa_id"]
        try:
            await whatsapp_client.send_text(
                wa_id,
                "Sorry, your appointment could not be confirmed at this time. "
                "Please try booking another slot. Send \"book\" to start again.",
            )
        except Exception as e:
            log.error(f"Failed to send rejection to {wa_id}: {e}")
    return Response(content="Booking rejected.")


# ---------------------------------------------------------------------------
# Enrollment doctor approval (buttons — not link clicks)
# ---------------------------------------------------------------------------
async def _process_enrollment_approval(booking_id: str) -> None:
    """Doctor approved enrollment via button tap."""
    pending = _pending_enrollment_approvals.pop(booking_id, None)
    if not pending:
        return

    task = _auto_approve_tasks.pop(booking_id, None)
    if task and not task.done():
        task.cancel()

    wa_id = pending["booking"]["wa_id"]

    # Register payment flow and start reminder timeline
    flow = payments.start_flow(pending["booking"], kind="enrollment")
    flow["created_at"] = datetime.now(timezone.utc).isoformat()
    task = asyncio.create_task(_payment_timeline_worker(wa_id))
    _payment_tasks[wa_id] = task

    # Send enrollment form to patient
    booking = pending["booking"]
    form_msg = payments.enrollment_form_text(booking.get("plan", ""))
    if settings.enrollment_form_url:
        await _send_cta_or_text(
            wa_id,
            form_msg,
            "📝 Fill Form",
            settings.enrollment_form_url,
            fallback_suffix=f"\n\n📋 Complete Form Here: {settings.enrollment_form_url}",
        )
    else:
        await whatsapp_client.send_text(wa_id, form_msg)

    # Doctor confirmation
    if settings.clinic_hotline:
        await whatsapp_client.send_text(
            settings.clinic_hotline,
            f"Enrollment approved — form sent to patient:\n\n"
            f"Plan: {booking.get('plan', '')}\n"
            f"Amount: Rs. {float(booking.get('amount') or 0):.0f}\n"
            f"Patient phone: {wa_id}\n\n"
            "Awaiting form submission + payment.",
        )

    log.info(f"Enrollment approved for {wa_id} ({booking.get('plan', '')})")


async def _reject_enrollment(booking_id: str) -> None:
    """Doctor rejected enrollment via button tap."""
    pending = _pending_enrollment_approvals.pop(booking_id, None)
    task = _auto_approve_tasks.pop(booking_id, None)
    if task and not task.done():
        task.cancel()

    if pending:
        wa_id = pending["booking"]["wa_id"]
        # Clean up the booking session so the patient can start fresh
        _sessions.pop(wa_id, None)
        try:
            await whatsapp_client.send_text(
                wa_id,
                "Sorry, your enrollment could not be confirmed at this time. "
                'Please try again by sending "enroll" or reach out to our team for help.',
            )
        except Exception as e:
            log.error(f"Failed to send enrollment rejection to {wa_id}: {e}")
    log.info(f"Enrollment rejected for booking_id {booking_id}")


# ---------------------------------------------------------------------------
# Google Form submission (Apps Script onFormSubmit -> this endpoint)
# ---------------------------------------------------------------------------
@app.post("/webhook/form")
async def form_submitted(request: Request) -> Response:
    """Apps Script trigger posts {phone, name?, email?} when the patient
    submits the screening form. We then send UPI payment instructions."""
    try:
        data = await request.json()
    except Exception:
        log.warning("Form webhook received malformed/non-JSON body")
        return JSONResponse({"status": "ignored", "error": "malformed body"}, status_code=400)

    phone_raw = str(data.get("phone", "") or "").strip()
    name_raw = str(data.get("name", "") or "").strip()
    email_raw = str(data.get("email", "") or "").strip()

    if not phone_raw and not email_raw:
        log.warning("Form webhook received with no phone and no email")
        return JSONResponse({"status": "ignored", "error": "missing phone and email"}, status_code=400)

    # Email is unique and already known from the WhatsApp booking flow (await_email
    # step), so it's a more reliable match than phone digits; fall back to phone.
    wa_id = payments.find_wa_id_by_email(email_raw) or payments.find_wa_id_by_phone(phone_raw)
    if not wa_id:
        log.warning(f"Form submitted for unknown patient phone: {phone_raw}")
        if settings.clinic_hotline:
            try:
                await whatsapp_client.send_text(
                    settings.clinic_hotline,
                    payments.unmatched_form_doctor_text(phone_raw, name_raw, email_raw),
                )
            except Exception as e:
                log.error(f"Failed to alert doctor of unmatched form submission: {e}")
        return JSONResponse({"status": "ignored", "error": "no matching patient"})

    flow = payments.get_flow(wa_id)
    if not flow:
        return JSONResponse({"status": "ok"})

    # Patient already paid — ignore stale form submission
    if payments.is_already_paid(wa_id):
        log.info(f"Form submission ignored - {wa_id} already paid")
        return JSONResponse({"status": "ok"})

    # Enrollment flows: the form is step 1 — payment details are only sent
    # AFTER this submission arrives. A submission here also enriches the
    # record (name/email) for the doctor's verification message.
    if flow.get("kind") == "enrollment":
        if flow["booking"].get("email") != email_raw and email_raw:
            flow["booking"]["email"] = email_raw
        if not flow["booking"].get("full_name") and name_raw:
            flow["booking"]["full_name"] = name_raw
        if flow["status"] == payments.AWAITING_FORM:
            payments.set_status(wa_id, payments.AWAITING_PAYMENT)
            await _begin_enrollment_payment(wa_id)
        return JSONResponse({"status": "ok"})

    if flow["status"] != payments.AWAITING_FORM:
        # Duplicate submission or already past this step
        return JSONResponse({"status": "ok"})

    # Use the freshest email from the form for the calendar invite
    form_email = str(data.get("email", "") or "").strip()
    if form_email:
        flow["booking"]["email"] = form_email
    flow["form_submitted"] = True
    payments.set_status(wa_id, payments.AWAITING_PAYMENT)

    await _begin_appointment_payment(wa_id, flow["booking"])
    log.info(f"Form received from {wa_id} - payment message sent")
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Cashfree payment webhook (auto-verified payment; no doctor screenshot review)
# ---------------------------------------------------------------------------
@app.post("/webhook/cashfree")
async def cashfree_webhook(request: Request) -> Response:
    raw = await request.body()
    signature = request.headers.get("x-webhook-signature", "")
    timestamp = request.headers.get("x-webhook-timestamp", "")

    if not cashfree_client.verify_webhook_signature(raw, signature, timestamp):
        log.warning("Rejected Cashfree webhook with invalid signature")
        return Response(content="invalid signature", status_code=401)

    try:
        payload = json.loads(raw)
    except Exception:
        return JSONResponse({"status": "ignored"}, status_code=400)

    link_id, link_status = cashfree_client.extract_link_status(payload)
    if link_status != "PAID" or not link_id:
        return JSONResponse({"status": "ignored"})

    wa_id = payments.find_wa_id_by_link_id(link_id)
    if not wa_id:
        log.warning(f"Cashfree webhook for unknown link_id: {link_id}")
        return JSONResponse({"status": "ignored"})

    flow = payments.get_flow(wa_id)
    if not flow or flow["status"] == payments.PAID:
        return JSONResponse({"status": "ok"})  # already handled / duplicate webhook

    booking = flow["booking"]
    flow_kind = flow.get("kind", "appointment")
    payments.set_status(wa_id, payments.PAID)

    task = _payment_tasks.pop(wa_id, None)
    if task and not task.done():
        task.cancel()
    payments.pop_flow(wa_id)

    await _finalize_paid_booking(wa_id, booking, flow_kind)
    log.info(
        f"Cashfree payment auto-verified for {wa_id} (link {link_id}, kind={flow_kind})"
    )
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Payment verification endpoints (doctor taps links from WhatsApp)
# ---------------------------------------------------------------------------
@app.get("/webhook/payment/approve/{payment_id}")
async def approve_payment(payment_id: str) -> Response:
    return await _approve_payment(payment_id)


@app.get("/webhook/payment/reject/{payment_id}")
async def reject_payment(payment_id: str) -> Response:
    return await _reject_payment(payment_id)


@app.get("/media/{payment_id}")
async def serve_screenshot(payment_id: str) -> Response:
    """Let the doctor view the submitted payment screenshot."""
    proof = _screenshot_approvals.get(payment_id)
    if not proof:
        return Response(content="Not found", status_code=404)
    return Response(content=proof["media_bytes"], media_type=proof["mime_type"])


@app.get("/media/pdf/{filename}")
async def serve_pdf(filename: str) -> Response:
    """Serve local PDF documents (program details, welcome letter, etc.)
    so WhatsApp Cloud API can fetch and deliver them to patients."""
    import re as _re

    if not _re.match(r"^[\w\s\-().]+\.pdf$", filename, _re.IGNORECASE):
        return Response(content="Invalid filename", status_code=400)

    docs_dir = Path(__file__).resolve().parent / "Docs"
    pdf_path = docs_dir / filename
    if not pdf_path.exists() or not pdf_path.is_file():
        return Response(content="PDF not found", status_code=404)

    return Response(
        content=pdf_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Auto-approve timer (mirrors n8n Wait For Approval with 10 min timeout)
# ---------------------------------------------------------------------------
async def _auto_approve_after_delay(booking_id: str, pending: dict, delay_seconds: int):
    """Wait for delay, then auto-approve if no manual action taken."""
    try:
        await asyncio.sleep(delay_seconds)
        # Still pending? Auto-approve
        if booking_id in _pending_approvals:
            log.info(f"Auto-approving booking {booking_id} after {delay_seconds}s timeout")
            del _pending_approvals[booking_id]
            await _complete_booking(pending)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Post-approval payment pipeline
# ---------------------------------------------------------------------------
async def _complete_booking(pending: dict):
    """After doctor approval: form link + reminders. NO calendar event yet —
    the slot is only booked once the patient's payment is verified.

    Returning-patient shortcut: BEFORE the screening form goes out, the
    payments sheet is checked by email (then phone). Found + Paid -> no new
    booking at all; the patient is told their screening call already exists.
    Found but unpaid -> the Google Form is skipped and the payment request
    goes out directly. Any lookup error falls back to the standard flow."""
    booking = pending["booking"]
    wa_id = booking["wa_id"]

    # 0. Fast-path: already paid (in-memory cache) — no sheet lookup needed
    if payments.is_already_paid(wa_id):
        await whatsapp_client.send_text(wa_id, payments.already_booked_patient_text())
        if settings.clinic_hotline:
            await whatsapp_client.send_text(
                settings.clinic_hotline, payments.already_booked_doctor_text(booking)
            )
        log.info(f"Booking blocked (paid-cache hit) - {wa_id} already has a Paid screening call on file")
        return

    # 0b. Payments-sheet check (fail-open on errors)
    try:
        match = await sheets_client.find_patient_row(booking.get("email", ""), wa_id)
    except Exception as e:
        log.error(f"Payments-sheet lookup FAILED for {booking.get('full_name', '')} ({wa_id}): {e}")
        match = None

    if match is not None:
        row, paid = match
        # Enrich the record from the sheet when those columns exist.
        for row_key, book_key in (("name", "full_name"), ("email", "email")):
            val = next((v for k, v in row.items() if row_key in k and v), "")
            if val and not booking.get(book_key):
                booking[book_key] = val
        if paid:
            # Screening call already completed/paid — don't double-book or charge.
            await whatsapp_client.send_text(wa_id, payments.already_booked_patient_text())
            if settings.clinic_hotline:
                await whatsapp_client.send_text(
                    settings.clinic_hotline, payments.already_booked_doctor_text(booking)
                )
            log.info(f"Booking blocked - {wa_id} already has a Paid screening call on file")
            return
        returning_unpaid = True
    else:
        log.warning(
            f"No match in payments sheet for {booking.get('full_name', '')} "
            f"(email={booking.get('email', '')}, wa_id={wa_id}) — treating as new patient"
        )
        returning_unpaid = False

    # 0c. If there's already an active flow for this patient, don't start another
    existing_flow = payments.get_flow(wa_id)
    if existing_flow:
        log.info(f"Booking skipped - active flow already exists for {wa_id} (status={existing_flow['status']})")
        return

    # 1. Register payment flow and start reminder timeline
    flow = payments.start_flow(booking)
    flow["created_at"] = datetime.now(timezone.utc).isoformat()
    if returning_unpaid:
        # No form step — the payment window opens right away
        payments.set_status(wa_id, payments.AWAITING_PAYMENT)
    task = asyncio.create_task(_payment_timeline_worker(wa_id))
    _payment_tasks[wa_id] = task

    # 2. Patient message: screening form ('Fill Form' button) for new
    # patients; direct payment request for returning ones
    if returning_unpaid:
        await _begin_appointment_payment(
            wa_id, booking, body_text=payments.returning_patient_pay_text(booking)
        )
    else:
        form_msg = payments.patient_form_text(booking)
        if settings.screening_form_url:
            await _send_cta_or_text(
                wa_id,
                form_msg,
                "📝 Fill Form",
                settings.screening_form_url,
                fallback_suffix=f"\n\n📋 Complete Form Here: {settings.screening_form_url}",
            )
        else:
            await whatsapp_client.send_text(wa_id, form_msg)

    # 3. Doctor notice (slot not final until payment verified)
    if settings.clinic_hotline:
        doctor_notice = (
            payments.doctor_approved_returning_text(booking)
            if returning_unpaid
            else payments.doctor_approved_text(booking)
        )
        await whatsapp_client.send_text(settings.clinic_hotline, doctor_notice)

    # 4. Log to the doctor's Google Sheet immediately on approval — this is a
    # visibility log of the pipeline, separate from the calendar event, which
    # still only gets created once payment is verified.
    if settings.google_apps_script_url:
        try:
            async with httpx.AsyncClient(timeout=15.0) as _http:
                await _http.post(
                    settings.google_apps_script_url,
                    json={
                        "timestamp": datetime.now(timezone.utc).strftime("%d/%m/%Y, %I:%M:%S %p"),
                        "name": booking.get("full_name", ""),
                        "email": booking.get("email", ""),
                        "phone": wa_id,
                        "date": booking.get("appt_date", ""),
                        "time": booking.get("appt_time", ""),
                        "service": booking.get("service", ""),
                        "message": "Approved via WhatsApp bot (awaiting payment)",
                    },
                )
            log.info(f"Booking logged to sheet on approval for {booking.get('full_name', '')}")
        except Exception as e:
            log.error(f"Failed to log approved booking to sheet: {e}")

    log.info(f"Payment flow started for {booking.get('full_name', '')} ({wa_id})")


async def _start_enrollment_flow(wa_id: str, booking: dict[str, Any]) -> None:
    """Weight-loss program enrollment (3rd menu option).

    Step 0 — sheet verification: if this WhatsApp number already appears in
    the clinic's enrollment Google Sheet with payment status "Paid", the
    patient is enrolled instantly (confirmation to patient + doctor, logged)
    and the form/payment steps are skipped entirely.

    If the patient is found but "Unpaid", the form step is skipped and the
    payment request goes out directly.

    Otherwise the form is sent directly to the patient (no doctor approval
    needed), and payment is collected after form submission."""
    # Fast-path: already paid (in-memory cache)
    if payments.is_already_paid(wa_id):
        log.info(f"Enrollment blocked (paid-cache hit) for {wa_id}")
        return

    # If there's already an active flow, don't start another
    if payments.get_flow(wa_id):
        log.info(f"Enrollment skipped - active flow already exists for {wa_id}")
        return

    # 0. Enrollment sheet check — paid patient is enrolled instantly;
    #    unpaid returning patient skips the form and goes to payment.
    #    The program-tier sheet is checked with BOTH the phone number and the
    #    selected program: only a Paid row for the SAME phone AND SAME program
    #    blocks the form. Same phone on a different program -> form continues.
    matched_row: dict[str, str] | None = None
    program_tier = ""
    if settings.enrollment_program_sheet_id:
        try:
            result = await sheets_client.find_enrollment_program_row(
                wa_id, booking.get("plan", "")
            )
            if result:
                matched_row, program_tier = result
        except Exception as e:
            log.error(f"Enrollment program-tier sheet check failed for {wa_id}: {e}")

    if matched_row:
        plan_label = (program_tier or booking.get("plan", "this program"))
        plan_label = re.sub(r"\s*\(.*\)\s*$", "", plan_label).strip()
        log.info(
            f"Enrollment pre-verified from program sheet (Paid, same program) "
            f"for {wa_id} ({plan_label}) - already registered"
        )
        await whatsapp_client.send_text(
            wa_id,
            f"It seems you are already registered in *{plan_label}*.\n\n"
            "If you have any doubts, please reach out to our team and we will be happy to help.",
        )
        return

    # 1. Send form directly to patient (no doctor approval needed)
    flow = payments.start_flow(booking, kind="enrollment")
    flow["created_at"] = datetime.now(timezone.utc).isoformat()

    # Start reminder / expiry timeline
    task = asyncio.create_task(_payment_timeline_worker(wa_id))
    _payment_tasks[wa_id] = task

    # Send enrollment form directly
    plan_name = booking.get("plan", "")
    plan_amount = float(booking.get("amount") or 0)
    form_msg = payments.enrollment_form_text(plan_name)
    if settings.enrollment_form_url:
        await _send_cta_or_text(
            wa_id,
            form_msg,
            "📝 Fill Form",
            settings.enrollment_form_url,
            fallback_suffix=f"\n\n📋 Complete Form Here: {settings.enrollment_form_url}",
        )
    else:
        await whatsapp_client.send_text(wa_id, form_msg)

    # Notify doctor
    if settings.clinic_hotline:
        await whatsapp_client.send_text(
            settings.clinic_hotline,
            f"New enrollment started — form sent to patient:\n\n"
            f"Plan: {plan_name}\n"
            f"Amount: Rs. {plan_amount:.0f}\n"
            f"Patient phone: {wa_id}\n"
            f"Name: {booking.get('full_name', '(via form)')}\n\n"
            "Awaiting form submission + payment.",
        )

    log.info(
        f"Enrollment started for {wa_id} ({plan_name}, "
        f"Rs. {plan_amount:.0f}) - form sent, awaiting submission"
    )


async def _auto_approve_after_enrollment_delay(
    booking_id: str, pending: dict, delay_seconds: int
):
    """Wait for delay, then auto-approve enrollment if no manual action."""
    try:
        await asyncio.sleep(delay_seconds)
        if booking_id in _pending_enrollment_approvals:
            log.info(f"Auto-approving enrollment {booking_id} after {delay_seconds}s timeout")
            del _pending_enrollment_approvals[booking_id]
            await _process_enrollment_approval(booking_id)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Direct payment flow for Skin / General (no doctor approval needed)
# ---------------------------------------------------------------------------
async def _start_direct_payment_flow(wa_id: str, booking: dict[str, Any]) -> None:
    """Skin / General appointments skip doctor approval and go straight to
    payment.  A Cashfree link (or UPI instructions) is sent immediately; on
    payment success the booking is stored in the sheet and a calendar event
    is created."""
    # Register payment flow
    flow = payments.start_flow(booking, kind="appointment")
    flow["created_at"] = datetime.now(timezone.utc).isoformat()

    amount = float(booking.get("payment_amount") or settings.skin_general_payment_amount or 400)

    # Create Cashfree payment link when configured
    if settings.cashfree_enabled and amount > 0 and not flow.get("payment_link_url"):
        link_id = f"kk-sg-{wa_id}-{uuid.uuid4().hex[:8]}"
        link = await cashfree_client.create_payment_link(
            link_id=link_id,
            amount=amount,
            customer_phone=wa_id,
            customer_name=booking.get("full_name", ""),
            customer_email=booking.get("email", ""),
        )
        if link:
            payments.set_payment_link(wa_id, link["link_id"], link["link_url"])
        else:
            log.error(f"Cashfree link creation failed for {wa_id} (skin/general) - falling back to UPI text")

    # Start reminder / expiry timeline
    task = asyncio.create_task(_payment_timeline_worker(wa_id))
    _payment_tasks[wa_id] = task

    flow = payments.get_flow(wa_id)
    link_url = flow.get("payment_link_url", "") if flow else ""

    # Send payment message to patient
    pay_text = (
        f"Your {booking.get('service', '')} appointment is confirmed!\n\n"
        f"📅 {booking.get('appt_date', '')} at {booking.get('appt_time', '')}\n"
        f"💰 Amount: Rs. {int(amount)}\n\n"
        "Tap the button below to complete your payment. "
        "Your appointment will be finalized once payment is received."
    )

    if link_url:
        try:
            await _send_cta_or_text(wa_id, pay_text, "Pay Now", link_url)
        except Exception as e:
            log.error(f"Skin/general payment link delivery FAILED for {wa_id}: {e}")
            await _notify_doctor_payment_stuck(wa_id, booking)
        try:
            await sheets_client.update_payment_status(
                wa_id, booking.get("email", ""), "Pending"
            )
        except Exception:
            pass
    elif settings.upi_id:
        amount_line = f"\nAmount: Rs. {int(amount)}"
        payee_line = f"\nPayee name: {settings.upi_payee_name}" if settings.upi_payee_name else ""
        await whatsapp_client.send_text(
            wa_id,
            pay_text
            + f"\n\nUPI ID: {settings.upi_id}{payee_line}{amount_line}\n\n"
            "After paying, send a screenshot of the payment confirmation here.",
        )
    else:
        log.error("Neither Cashfree nor UPI_ID configured - cannot send skin/general payment")
        await whatsapp_client.send_text(
            wa_id,
            "Your appointment is confirmed! Our team will send you the payment details shortly.",
        )

    # Notify doctor
    if settings.clinic_hotline:
        await whatsapp_client.send_text(
            settings.clinic_hotline,
            f"Skin/General appointment — payment link sent (Rs. {int(amount)}):\n\n"
            f"Name: {booking.get('full_name', '')}\n"
            f"Service: {booking.get('service', '')}\n"
            f"Date: {booking.get('appt_date', '')}\n"
            f"Time: {booking.get('appt_time', '')}\n"
            f"Patient phone: {wa_id}\n"
            f"Email: {booking.get('email', '')}\n\n"
            "Awaiting payment confirmation.",
        )

    log.info(
        f"Direct payment flow started for {wa_id} ({booking.get('service', '')}, "
        f"Rs. {int(amount)}) - payment link sent"
    )


async def _notify_doctor_payment_stuck(wa_id: str, booking: dict[str, Any]) -> None:
    """Payment link could not be delivered to the patient."""
    if not settings.clinic_hotline:
        return
    try:
        await whatsapp_client.send_text(
            settings.clinic_hotline,
            "PAYMENT FOLLOW-UP NEEDED:\n\n"
            f"Name: {booking.get('full_name', '')}\n"
            f"Service: {booking.get('service', '')}\n"
            f"Date: {booking.get('appt_date', '')} at {booking.get('appt_time', '')}\n"
            f"Patient phone: {wa_id}\n\n"
            "The payment link could NOT be delivered on WhatsApp. "
            "Please contact them manually with the payment details.",
        )
    except Exception as e:
        log.error(f"Failed to alert doctor about stuck payment: {e}")


def _verify_buttons_payload(to: str, body: str, payment_id: str) -> dict[str, Any]:
    """Approve / Reject quick-reply buttons for the doctor's payment
    verification message."""
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"payok_{payment_id}", "title": "✅ Approve"}},
                    {"type": "reply", "reply": {"id": f"payno_{payment_id}", "title": "❌ Reject"}},
                ]
            },
        },
    }


def _enrollment_approval_buttons_payload(to: str, body: str, booking_id: str) -> dict[str, Any]:
    """Approve / Reject buttons for the doctor's enrollment approval message."""
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"enrollok_{booking_id}", "title": "✅ Approve"}},
                    {"type": "reply", "reply": {"id": f"enrollno_{booking_id}", "title": "❌ Reject"}},
                ]
            },
        },
    }


def _enrollment_approval_buttons_url_payload(to: str, body: str, booking_id: str) -> dict[str, Any]:
    """Fallback URL-based buttons for enrollment approval (when interactive
    buttons fail)."""
    base_url = settings.app_base_url or "http://localhost:8000"
    approve_url = f"{base_url}/webhook/enrollment/approve/{booking_id}"
    reject_url = f"{base_url}/webhook/enrollment/reject/{booking_id}"
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {
            "body": (
                f"{body}\n\n"
                f"✅ Approve:\n{approve_url}\n\n"
                f"❌ Reject:\n{reject_url}"
            ),
            "preview_url": True,
        },
    }


def _appointment_approval_buttons_payload(to: str, body: str, booking_id: str) -> dict[str, Any]:
    """Approve / Reject buttons for the doctor's appointment approval message."""
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": f"apptok_{booking_id}", "title": "✅ Approve"}},
                    {"type": "reply", "reply": {"id": f"apptno_{booking_id}", "title": "❌ Reject"}},
                ]
            },
        },
    }


def _cta_url_payload(to: str, body: str, display_text: str, url: str) -> dict[str, Any]:
    """Interactive message whose single tappable button (e.g. 'Pay Now',
    'Fill Form') opens the given URL — so patients never see a raw link."""
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": body},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": display_text[:25],
                    "url": url,
                },
            },
        },
    }


async def _send_cta_or_text(
    to: str,
    body: str,
    display_text: str,
    url: str,
    *,
    fallback_suffix: str = "",
) -> None:
    """Send a tap-to-open link button; if that cannot be delivered (e.g.
    outside the 24h window), fall back to plain text with the raw URL
    appended. Raises only if BOTH attempts fail."""
    try:
        await whatsapp_client.send_message(_cta_url_payload(to, body, display_text, url))
        return
    except Exception as e:
        log.error(f"Link button '{display_text}' failed for {to}, sending text fallback: {e}")
    try:
        await whatsapp_client.send_text(to, f"{body}{fallback_suffix}\n\n{url}".strip())
    except Exception as e:
        log.error(f"Text fallback ALSO failed for {to}: {e}")
        raise


async def _begin_appointment_payment(
    wa_id: str, booking: dict[str, Any], *, body_text: str | None = None
) -> None:
    """Send the screening-call payment message: a 'Pay Now' button bound to a
    fresh Cashfree link when configured, otherwise raw UPI instructions.
    `body_text` overrides the default message body (returning patients)."""
    flow = payments.get_flow(wa_id)

    if settings.cashfree_enabled and flow and not flow.get("payment_link_url"):
        # Use booking's payment_amount (skin/general=400) or global setting
        amount = float(
            booking.get("payment_amount")
            or settings.skin_general_payment_amount
            or settings.payment_amount
            or 0
        ) or 1.0
        link_id = f"kk-{wa_id}-{uuid.uuid4().hex[:8]}"
        link = await cashfree_client.create_payment_link(
            link_id=link_id,
            amount=amount,
            customer_phone=wa_id,
            customer_name=booking.get("full_name", ""),
            customer_email=booking.get("email", ""),
        )
        if link:
            payments.set_payment_link(wa_id, link["link_id"], link["link_url"])
        else:
            log.error(f"Cashfree link creation failed for {wa_id} - falling back to UPI text")

    flow = payments.get_flow(wa_id)
    link_url = flow.get("payment_link_url", "") if flow else ""

    if link_url:
        await _send_cta_or_text(
            wa_id,
            body_text or payments.payment_link_text(booking),
            "Pay Now",
            link_url,
        )
        try:
            await sheets_client.update_payment_status(
                wa_id, booking.get("email", ""), "Pending"
            )
        except Exception:
            pass
        return

    if not settings.upi_id:
        log.error("Neither Cashfree nor UPI_ID is configured - cannot send payment instructions")
        await whatsapp_client.send_text(
            wa_id,
            "Thank you! Your form has been received. Our team will send you the "
            "payment details shortly.",
        )
        return
    await whatsapp_client.send_text(
        wa_id,
        payments.returning_patient_upi_text(booking)
        if body_text  # returning patient — skip the "form received" wording
        else payments.payment_instructions_text(booking),
    )


async def _begin_enrollment_payment(wa_id: str) -> None:
    """Step 2 — the form was submitted: create the Cashfree payment link for
    the selected plan's amount (once) and send it directly to the patient.
    Falls back to UPI instructions when Cashfree is not configured."""
    flow = payments.get_flow(wa_id)
    if not flow or flow.get("kind") != "enrollment":
        return
    b = flow["booking"]
    amount = float(b.get("amount") or 0)

    if settings.cashfree_enabled and amount > 0 and not flow.get("payment_link_url"):
        link_id = f"kk-enroll-{wa_id}-{uuid.uuid4().hex[:8]}"
        link = await cashfree_client.create_payment_link(
            link_id=link_id,
            amount=amount,
            customer_phone=wa_id,
            customer_name=b.get("full_name", ""),
            customer_email=b.get("email", ""),
        )
        if link:
            payments.set_payment_link(wa_id, link["link_id"], link["link_url"])
        else:
            log.error(f"Cashfree link creation failed for enrollment {wa_id} - falling back to UPI text")

    # Payment window opens now — start the reminder/expiry timeline.
    if wa_id not in _payment_tasks or _payment_tasks[wa_id].done():
        task = asyncio.create_task(_payment_timeline_worker(wa_id))
        _payment_tasks[wa_id] = task

    flow = payments.get_flow(wa_id)
    link_url = flow.get("payment_link_url", "") if flow else ""

    if link_url:
        try:
            await _send_cta_or_text(
                wa_id,
                payments.enrollment_payment_link_text(b),
                "Pay Now",
                link_url,
            )
            log.info(f"Enrollment Pay Now button sent directly to {wa_id}")
        except Exception as e:
            # E.g. patient submitted the form outside the 24h WhatsApp reply
            # window — Meta rejects ANY business-initiated message then.
            log.error(f"Enrollment payment delivery FAILED for {wa_id}: {e}")
            await _notify_doctor_enrollment_stuck(wa_id, b)
        try:
            await sheets_client.update_payment_status(wa_id, b.get("email", ""), "Pending")
        except Exception:
            pass
        return

    try:
        await whatsapp_client.send_text(wa_id, payments.enrollment_upi_text(b))
        log.info(f"Enrollment UPI instructions sent to {wa_id}")
    except Exception as e:
        log.error(f"Enrollment UPI delivery FAILED for {wa_id}: {e}")
        await _notify_doctor_enrollment_stuck(wa_id, b)


async def _notify_doctor_enrollment_stuck(wa_id: str, b: dict[str, Any]) -> None:
    """Payment details could not be delivered to the patient (most common
    cause: form submitted more than 24h after their last WhatsApp message).
    Tell the doctor to follow up manually so nobody falls through."""
    if not settings.clinic_hotline:
        return
    try:
        await whatsapp_client.send_text(
            settings.clinic_hotline,
            "ENROLLMENT FOLLOW-UP NEEDED:\n\n"
            f"Name: {b.get('full_name', '(on form)')}\n"
            f"Plan: {b.get('plan', '')}\n"
            f"Amount: Rs. {b.get('amount', '')}\n"
            f"Patient phone: {wa_id}\n\n"
            "Their form WAS submitted but the payment message could NOT be "
            "delivered on WhatsApp (likely outside the 24h reply window). "
            "Please contact them manually with the payment details.",
        )
    except Exception as e:
        log.error(f"Failed to alert doctor about stuck enrollment: {e}")


async def _payment_timeline_worker(wa_id: str):
    """Remind the patient at 24h / 3d / 7d; alert the doctor after expiry."""
    try:
        schedule = payments.reminder_schedule_seconds()
        prev = 0.0
        for i, at in enumerate(schedule):
            await asyncio.sleep(at - prev)
            prev = at

            flow = payments.get_flow(wa_id)
            if not flow or flow["status"] in (payments.PAID, payments.EXPIRED):
                return
            try:
                link_url = flow.get("payment_link_url", "")
                if link_url:
                    await _send_cta_or_text(
                        wa_id,
                        payments.reminder_text(flow["booking"], i, link_url),
                        "Pay Now",
                        link_url,
                    )
                else:
                    await whatsapp_client.send_text(
                        wa_id, payments.reminder_text(flow["booking"], i)
                    )
                log.info(f"Payment reminder {i + 1} sent to {wa_id}")
            except Exception as e:
                log.error(f"Payment reminder failed for {wa_id}: {e}")

        # Timeline passed with no verified payment -> notify doctor
        flow = payments.get_flow(wa_id)
        if not flow or flow["status"] == payments.PAID:
            return
        proof_pending = flow["status"] == payments.VERIFYING
        payments.set_status(wa_id, payments.EXPIRED)
        if not proof_pending:
            payments.pop_flow(wa_id)
        if settings.clinic_hotline:
            await whatsapp_client.send_text(
                settings.clinic_hotline,
                payments.expired_doctor_text(flow["booking"], proof_pending),
            )
        log.info(f"Payment timeline expired for {wa_id} (proof_pending={proof_pending})")
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Payment verification (screenshot -> doctor approve/reject)
# ---------------------------------------------------------------------------
async def _approve_payment(payment_id: str) -> Response:
    """Doctor confirmed the payment screenshot -> book the slot for real."""
    proof = _screenshot_approvals.pop(payment_id, None)
    if not proof:
        return Response(content="Payment not found or already processed.", status_code=404)

    wa_id = proof["wa_id"]
    booking = proof["booking"]

    # Cancel the reminder timeline
    task = _payment_tasks.pop(wa_id, None)
    if task and not task.done():
        task.cancel()
    payments.pop_flow(wa_id)

    await _finalize_paid_booking(wa_id, booking, proof.get("kind", "appointment"))
    return Response(content="Thank you! Payment verified. The appointment has been added to the calendar.")


async def _finalize_paid_booking(
    wa_id: str, booking: dict[str, Any], kind: str = "appointment"
) -> None:
    """Shared by all payment paths: appointment screenshot approval,
    enrollment screenshot approval and the Cashfree webhook. For
    kind="appointment" this creates the calendar event; for
    kind="enrollment" it just confirms + hands off to the Kayakalp team."""
    # Cache the paid status so future booking attempts for this wa_id are
    # blocked instantly, even if the Google Sheet is temporarily unreachable.
    payments.mark_paid(wa_id, kind)
    is_enrollment = kind == "enrollment"
    full_name = booking.get("full_name", "")
    service = booking.get("service", "")
    email = booking.get("email", "")

    # 1. Create Google Calendar event (appointments only — enrollments are
    # onboarded manually by the team, so there is nothing to book)
    calendar_event_created = False
    invite_emailed = False
    if (
        not is_enrollment
        and settings.google_calendar_id
        and booking.get("start_iso")
        and booking.get("end_iso")
    ):
        try:
            from src.calendar_client import calendar_client
            summary = f"Intro Call - {full_name} ({service})"
            description = (
                f"Kayakalp introduction call booked via WhatsApp.\n"
                f"Patient: {full_name}\n"
                f"Service: {service}\n"
                f"Phone: {wa_id}\n"
                f"Email: {email}"
            )
            event = await calendar_client.create_event(
                summary=summary,
                start_iso=booking["start_iso"],
                end_iso=booking["end_iso"],
                description=description,
                attendee_email=email,
            )
            if event.get("id"):
                calendar_event_created = True
                invite_emailed = bool(event.get("attendees"))
                log.info(
                    f"Calendar event created for booking {wa_id} "
                    f"[invite_emailed={invite_emailed}]"
                )
            else:
                log.error(f"Calendar event creation returned no id for {wa_id}: {event}")
        except Exception as e:
            log.error(f"Failed to create calendar event for {wa_id}: {e}")

    # 2. Log booking as paid
    booking_record = dict(booking)
    booking_record["status"] = "paid_confirmed"
    booking_record["created_date"] = datetime.now(timezone.utc).isoformat()
    _bookings.setdefault(wa_id, []).append(booking_record)

    # 3. Submit to Google Apps Script (website form)
    if settings.google_apps_script_url:
        sheet_message = (
            "Weight-loss program enrollment paid via WhatsApp bot"
            if is_enrollment
            else "Booked via WhatsApp bot (payment verified)"
        )
        try:
            async with httpx.AsyncClient(timeout=15.0) as _http:
                await _http.post(
                    settings.google_apps_script_url,
                    json={
                        "timestamp": datetime.now(timezone.utc).strftime("%d/%m/%Y, %I:%M:%S %p"),
                        "name": full_name,
                        "email": email,
                        "phone": wa_id,
                        "date": booking.get("appt_date", ""),
                        "time": booking.get("appt_time", ""),
                        "service": service,
                        "message": sheet_message,
                    },
                )
            log.info(f"Booking submitted to website for {full_name}")
        except Exception as e:
            log.error(f"Failed to submit booking to website: {e}")

    # 3b. Update Payment status to Paid in the enrollment/sheet
    try:
        await sheets_client.update_payment_status(wa_id, email, "Paid")
    except Exception:
        pass

    # 3c. For enrollments, also mark the program-tier sheet as Paid so the
    #     same-phone + same-program check recognises this patient as already
    #     registered the next time they pick this program.
    if is_enrollment and settings.enrollment_program_sheet_id:
        try:
            await sheets_client.update_payment_status(
                wa_id,
                email,
                "Paid",
                sheet_id=settings.enrollment_program_sheet_id,
                gid=settings.enrollment_program_sheet_gid,
                plan_name=booking.get("plan", ""),
            )
        except Exception:
            pass

    # 4. Patient confirmation (calendar invite on its way — or an honest note if it failed)
    if is_enrollment:
        await whatsapp_client.send_text(wa_id, payments.enrollment_paid_patient_text(booking))
    else:
        await whatsapp_client.send_text(
            wa_id, payments.paid_patient_text(booking, invite_emailed)
        )

    # 5. Doctor confirmation (includes an explicit warning if the calendar failed)
    if settings.clinic_hotline:
        doctor_confirmation = (
            payments.enrollment_paid_doctor_text(booking)
            if is_enrollment
            else payments.paid_doctor_text(booking, calendar_event_created, invite_emailed)
        )
        await whatsapp_client.send_text(settings.clinic_hotline, doctor_confirmation)

    # 6. Send PDF link to patient (opens in new tab on tap)
    if is_enrollment:
        # Program details PDF after enrollment payment
        try:
            await _send_cta_or_text(
                wa_id,
                "Here are the program details for your review:",
                "📄 View Program Details",
                settings.program_detail_pdf_url,
                fallback_suffix=f"\n\n📋 Program Details: {settings.program_detail_pdf_url}",
            )
            log.info(f"Program detail PDF link sent to {wa_id}")
        except Exception as e:
            log.error(f"Failed to send program detail PDF link to {wa_id}: {e}")
    else:
        # Welcome to KayaKalp PDF after screening call payment
        try:
            await _send_cta_or_text(
                wa_id,
                "Welcome to KayaKalp! Here's your welcome guide:",
                "📄 View Welcome Guide",
                settings.welcome_pdf_url,
                fallback_suffix=f"\n\n📋 Welcome Guide: {settings.welcome_pdf_url}",
            )
            log.info(f"Welcome PDF link sent to {wa_id}")
        except Exception as e:
            log.error(f"Failed to send welcome PDF link to {wa_id}: {e}")

    if is_enrollment:
        log.info(
            f"Enrollment paid & confirmed for {booking.get('plan', '')} ({wa_id})"
        )
    else:
        log.info(
            f"Payment verified & slot booked for {full_name} ({wa_id}) "
            f"[calendar_ok={calendar_event_created}, invite_emailed={invite_emailed}]"
        )


async def _reject_payment(payment_id: str) -> Response:
    """Screenshot unreadable/invalid -> let the patient resend."""
    proof = _screenshot_approvals.pop(payment_id, None)
    if not proof:
        return Response(content="Payment not found or already processed.", status_code=404)

    wa_id = proof["wa_id"]
    payments.set_status(wa_id, payments.AWAITING_PAYMENT)
    try:
        await whatsapp_client.send_text(wa_id, payments.screenshot_reject_text(proof["booking"]))
    except Exception as e:
        log.error(f"Failed to notify patient of rejected screenshot: {e}")
    log.info(f"Payment screenshot rejected for {wa_id}")
    return Response(content="The patient has been asked to resend their payment screenshot.")


# ---------------------------------------------------------------------------
# Main webhook endpoint (POST) — mirrors n8n Route By Type
# ---------------------------------------------------------------------------
@app.post("/webhook/whatsapp")
async def receive_message(request: Request) -> Response:
    body = await request.body()

    # Optional signature verification
    if settings.whatsapp_webhook_secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not whatsapp_client.verify_webhook_signature(body, sig, settings.whatsapp_webhook_secret):
            log.warning("Invalid webhook signature")
            return Response(status_code=403)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=400)

    # Parse the WhatsApp entry
    try:
        entry = payload["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        messages = value.get("messages", [])
        contacts = value.get("contacts", [])
    except (KeyError, IndexError):
        return JSONResponse({"status": "ok"})

    if not messages:
        # No "messages" — check for delivery-status updates (sent/delivered/
        # read/failed) instead. Meta's 200 OK on send only means "accepted",
        # not "delivered" — this is the only place a failure ever surfaces.
        for st in value.get("statuses", []):
            status = st.get("status", "")
            recipient = st.get("recipient_id", "")
            if status == "failed":
                errors = st.get("errors", [])
                log.error(f"WhatsApp delivery FAILED to {recipient}: {errors}")
            else:
                log.info(f"WhatsApp status for {recipient}: {status}")
        return JSONResponse({"status": "ok"})

    msg = messages[0]
    msg_type = msg.get("type", "")
    wa_id = contacts[0].get("wa_id", "") if contacts else msg.get("from", "")

    if not wa_id:
        return JSONResponse({"status": "ok"})

    log.info(f"Message from {wa_id}: type={msg_type}")

    # Extract text and reply_id based on message type
    text = ""
    reply_id = ""

    if msg_type == "text":
        text = (msg.get("text", {}).get("body", "") or "").strip()
    elif msg_type == "interactive":
        inter = msg.get("interactive", {})
        if "button_reply" in inter:
            reply_id = inter["button_reply"].get("id", "")
            text = inter["button_reply"].get("title", "")
        elif "list_reply" in inter:
            reply_id = inter["list_reply"].get("id", "")
            text = inter["list_reply"].get("title", "")

    # ROUTE: text or interactive
    if msg_type in ("text", "interactive"):
        # Doctor tapped any approve/reject button — only the clinic hotline
        # can act on these; a patient typing matching text must never trigger.
        if _is_clinic_hotline(wa_id) and (
            reply_id.startswith("payok_")
            or reply_id.startswith("payno_")
            or reply_id.startswith("apptok_")
            or reply_id.startswith("apptno_")
            or reply_id.startswith("enrollok_")
            or reply_id.startswith("enrollno_")
        ):
            if reply_id.startswith("payok_") or reply_id.startswith("payno_"):
                # Payment screenshot verification
                pid = reply_id.split("_", 1)[1]
                if reply_id.startswith("payok_"):
                    resp = await _approve_payment(pid)
                else:
                    resp = await _reject_payment(pid)
                if resp.status_code != 200:
                    log.warning(f"Payment action {reply_id} did not succeed: {resp.body}")
            elif reply_id.startswith("apptok_") or reply_id.startswith("apptno_"):
                # Appointment doctor approval (buttons, not links)
                bid = reply_id.split("_", 1)[1]
                if reply_id.startswith("apptok_"):
                    await _process_approval(bid)
                else:
                    await _reject_booking(bid)
            elif reply_id.startswith("enrollok_") or reply_id.startswith("enrollno_"):
                # Enrollment doctor approval (buttons, not links)
                bid = reply_id.split("_", 1)[1]
                if reply_id.startswith("enrollok_"):
                    await _process_enrollment_approval(bid)
                else:
                    await _reject_enrollment(bid)
            return JSONResponse({"status": "ok"})

        # Check if the DOCTOR is replying to an approval message. Only the
        # clinic hotline's own number can approve/reject this way — a patient
        # (or anyone else) typing "yes"/"no" in an unrelated chat must never
        # trigger this, or it silently approves/rejects the wrong booking.
        if _is_clinic_hotline(wa_id) and text.lower() in (
            "approve", "confirmed", "yes", "reject", "cancel", "no"
        ):
            oldest_bid = next(iter(_pending_approvals), None)
            if oldest_bid:
                if text.lower() in ("approve", "confirmed", "yes"):
                    await _process_approval(oldest_bid)
                    return JSONResponse({"status": "ok"})
                elif text.lower() in ("reject", "cancel", "no"):
                    await _reject_booking(oldest_bid)
                    return JSONResponse({"status": "ok"})

        # Get existing session
        session = _sessions.get(wa_id, {})
        my_bookings = _bookings.get(wa_id, [])

        # Get calendar busy slots for the selected date (if in date selection)
        busy_hours: set[int] | None = None
        if session.get("step") == "await_date" and reply_id.startswith("date_"):
            try:
                from src.calendar_client import calendar_client
                from src.booking import CLINIC_SLOT_TIMES, CALL_DURATION_MINUTES
                selected_date = reply_id[5:]
                busy_hours = await calendar_client.get_busy_slots(
                    selected_date, CLINIC_SLOT_TIMES, CALL_DURATION_MINUTES
                )
            except Exception as e:
                log.error(f"Failed to get calendar events: {e}")
                busy_hours = set()

        # Run the booking brain
        result = process_message(
            wa_id=wa_id,
            msg_type=msg_type,
            text=text,
            reply_id=reply_id,
            session=session,
            my_bookings=my_bookings,
            busy_hours=busy_hours,
        )

        if result["route"] == "faq":
            rag_results = local_rag.search(text, top_k=3)
            if rag_results and local_rag.is_confident(rag_results):
                # Try LLM-drafted answer grounded in the retrieved context
                draft = await local_rag.draft_answer(text, rag_results)
                if draft:
                    reply_text = draft
                else:
                    # Fallback: direct FAQ answer if LLM drafting failed
                    best = rag_results[0]
                    reply_text = str(best.get("answer", ""))

                # Escalate-to-human suffix on EVERY response
                reply_text += (
                    "\n\nHave more questions? Just type them here — "
                    "our team is happy to help!"
                )
            else:
                # Low confidence: don't guess — fall back gracefully
                reply_text = (
                    "I'm sorry, I don't have specific information about that yet. "
                    "A Kayakalp team member will follow up with you shortly.\n\n"
                    "In the meantime, you can ask me about weight management, "
                    "GLP-1 treatments, skin care, pricing, or appointments."
                )
            await whatsapp_client.send_text(wa_id, reply_text)

        elif result["route"] == "bot":
            # 0b. Screening gate — weight-loss program enrollment is only
            #     allowed AFTER the patient has completed + paid their
            #     screening consultation. Check the screening sheet before
            #     showing the program list.
            if result.get("screening_gate"):
                screening_done = await sheets_client.is_screening_paid(wa_id)
                if not screening_done:
                    alert = (
                        "To enroll in a weight-loss program, your screening "
                        "consultation must be completed and paid for first.\n\n"
                        "Please complete the appointment/screening form first — "
                        'type "book" to schedule your screening call. Once your '
                        "screening is done, you'll be able to select a program."
                    )
                    await whatsapp_client.send_text(wa_id, alert)
                    return JSONResponse({"status": "ok"})

            # 1. Update session
            previous_session = session
            _sessions[wa_id] = result["sessionUpdate"]

            # 2. Send reply — if it fails, roll the session back so the patient
            # isn't stranded waiting on a message (e.g. a list of options)
            # they never actually received.
            msg_payload = result.get("messagePayload")
            if msg_payload:
                msg_payload["to"] = wa_id
                try:
                    await whatsapp_client.send_message(msg_payload)
                except Exception as e:
                    log.error(f"Failed to send WhatsApp message to {wa_id}: {e}")
                    _sessions[wa_id] = previous_session
                    try:
                        await whatsapp_client.send_text(
                            wa_id,
                            "Sorry, something went wrong on our end. Please try that again.",
                        )
                    except Exception:
                        pass
                    return JSONResponse({"status": "ok"})

            # Send clickable website CTA when welcome message is shown
            website_payload = result.get("websitePayload")
            if website_payload:
                website_payload["to"] = wa_id
                try:
                    await whatsapp_client.send_message(website_payload)
                except Exception as e:
                    log.error(f"Failed to send website CTA to {wa_id}: {e}")

            # 3. If booking confirmed, start doctor approval flow
            if result.get("submit"):
                # Weight-loss program enrollment -> form-first pipeline
                # (payment link is only sent after the form is submitted;
                # no calendar event, no appointment approval)
                if result.get("flow") == "enrollment":
                    try:
                        await _start_enrollment_flow(wa_id, result["booking"])
                    except Exception as e:
                        log.error(f"Failed to start enrollment flow: {e}")
                    return JSONResponse({"status": "ok"})

                # Skin / General -> direct payment (no doctor approval)
                if result.get("direct_payment"):
                    try:
                        await _start_direct_payment_flow(wa_id, result["booking"])
                    except Exception as e:
                        log.error(f"Failed to start direct payment flow: {e}")
                    return JSONResponse({"status": "ok"})

                booking = result["booking"]

                # Pre-check: payments sheet — skip doctor approval for
                # returning patients (paid or unpaid) to avoid unnecessary
                # approval notifications.
                # Already paid (in-memory cache)
                if payments.is_already_paid(wa_id):
                    await whatsapp_client.send_text(wa_id, payments.already_booked_patient_text())
                    if settings.clinic_hotline:
                        await whatsapp_client.send_text(
                            settings.clinic_hotline, payments.already_booked_doctor_text(booking)
                        )
                    log.info(f"Booking blocked (paid-cache hit) - {wa_id}")
                    return JSONResponse({"status": "ok"})

                # Payments-sheet lookup
                sheet_match = None
                try:
                    sheet_match = await sheets_client.find_patient_row(
                        booking.get("email", ""), wa_id
                    )
                except Exception as e:
                    log.error(f"Payments-sheet lookup failed for {wa_id}: {e}")

                if sheet_match is not None:
                    row, is_paid = sheet_match
                    # Enrich record from sheet
                    for row_key, book_key in (("name", "full_name"), ("email", "email")):
                        val = next((v for k, v in row.items() if row_key in k and v), "")
                        if val and not booking.get(book_key):
                            booking[book_key] = val

                    if is_paid:
                        # Already has a paid screening call — block
                        await whatsapp_client.send_text(wa_id, payments.already_booked_patient_text())
                        if settings.clinic_hotline:
                            await whatsapp_client.send_text(
                                settings.clinic_hotline, payments.already_booked_doctor_text(booking)
                            )
                        log.info(f"Booking blocked - {wa_id} already has Paid screening call")
                        return JSONResponse({"status": "ok"})

                    # Found but unpaid → skip doctor approval, go straight
                    # to form + payment (returning patient shortcut)
                    log.info(f"Returning unpaid patient {wa_id} — skipping doctor approval")
                    flow = payments.start_flow(booking)
                    flow["created_at"] = datetime.now(timezone.utc).isoformat()
                    payments.set_status(wa_id, payments.AWAITING_PAYMENT)
                    task = asyncio.create_task(_payment_timeline_worker(wa_id))
                    _payment_tasks[wa_id] = task
                    await _begin_appointment_payment(
                        wa_id, booking,
                        body_text=payments.returning_patient_pay_text(booking),
                    )
                    if settings.clinic_hotline:
                        await whatsapp_client.send_text(
                            settings.clinic_hotline,
                            payments.doctor_approved_returning_text(booking),
                        )
                    return JSONResponse({"status": "ok"})

                # Not found → proceed with normal doctor approval
                booking_id = str(uuid.uuid4())[:8]

                # Store pending approval
                _pending_approvals[booking_id] = {
                    "booking": booking,
                    "doctorText": result.get("doctorText", ""),
                    "doctorTextTail": result.get("doctorTextTail", ""),
                    "patientConfirmText": result.get("patientConfirmText", ""),
                    "doctorConfirmText": result.get("doctorConfirmText", ""),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

                # Send doctor approval message WITH BUTTONS (not link clicks)
                doctor_msg = (
                    result.get("doctorText", "")
                    + result.get("doctorTextTail", "")
                )
                if settings.clinic_hotline:
                    try:
                        await whatsapp_client.send_message(
                            _appointment_approval_buttons_payload(
                                settings.clinic_hotline, doctor_msg, booking_id
                            )
                        )
                    except Exception as btn_err:
                        log.error(
                            f"Appointment approval buttons failed for {booking_id}, "
                            f"sending text fallback: {btn_err}"
                        )
                        # Fallback: send text with URL links
                        base_url = settings.app_base_url or "http://localhost:8000"
                        approve_url = f"{base_url}/webhook/approve/{booking_id}"
                        await whatsapp_client.send_text(
                            settings.clinic_hotline,
                            f"{doctor_msg}\n\nTap to APPROVE:\n{approve_url}",
                        )
                    log.info(f"Doctor approval request sent for booking {booking_id}")

                # Start auto-approve timer (10 minutes = 600 seconds)
                task = asyncio.create_task(
                    _auto_approve_after_delay(booking_id, _pending_approvals.get(booking_id, {}), 600)
                )
                _auto_approve_tasks[booking_id] = task

        return JSONResponse({"status": "ok"})

    # ROUTE: image
    elif msg_type == "image":
        # Payment screenshot? (patient has an active flow awaiting payment proof)
        pay_flow = payments.get_flow(wa_id)
        if pay_flow and pay_flow["status"] in (payments.AWAITING_PAYMENT, payments.VERIFYING):
            media_id = msg.get("image", {}).get("id", "")
            if media_id:
                try:
                    media_info = await whatsapp_client.get_media_url(media_id)
                    media_url = media_info.get("url", "")
                    if media_url:
                        img_bytes = await whatsapp_client.download_media(media_url)
                        payment_id = str(uuid.uuid4())[:8]
                        _screenshot_approvals[payment_id] = {
                            "wa_id": wa_id,
                            "booking": pay_flow["booking"],
                            "kind": pay_flow.get("kind", "appointment"),
                            "media_bytes": img_bytes,
                            "mime_type": media_info.get("mime_type", "image/jpeg"),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        pay_flow["status"] = payments.VERIFYING
                        pay_flow["payment_id"] = payment_id

                        base_url = settings.app_base_url or "http://localhost:8000"
                        b = pay_flow["booking"]
                        is_enroll = pay_flow.get("kind") == "enrollment"
                        if is_enroll:
                            what_line = f"Plan: {b.get('plan', '')}"
                            header = "Program enrollment payment verification needed:\n\n"
                        else:
                            what_line = (
                                f"Service: {b.get('service', '')} on {b.get('appt_date', '')}"
                                f" at {b.get('appt_time', '')}"
                            )
                            header = "Payment verification needed:\n\n"

                        # Send the actual screenshot into the doctor's chat so
                        # they can see it without opening any link.
                        screenshot_url = f"{base_url}/media/{payment_id}"
                        caption = (
                            f"Payment screenshot — {b.get('full_name', '')}\n"
                            f"{what_line}\n"
                            f"Amount: Rs. {settings.payment_amount or b.get('amount') or '(see screenshot)'}"
                        )
                        try:
                            await whatsapp_client.send_image_by_url(
                                settings.clinic_hotline, screenshot_url, caption=caption
                            )
                        except Exception as img_err:
                            log.error(
                                f"Could not send screenshot image to doctor "
                                f"(is APP_BASE_URL publicly reachable?): {img_err}"
                            )

                        approve_hint = (
                            "APPROVE (confirm enrollment)"
                            if is_enroll
                            else "APPROVE (send calendar invite)"
                        )
                        doctor_msg = (
                            f"{header}"
                            f"Name: {b.get('full_name', '')}\n"
                            f"{what_line}\n"
                            f"Patient phone: {wa_id}\n"
                            f"Amount: Rs. {settings.payment_amount or b.get('amount') or '(see screenshot)'}\n\n"
                            "Please verify the payment screenshot above:"
                        )
                        if settings.clinic_hotline:
                            try:
                                await whatsapp_client.send_message(
                                    _verify_buttons_payload(settings.clinic_hotline, doctor_msg, payment_id)
                                )
                            except Exception as btn_err:
                                log.error(
                                    f"Verification buttons failed for {payment_id}, "
                                    f"sending text fallback: {btn_err}"
                                )
                                await whatsapp_client.send_text(
                                    settings.clinic_hotline,
                                    f"{doctor_msg}\n\n"
                                    f"{approve_hint}:\n{base_url}/webhook/payment/approve/{payment_id}\n\n"
                                    f"REJECT (ask patient to resend):\n{base_url}/webhook/payment/reject/{payment_id}",
                                )
                        await whatsapp_client.send_text(wa_id, payments.screenshot_ack_text())
                        log.info(f"Payment screenshot received from {wa_id} ({payment_id})")
                except Exception as e:
                    log.error(f"Payment screenshot handling error: {e}")
                    await whatsapp_client.send_text(
                        wa_id,
                        "Sorry, we could not process your screenshot. Please try resending it.",
                    )
            return JSONResponse({"status": "ok"})

        media_id = msg.get("image", {}).get("id", "")
        if media_id:
            try:
                media_info = await whatsapp_client.get_media_url(media_id)
                media_url = media_info.get("url", "")
                if media_url:
                    image_bytes = await whatsapp_client.download_media(media_url)
                    mime_type = media_info.get("mime_type", "image/jpeg")
                    analysis = await image_analyzer.analyze_image(image_bytes, mime_type)
                    await whatsapp_client.send_text(wa_id, analysis)
            except Exception as e:
                log.error(f"Image analysis error: {e}")
                await whatsapp_client.send_text(
                    wa_id,
                    "Sorry, we could not analyze your image right now as our service is busy. Please try resending it in a few minutes.",
                )
        return JSONResponse({"status": "ok"})

    # ROUTE: document
    elif msg_type == "document":
        doc = msg.get("document", {})
        media_id = doc.get("id", "")
        if media_id:
            try:
                media_info = await whatsapp_client.get_media_url(media_id)
                media_url = media_info.get("url", "")
                if media_url:
                    doc_bytes = await whatsapp_client.download_media(media_url)
                    mime_type = doc.get("mime_type", "application/pdf")
                    analysis = await image_analyzer.analyze_document(doc_bytes, mime_type)
                    await whatsapp_client.send_text(wa_id, analysis)
            except Exception as e:
                log.error(f"Document analysis error: {e}")
                await whatsapp_client.send_text(
                    wa_id,
                    "Sorry, we could not analyze that document right now. Please try again later.",
                )
        return JSONResponse({"status": "ok"})

    log.info(f"Unhandled message type: {msg_type}")
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {
        "status": "healthy",
        "faq_rows": len(local_rag.rows),
        "active_sessions": len(_sessions),
        "pending_approvals": len(_pending_approvals),
        "active_payment_flows": len(payments._flows),
        "pending_payment_proofs": len(_screenshot_approvals),
    }


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@app.on_event("startup")
async def startup():
    log.info("Kaya starting up...")
    log.info(f"Loaded {len(local_rag.rows)} FAQ rows")
    log.info(f"WhatsApp phone number ID: {settings.whatsapp_phone_number_id}")
    log.info(f"Clinic hotline: {settings.clinic_hotline}")
    log.info(
        f"Google credentials: {settings.google_credentials_path} "
        f"(exists={Path(settings.google_credentials_path).is_file()})"
    )
    if not settings.screening_form_url:
        log.warning("SCREENING_FORM_URL is empty - form step will be skipped in messages")
    if not settings.upi_id:
        log.warning("UPI_ID is empty - payment instructions will be deferred until it is set")
    if settings.enrollment_sheet_id:
        if await sheets_client.check_access():
            log.info(
                f"Enrollment payments sheet readable "
                f"(id={settings.enrollment_sheet_id}, gid={settings.enrollment_sheet_gid or 'first tab'})"
            )
        else:
            log.warning(
                "ENROLLMENT_SHEET_ID is set but NOT readable - share the sheet with "
                "the service account email from GOOGLE_CREDENTIALS_PATH, or enrollment "
                "will always fall back to the form + payment flow"
            )


@app.on_event("shutdown")
async def shutdown():
    # Cancel all pending auto-approve tasks
    for task in _auto_approve_tasks.values():
        if not task.done():
            task.cancel()
    _auto_approve_tasks.clear()

    # Cancel payment reminder timelines
    for task in _payment_tasks.values():
        if not task.done():
            task.cancel()
    _payment_tasks.clear()

    await whatsapp_client.close()
    await image_analyzer.close()
    await sheets_client.close()
