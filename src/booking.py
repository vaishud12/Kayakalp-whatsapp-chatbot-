"""Booking state machine — mirrors the n8n 'Booking Brain' Code node.

Handles the full appointment booking flow:
  idle -> greet/menu -> await_name -> await_service -> await_date -> await_time
  -> await_email -> await_confirm -> (submit) -> doctor approval -> calendar event

Also handles:
- FAQ/Ask queries when the user is not in a booking flow.
- Weight-loss program enrollment (3rd menu option):
  menu -> await_plan -> submit(flow="enrollment") -> form + payment
  -> screenshot verification by doctor -> team outreach (NO calendar event).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from src.rag import local_rag
from src.config import settings


# ---------------------------------------------------------------------------
# Message builders (mirrors n8n helpers)
# ---------------------------------------------------------------------------

def _text_msg(to: str, body: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }


def _button_msg(to: str, body: str, buttons: list[dict]) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": buttons},
        },
    }


def get_welcome_message() -> str:
    """Generate the welcome message with current website URL from settings."""
    website_url = settings.website_url or "https://kayakalp.in"  # Default fallback
    return (
        "🌟 *Welcome to KayaKalp Clinic!* 🌟\n\n"
        "We're Dr. Lekha Jadhav's medical clinic in Kharadi, Pune — offering "
        "medically supervised weight management (including GLP-1 therapy), "
        "skin, hair, and body aesthetics treatments.\n\n"
        "Our 4-doctor panel covers internal medicine, gynecology, and "
        "dermatology/nutrition.\n\n"
        "📋 *What would you like to do?*\n"
        "🔹 *Book an appointment* — Weight Management, Skin Care, or General Consultation\n"
        "🔹 *Ask your queries* — Get answers about treatments, GLP-1, pricing, or appointments\n"
        "🔹 *Weight Loss Programs* — Clinical Expert Supervision, Precision Nutrition, or "
        "The Total Transformation Elite\n\n"
        f"🌐 *Visit our website:* {website_url}\n\n"
        "✨ *\"The best time to start your wellness journey is today.\"*\n\n"
        "We're here to help you achieve your health and beauty goals!\n"
    )


def _list_msg(
    to: str,
    body: str,
    rows: list[dict],
    button_text: str = "View",
    section_title: str = "Options",
) -> dict[str, Any]:
    # WhatsApp rejects the whole message with 400 if rows > 10 — better to
    # silently truncate than to crash the send and desync the session.
    rows = rows[:_WHATSAPP_LIST_ROW_LIMIT]
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text,
                "sections": [{"title": section_title, "rows": rows}],
            },
        },
    }


def _btn(btn_id: str, title: str) -> dict:
    return {"type": "reply", "reply": {"id": btn_id, "title": title}}


def _website_cta_payload(to: str, url: str, display_text: str = "🌐 Visit Our Website") -> dict[str, Any]:
    """Interactive CTA button message that opens the website in a browser."""
    return {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": f"🌐 Visit our official website for services, doctors, and more:\n{url}"},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": display_text[:25],
                    "url": url,
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Pre-built button / list sets (mirrors n8n exactly)
# ---------------------------------------------------------------------------

MENU_BUTTONS = [
    _btn("book", "Book appointment"),
    _btn("ask", "Ask your Queries"),
    _btn("enroll", "Weight Loss Programs"),
]
SVC_BUTTONS = [
    _btn("svc_weight", "Weight Mgmt"),
    _btn("svc_skin", "Skin Care"),
    _btn("svc_general", "General"),
]
CONFIRM_BUTTONS = [_btn("confirm", "Confirm"), _btn("cancel", "Cancel")]

# ---------------------------------------------------------------------------
# Weight-loss program plans (mirrors kayakalpbydrlekha.com -> OUR PLANS)
# ---------------------------------------------------------------------------
PLANS = [
    {
        "id": "clinical",
        "row_id": "plan_clinical",
        "name": "Clinical Expert Supervision",
        "short": "Clinical Supervision",
        "price": 3000,
        "tagline": "Safe GLP-1 medical backing",
        "features": (
            "Unlimited Prescriptions — direct access to our 4-doctor team to "
            "manage side effects, plus weekly 20-minute progress calls with "
            "your doctor."
        ),
        "match": ["clinical", "supervision"],
    },
    {
        "id": "nutrition",
        "row_id": "plan_nutrition",
        "name": "Precision Nutrition Plan",
        "short": "Precision Nutrition Plan",
        "price": 3000,
        "tagline": "Custom GLP-1 nutrition mapping",
        "features": (
            "Smart Weekly Meal Plans adjusted to your GLP-1 appetite, with "
            "weekly dietitian check-ins so you lose fat, not muscle."
        ),
        "match": ["nutrition", "diet", "meal"],
    },
    {
        "id": "elite",
        "row_id": "plan_elite",
        "name": "The Total Transformation Elite",
        "short": "Transformation Elite",
        "price": 6000,
        "tagline": 'The "All-Inclusive" Concierge',
        "features": (
            "Complete Doctor + Nutrition access with priority SOS messaging, "
            "plus multi-specialty care (MD Medicine, Gynecology, Skin/Hair)."
        ),
        "match": ["elite", "transformation", "total", "all-inclusive"],
    },
]

PLAN_LIST_BODY = (
    "Transform your body with our medically supervised GLP-1 weight loss "
    "programs, designed and monitored by our team of 4 doctors:\n\n"
    "1. *Clinical Expert Supervision* — Rs.3,000\n"
    "Unlimited prescriptions + weekly doctor progress calls.\n\n"
    "2. *Precision Nutrition Plan* — Rs.3,000\n"
    "Custom GLP-1 nutrition mapping + expert dietitian support.\n\n"
    "3. *The Total Transformation Elite* — Rs.6,000\n"
    'The all-inclusive concierge: 360° doctor + nutrition care.\n\n'
    "Tap below to choose your program:"
)


def _plan_rows() -> list[dict]:
    return [
        {
            "id": p["row_id"],
            "title": p["short"][:24],
            "description": f"{p['tagline']} | Rs. {p['price']:,}"[:72],
        }
        for p in PLANS
    ]


def _plans_list_msg(to: str, body: str = PLAN_LIST_BODY) -> dict[str, Any]:
    return _list_msg(to, body, _plan_rows(), "View plans", "Weight Loss Programs")

DISCLAIMER = (
    "\n\nPlease consult a qualified doctor at Kayakalp for your personal "
    "medical decisions."
)

_CANCEL_RE = re.compile(r"^(cancel|stop|exit|quit)$", re.IGNORECASE)
_GREET_RE = re.compile(
    r"^(hi|hii|hiii|hello|helo|hey|hai|menu|start|restart)$", re.IGNORECASE
)
_MEDICAL_RE = re.compile(r"glp|medical|skin|hair|body|derma|treatment|health")

# Indexed by datetime.weekday() -> 0=Mon ... 6=Sun
_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
_CLINIC_CLOSED_WEEKDAY = 6  # Sunday
CALL_DURATION_MINUTES = 60

# Clinic hours 10:00 AM - 7:00 PM, hourly slots (10 slots/day — matches
# WhatsApp's 10-row list limit exactly). Slot IDs are HHMM ints (1000 = 10:00).
CLINIC_SLOT_TIMES = [h * 100 for h in range(10, 20)]

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slot_label(hhmm: int) -> str:
    h, m = divmod(hhmm, 100)
    ap = "AM" if h < 12 else "PM"
    hh = h % 12 or 12
    return f"{hh:02d}:{m:02d} {ap}"


def _parse_slot(label: str) -> int:
    s = label.strip()
    hh = int(s[:2])
    mm = int(s[3:5])
    ap = s[-2:].upper()
    if ap == "PM" and hh != 12:
        hh += 12
    if ap == "AM" and hh == 12:
        hh = 0
    return hh * 100 + mm


def _build_iso(date_str: str, hhmm: int, add_minutes: int = 0) -> str:
    parts = date_str.split("-")
    total_minutes = (hhmm // 100) * 60 + (hhmm % 100) + add_minutes
    h, m = divmod(total_minutes, 60)
    return f"{parts[2]}-{parts[1]}-{parts[0]}T{h:02d}:{m:02d}:00+05:30"


def _build_date_rows() -> list[dict]:
    now_ist = datetime.now(IST)
    rows: list[dict] = []
    d = 0
    while len(rows) < 7 and d <= 14:
        dt = now_ist + timedelta(days=d)
        dow = dt.weekday()
        # Skip today once the last bookable slot for the day has already passed
        is_today = d == 0
        now_total_min = now_ist.hour * 60 + now_ist.minute
        last_slot = CLINIC_SLOT_TIMES[-1]
        last_slot_total_min = (last_slot // 100) * 60 + (last_slot % 100)
        today_has_slots = now_total_min < last_slot_total_min
        if dow != 6 and (not is_today or today_has_slots):
            dd = f"{dt.day:02d}"
            mm = f"{dt.month:02d}"
            yyyy = dt.year
            label = f"{_DAY_NAMES[dow]}, {dd} {_MONTH_NAMES[dt.month - 1]}"
            rows.append({"id": f"date_{dd}-{mm}-{yyyy}", "title": label})
        d += 1
    return rows


# ---------------------------------------------------------------------------
# FAQ helpers
# ---------------------------------------------------------------------------

def _is_medical_category(cat: str) -> bool:
    return bool(_MEDICAL_RE.search((cat or "").lower()))


def _answer_faq_row(row: dict[str, Any], to: str = "") -> dict[str, Any]:
    """Answer an FAQ row.  Shows the answer for ALL questions (including
    those flagged for escalation) and appends an escalate-to-human suffix
    on every response so the patient can always reach the team."""
    reply_text = str(row.get("answer", ""))

    suffix = DISCLAIMER if _is_medical_category(row.get("category", "")) else ""
    reply_text += suffix

    # Escalate-to-human suffix on EVERY response
    reply_text += (
        "\n\nHave more questions? Just type them here — "
        "our team is happy to help!"
    )
    return _text_msg(to, reply_text)


def _related_faqs(query: str) -> list[dict[str, Any]]:
    """Find related FAQs for button browsing.  Uses BM25 search from the
    RAG module for consistent, high-quality retrieval."""
    results = local_rag.search(query, top_k=8)
    return [{"row": r, "score": r.get("_score", 0)} for r in results]


def _faq_fallback(to: str) -> dict[str, Any]:
    return _text_msg(
        to,
        "I am sorry, I could not find an answer to that yet. "
        "A Kayakalp team member will follow up shortly.\n\n"
        "In the meantime, you can ask me about weight management, "
        "GLP-1 treatments, skin care, pricing, or appointments.\n\n"
        'You can also send "book" to make an appointment.',
    )


def _answer_question(query: str, to: str) -> dict[str, Any]:
    scored = _related_faqs(query)
    if not scored:
        return _faq_fallback(to)

    top_score = scored[0]["score"]

    # If the top score is too low, the query likely has no good FAQ match.
    # ChromaDB cosine-similarity scores range 0–1. We use 0.58 as the lower
    # bound for the FAQ-mode handler (the user is in a question-asking flow)
    # so casual phrasings like "explain all this" still get an answer, but
    # clearly out-of-scope queries ("meaning of life") are rejected.
    if top_score < 0.58:
        return _faq_fallback(to)

    total_words = len(
        [w for w in re.sub(r"[^a-z0-9 ]", " ", query.lower()).split() if w]
    )
    browse_mode = total_words <= 2 and len(scored) >= 3

    if browse_mode:
        # Only show entries with a meaningful score (at least 40% of top)
        ambiguous = [s for s in scored if s["score"] >= top_score * 0.4][:6]
        if not ambiguous:
            return _answer_faq_row(scored[0]["row"], to)
        list_rows = []
        for s in ambiguous:
            r = s["row"]
            t = str(r.get("question", ""))
            if len(t) > 24:
                t = t[:23] + "\u2026"
            dsc = str(r.get("category", ""))
            if len(dsc) > 72:
                dsc = dsc[:71] + "\u2026"
            list_rows.append({
                "id": "faq_" + str(r.get("external_id", r.get("id", ""))),
                "title": t,
                "description": dsc,
            })
        return _list_msg(
            to,
            "I found a few related questions. Tap the one you meant:",
            list_rows,
            "View questions",
            "FAQs",
        )

    return _answer_faq_row(scored[0]["row"], to)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

def process_message(
    wa_id: str,
    msg_type: str,
    text: str,
    reply_id: str,
    session: dict[str, Any],
    my_bookings: list[dict[str, Any]],
    busy_hours: set[int] | None = None,
) -> dict[str, Any]:
    step = session.get("step", "idle")
    full_name = session.get("full_name", "")
    service = session.get("service", "")
    appt_date = session.get("appt_date", "")
    appt_time = session.get("appt_time", "")
    email = session.get("email", "")
    enroll_plan = session.get("enroll_plan") or None
    flow_kind = "booking"

    lower = text.lower().strip()
    num = "".join(c for c in text if c.isdigit())

    to = wa_id
    payload = None
    website_payload = None
    submit = False
    new_step = step
    route = "bot"
    screening_gate = False

    # Global cancel
    if step != "idle" and _CANCEL_RE.match(lower):
        new_step = "idle"
        full_name = service = appt_date = appt_time = email = ""
        enroll_plan = None
        payload = _text_msg(to, 'No problem, I have cancelled that. Send "book" anytime to start again.')

    # Greeting words -> show main menu
    elif _GREET_RE.match(lower):
        new_step = "await_menu"
        full_name = service = appt_date = appt_time = email = ""
        enroll_plan = None
        payload = _button_msg(to, get_welcome_message(), MENU_BUTTONS)

    # FAQ button tap (faq_xxx)
    elif reply_id.startswith("faq_"):
        eid = reply_id[4:]
        picked = local_rag.get_by_external_id(eid)
        new_step = "asking"
        if picked:
            payload = _answer_faq_row(picked, to)
        else:
            payload = _faq_fallback(to)

    # IDLE
    elif step == "idle":
        wants_appt = bool(
            re.search(r"appoint|appoin|book|schedul", lower)
            or reply_id == "book"
        )
        wants_enroll = bool(
            re.search(r"enroll|program|weight.?loss", lower) or reply_id == "enroll"
        )
        if wants_appt:
            new_step = "await_name"
            payload = _text_msg(to, "Great! Lets book your appointment.\n\nPlease type your full name:")
        elif wants_enroll:
            new_step = "await_plan"
            payload = _plans_list_msg(to)
        else:
            new_step = "await_menu"
            payload = _button_msg(to, get_welcome_message(), MENU_BUTTONS)

    # AWAIT MENU
    elif step == "await_menu":
        if reply_id == "book" or num == "1":
            new_step = "await_name"
            payload = _text_msg(to, "Great! Lets book your appointment.\n\nPlease type your full name:")
        elif reply_id == "ask" or num == "2":
            new_step = "asking"
            payload = _text_msg(
                to,
                "Sure! Please type your question about Kayakalp (treatments, GLP-1, pricing, timings, etc.) "
                "and I will answer from our clinic information.\n\n"
                'You can type "book" anytime to make an appointment.',
            )
        elif reply_id == "enroll" or num == "3":
            new_step = "await_plan"
            payload = _plans_list_msg(to)
            screening_gate = True
        else:
            payload = _button_msg(to, "Please choose an option:", MENU_BUTTONS)

    # ASKING (FAQ mode)
    elif step == "asking":
        wants_appt = bool(
            re.match(r"^(book|appointment|appoint|appoin|schedule)$", lower)
            or reply_id == "book"
        )
        # Only jump to enrollment if user explicitly wants to buy/subscribe.
        # Plain questions about "programs" or "plans" stay in FAQ mode so
        # the RAG can answer them — don't yank the user into a purchase.
        wants_enroll = bool(
            re.search(r"enroll|subscribe|sign.?up|buy.?plan|start.?plan|join.?plan", lower)
            or reply_id == "enroll"
        )
        if wants_appt:
            new_step = "await_name"
            payload = _text_msg(to, "Great! Lets book your appointment.\n\nPlease type your full name:")
        elif wants_enroll:
            new_step = "await_plan"
            payload = _plans_list_msg(to)
            screening_gate = True
        elif not text:
            payload = _text_msg(to, 'Please type your question, or type "book" to make an appointment.')
        else:
            new_step = "asking"
            payload = _answer_question(text, to)

    # AWAIT PLAN (weight-loss program enrollment)
    elif step == "await_plan":
        plan = None
        if reply_id.startswith("plan_"):
            pid = reply_id[5:]
            plan = next((p for p in PLANS if p["id"] == pid), None)
        elif text:
            t = lower
            for p in PLANS:
                if any(k in t for k in p["match"]):
                    plan = p
                    break
        if not plan:
            new_step = "await_plan"
            payload = _plans_list_msg(to, "Please tap one of the programs below to continue:")
        else:
            enroll_plan = plan
            service = f"{plan['name']} — Weight Loss Program"
            flow_kind = "enrollment"
            new_step = "idle"
            submit = True
            payload = None

    # AWAIT NAME
    elif step == "await_name":
        if not text:
            payload = _text_msg(to, "Please type your full name to continue:")
        else:
            full_name = text
            new_step = "await_service"
            payload = _button_msg(to, f"Thanks {full_name}! Which service would you like?", SVC_BUTTONS)

    # AWAIT SERVICE
    elif step == "await_service":
        svc = ""
        if reply_id == "svc_weight" or re.search(r"weight|glp", lower):
            svc = "Weight Management"
        elif reply_id == "svc_skin" or re.search(r"skin|hair|derma|aesthet", lower):
            svc = "Skin Care"
        elif reply_id == "svc_general" or re.search(r"general|consult", lower):
            svc = "General Consultation"

        if not svc:
            payload = _button_msg(to, "Please tap one of the services below:", SVC_BUTTONS)
        else:
            service = svc
            new_step = "await_date"
            payload = _list_msg(
                to,
                f"Great choice! Please pick a date for your {service} appointment:",
                _build_date_rows(),
                "Choose a date",
                "Available dates",
            )

    # AWAIT DATE
    elif step == "await_date":
        if reply_id.startswith("date_"):
            appt_date = reply_id[5:]
            busy = busy_hours or set()
            now_ist = datetime.now(IST)
            dd, mm, yyyy = appt_date.split("-")
            is_today = (int(dd), int(mm), int(yyyy)) == (now_ist.day, now_ist.month, now_ist.year)
            now_total_min = now_ist.hour * 60 + now_ist.minute
            all_slots = [
                t for t in CLINIC_SLOT_TIMES
                if not is_today or ((t // 100) * 60 + (t % 100)) > now_total_min
            ]
            free_rows = [
                {"id": f"time_{t}", "title": _slot_label(t)}
                for t in all_slots
                if t not in busy
            ]
            if not free_rows:
                new_step = "await_date"
                payload = _list_msg(
                    to,
                    "That date is fully booked. Please pick another date:",
                    _build_date_rows(),
                    "Choose a date",
                    "Available dates",
                )
            else:
                new_step = "await_time"
                payload = _list_msg(
                    to,
                    f"These slots are free on {appt_date}. Please pick a time:",
                    free_rows,
                    "Choose a time",
                    "Available times",
                )
        else:
            payload = _text_msg(to, 'Please tap a date from the list above. Send "cancel" to stop.')

    # AWAIT TIME
    elif step == "await_time":
        if reply_id.startswith("time_"):
            hhmm = int(reply_id[5:])
            appt_time = _slot_label(hhmm)
            # Skin / General -> no email needed (no call to schedule)
            if service in ("Skin Care", "General Consultation"):
                new_step = "await_confirm"
                summary = (
                    "Please confirm your appointment:\n\n"
                    f"Name: {full_name}\n"
                    f"Service: {service}\n"
                    f"Date: {appt_date}\n"
                    f"Time: {appt_time}"
                )
                payload = _button_msg(to, summary, CONFIRM_BUTTONS)
            else:
                new_step = "await_email"
                payload = _text_msg(
                    to,
                    "Almost done! Please type your email address so we can send you a calendar invite for the call:",
                )
        else:
            payload = _text_msg(to, 'Please tap a time from the list above. Send "cancel" to stop.')

    # AWAIT EMAIL
    elif step == "await_email":
        email_re = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
        if not email_re.match(text):
            payload = _text_msg(
                to,
                "That does not look like a valid email. Please type a valid email address (e.g. name@example.com):",
            )
        else:
            email = text.strip()
            new_step = "await_confirm"
            summary = (
                "Please confirm your appointment:\n\n"
                f"Name: {full_name}\n"
                f"Service: {service}\n"
                f"Date: {appt_date}\n"
                f"Time: {appt_time}\n"
                f"Email: {email}"
            )
            payload = _button_msg(to, summary, CONFIRM_BUTTONS)

    # AWAIT CONFIRM
    elif step == "await_confirm":
        if reply_id == "confirm" or num == "1" or re.match(r"^(confirm|yes)$", lower):
            submit = True
            new_step = "idle"
            # Skin / General -> direct payment (no doctor approval needed)
            if service in ("Skin Care", "General Consultation"):
                payload = _text_msg(
                    to,
                    f"Great! Your {service} appointment is confirmed.\n\n"
                    f"📅 {appt_date} at {appt_time}\n\n"
                    "A payment link of Rs. 400 will be sent to you now. "
                    "Once payment is received, your appointment will be finalized.",
                )
            else:
                # Weight Management -> doctor approval first
                payload = _text_msg(
                    to,
                    "Thank you! Your appointment request has been sent to our doctor for approval.\n\n"
                    f"{service} on {appt_date} at {appt_time}.\n\n"
                    "We will confirm shortly. If there is no response, it will be auto-confirmed within 10 minutes.",
                )
        elif reply_id == "cancel" or num == "2" or re.match(r"^(cancel|no)$", lower):
            new_step = "idle"
            full_name = service = appt_date = appt_time = email = ""
            payload = _text_msg(to, 'Okay, I have cancelled that booking. Send "book" to start again.')
        else:
            payload = _button_msg(to, "Please tap Confirm or Cancel:", CONFIRM_BUTTONS)

    # UNKNOWN STEP
    else:
        new_step = "idle"
        payload = _faq_fallback(to)

    # Build ISO times for calendar
    start_iso = ""
    end_iso = ""
    if appt_date and appt_time:
        bk_slot = _parse_slot(appt_time)
        start_iso = _build_iso(appt_date, bk_slot, 0)
        end_iso = _build_iso(appt_date, bk_slot, CALL_DURATION_MINUTES)

    # Doctor / patient confirmation texts (mirrors n8n)
    doctor_text = (
        "New appointment request for an introduction call:\n\n"
        f"Name: {full_name}\n"
        f"Service: {service}\n"
        f"Date: {appt_date}\n"
        f"Time: {appt_time}\n"
        f"Patient phone: {wa_id}\n\n"
        "Tap to APPROVE this booking:\n"
    )
    doctor_text_tail = (
        "\n\nIf no action is taken, it will be auto-approved in 10 minutes."
    )
    patient_confirm_text = (
        "Your appointment is confirmed!\n\n"
        f"{service} on {appt_date} at {appt_time}.\n\n"
        "This is an introduction call. Our team will call you at the scheduled time."
    )
    doctor_confirm_text = (
        "Appointment scheduled (approved or auto-approved):\n\n"
        f"Name: {full_name}\n"
        f"Service: {service}\n"
        f"Date: {appt_date}\n"
        f"Time: {appt_time}\n"
        f"Patient phone: {wa_id}\n\n"
        "It has been added to the calendar as an introduction call."
    )

    booking = {
        "wa_id": wa_id,
        "full_name": full_name,
        "service": service,
        "appt_date": appt_date,
        "appt_time": appt_time,
        "email": email,
        "start_iso": start_iso,
        "end_iso": end_iso,
    }
    if enroll_plan:
        booking["plan"] = enroll_plan["name"]
        booking["amount"] = enroll_plan["price"]
    # Skin / General -> direct payment (no approval step)
    if service in ("Skin Care", "General Consultation"):
        booking["payment_amount"] = 400

    # direct_payment flag: True when the patient should receive a payment
    # link immediately (skin / general), False when doctor approval is needed
    # first (weight management / enrollment).
    direct_payment = (
        submit and service in ("Skin Care", "General Consultation")
    )

    return {
        "route": route,
        "messagePayload": payload,
        "websitePayload": website_payload,
        "submit": submit,
        "flow": flow_kind,
        "direct_payment": direct_payment,
        "screening_gate": screening_gate,
        "sessionUpdate": {
            "wa_id": wa_id,
            "step": new_step,
            "full_name": full_name,
            "service": service,
            "appt_date": appt_date,
            "appt_time": appt_time,
            "email": email,
            # The picked plan is transient: it rides out on result["booking"]
            # for THIS reply only and must never leak into later sessions.
            "enroll_plan": (enroll_plan or "") if new_step == "await_plan" else "",
        },
        "booking": booking,
        "doctorText": doctor_text,
        "doctorTextTail": doctor_text_tail,
        "patientConfirmText": patient_confirm_text,
        "doctorConfirmText": doctor_confirm_text,
    }
