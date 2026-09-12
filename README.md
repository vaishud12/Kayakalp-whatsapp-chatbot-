# Kaya — KayaKalp Clinic WhatsApp AI Assistant

**Kaya** is a WhatsApp chatbot for **KayaKalp Clinic** (Dr. Lekha Jadhav). It supports patients on weight-management journeys using GLP-1 therapies (Mounjaro). Pure RAG — no AI chat model for FAQ responses, only local TF-IDF search from the knowledge base.

```
WhatsApp Cloud API ── webhook ──> FastAPI (Python)
                                     │
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
              text/interactive      image           document
                    │                 │                 │
              Booking Brain     Gemini Vision     Gemini Vision
              (state machine)   (analyze image)   (analyze document)
                    │                 │                 │
              ┌─────┴─────┐          │                 │
              │           │          │                 │
         bot flow      FAQ flow      │                 │
         (session     (TF-IDF        │                 │
          machine)    local RAG)     │                 │
              │           │          │                 │
              └─────┬─────┘          │                 │
                    │                │                 │
                    ▼                ▼                 ▼
              WhatsApp Reply    WhatsApp Reply    WhatsApp Reply
```

- **FAQ responses** → Local TF-IDF search over `src/Docs/KayaKalp_WhatsApp_FAQ_RAG.xlsx` (no external AI API)
- **Booking flow** → Full state machine (name → service → date → time → confirm)
- **Image/Document analysis** → Google Gemini Vision (optional, for medical report/photo analysis)
- **WhatsApp** → WhatsApp Cloud API (Meta) webhook + send API

> This is an AI assistant, not a replacement for medical care. All responses include the mandatory disclaimer and route emergencies to the clinic hotline.

---

## Project Structure

```
Whatsapp chatbot/
├── README.md
├── .env.example               # Copy to .env and fill in keys
├── requirements.txt
├── SYSTEM_PROMPT.md           # Kaya's persona and medical safety rules
├── config/
│   └── settings.json          # Non-secret app settings
└── src/
    ├── __init__.py
    ├── main.py                # FastAPI webhook server (entry point)
    ├── booking.py             # Booking state machine (mirrors n8n Code node)
    ├── rag.py                 # Local TF-IDF FAQ search from xlsx
    ├── whatsapp.py            # WhatsApp Cloud API client (send + media)
    ├── config.py              # Loads settings + env vars
    ├── image_analyzer.py      # Google Gemini 3.1 Flash Lite for image/doc analysis
    └── Docs/
        └── KayaKalp_WhatsApp_FAQ_RAG.xlsx   # FAQ knowledge base
```

---

## How It Works

### Message Flow (mirrors n8n workflow)

1. Patient messages the clinic's WhatsApp number.
2. Meta's **WhatsApp Cloud API** delivers the webhook to `POST /webhook/whatsapp`.
3. The server **routes by message type**:
   - **Text / Interactive** → Run the **Booking Brain** state machine:
     - If the user is in a booking flow (await_name, await_service, etc.), advance the state and reply.
     - If the user sends a free-text question, run **TF-IDF search** against the FAQ xlsx. If a clear match is found, return the answer directly. If ambiguous, show a list of options. If no match, respond that a team member will follow up.
     - If the user confirms a booking, log it and optionally submit to Google Apps Script.
   - **Image** → Download → **Gemini Vision** analysis → Reply with findings.
   - **Document** → Download → **Gemini Vision** analysis → Reply with findings.

### Booking State Machine

```
idle → await_menu → await_name → await_service → await_date → await_time → await_email → await_confirm → (submit)
```

At any point the user can send "cancel" to reset to idle.

### Post-Approval Payment Pipeline (slot booked only on payment)

```
Doctor approval
   └─> Patient: confirmation + Google Form link
   └─> Doctor: "awaiting form + payment" notice
        │
        ├─ [24h] reminder ─ [3d] reminder ─ [7d] FINAL reminder (to patient)
        │                                     │
        │                          still unpaid after day 7?
        │                                     └─> Doctor alert w/ patient details
        │                                         (no calendar event was ever created)
        │
        └─> Patient submits Google Form
                └─ Apps Script POSTs /webhook/form {phone, email}
                └─ Patient receives UPI payment instructions
                     └─ Patient sends payment screenshot here
                          └─ Bot stores it, doctor gets View/Approve/Reject links
                               ├─ APPROVE -> calendar event + invite + confirmations
                               └─ REJECT  -> patient asked to resend screenshot
```

- **No calendar event is created until payment is verified** — either automatically (Cashfree webhook) or manually (doctor approves a screenshot).
- Reminders/expiry run as background tasks (`PAY_REMINDER_*` env vars; set sub-second values for testing).
- If the patient's proof is unverified at expiry, the doctor alert says so and the flow stays open for the doctor's decision.
- Screenshot verification links: `GET /webhook/payment/approve/{id}`, `/webhook/payment/reject/{id}`, view at `/media/{id}`.

#### Automatic payment verification (Cashfree, optional)

Set `CASHFREE_APP_ID` and `CASHFREE_SECRET_KEY` in `.env` to switch from manual
UPI-screenshot review to automatic verification:

```
Patient submits Google Form
   └─> Bot creates a Cashfree Payment Link, sends it via WhatsApp
        └─> Patient pays on Cashfree's hosted page (UPI/card/netbanking)
             └─> Cashfree calls POST /webhook/cashfree (signed)
                  └─> Bot verifies signature, marks PAID
                       └─> Calendar event + confirmations sent automatically
```

Setup:

1. Create a [Cashfree](https://www.cashfree.com/) account (test/sandbox mode needs no KYC).
2. Dashboard → Developers → API Keys → copy the App ID and Secret Key into `.env` (`CASHFREE_APP_ID`, `CASHFREE_SECRET_KEY`). Keep `CASHFREE_ENV=sandbox` until you're ready to accept real payments.
3. Dashboard → Developers → Webhooks → add a webhook pointing at `https://YOUR-DOMAIN/webhook/cashfree`, subscribed to Payment Links events. Set a webhook secret there and put it in `CASHFREE_WEBHOOK_SECRET` (or leave blank to reuse `CASHFREE_SECRET_KEY`).
4. Go live: complete Cashfree KYC (PAN, bank details, business proof), switch `CASHFREE_ENV=production`, and swap in your live API keys.

When Cashfree is not configured, the flow falls back to the manual UPI + screenshot review described above — nothing else changes.

#### Google Form setup (one-time)

In your screening Google Form, add an Apps Script (Extensions -> Apps Script):

```javascript
function onFormSubmit(e) {
  const r = e.response.getItemResponses();
  UrlFetchApp.fetch('https://YOUR-DOMAIN/webhook/form', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify({
      phone: r[0].getResponse(),   // adjust index: the phone question
      name:  r[1].getResponse(),
      email: r[2].getResponse(),   // used for the calendar invite
    }),
  });
}
```

Then add a trigger: Triggers -> Add Trigger -> `onFormSubmit` -> From form -> On form submit.

#### Enrollment Google Form setup (weight-loss programs)

The enrollment flow is **sheet-verified first**: when a patient picks a plan,
the bot looks up their WhatsApp number in the payments Google Sheet
(`ENROLLMENT_SHEET_ID` / `ENROLLMENT_SHEET_GID`). If a row matches their phone
AND payment status is **Paid**, they are enrolled instantly — no form, no
payment steps (patient + doctor get the confirmation, and it's logged).
Otherwise the normal form-first flow runs: plan picked -> form only ->
**payment button sent automatically once this webhook fires**. Use the
ready-made script in
[`scripts/apps_script_enrollment_form.js`](scripts/apps_script_enrollment_form.js):

1. Open the enrollment form (https://forms.gle/KFwguS13cwEbrSFn8) -> Extensions -> Apps Script.
2. Paste the script, set `WEBHOOK_URL` to your public `APP_BASE_URL`.
3. Make sure the form has a **WhatsApp number** question — patients must enter
   the same number they chat on (the bot matches by phone; email isn't known
   yet at this stage).
4. Add the trigger: `onFormSubmit` -> From form -> On form submit.

For the sheet check, share the payments sheet (Viewer access) with the service
account email inside `GOOGLE_CREDENTIALS_PATH` — otherwise lookups fail and
every patient falls back to the form → payment flow (a startup warning tells you).

Sequence after setup:

```
Plan selected
     ├─ phone found in payments sheet with status "Paid"
     |      └─> enrolled instantly (confirmation sent, no form, no payment)
     └─ not found / unpaid
            └─> form link sent -> patient submits form
                 └─> Apps Script POSTs /webhook/form {phone, name, email}
                       └─> [ Pay Now ] button (Cashfree link for the plan amount)
                             └─> tap -> payment page -> auto-verified via Cashfree webhook
```

### FAQ Search (RAG)

- No external embedding API needed — pure **TF-IDF** with IDF weighting.
- Reads `src/Docs/KayaKalp_WhatsApp_FAQ_RAG.xlsx` at startup.
- Searches question, category, and answer fields.
- Medical-category answers automatically get the doctor disclaimer appended.

---

## Setup

### 1. Prerequisites

- Python 3.10+
- A [Meta WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api/) app + phone number
- A [Google Gemini API key](https://aistudio.google.com/apikey) for FAQ embeddings + image/PDF analysis with Gemini 3.1 Flash Lite

### 2. Install

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
copy .env.example .env   # Windows
# fill in WhatsApp keys and the Gemini API key
```

### 4. Prepare the FAQ knowledge base

Place your FAQ xlsx at `src/Docs/KayaKalp_WhatsApp_FAQ_RAG.xlsx`. The file should have columns like:
- `question` — the patient question
- `answer` — the reply text
- `category` — topic category (e.g., "Weight Management", "Pricing")
- `external_id` — unique ID (used for list button callbacks)
- `escalate_to_human` — "Yes" or true to route to a human

### 5. Run

```bash
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
```

### 6. Expose to Meta

Tunnel for local dev: `ngrok http 8000`, or deploy to any host. Register the URL in the WhatsApp Cloud API app's webhook settings:

- **Callback URL:** `https://<your-domain>/webhook/whatsapp`
- **Verify token:** the `WHATSAPP_VERIFY_TOKEN` from your `.env`
- **Subscribe** to the `messages` webhook field.

---

## Environment Variables (`.env`)

| Variable | Purpose | Required |
|---|---|---|
| `WHATSAPP_ACCESS_TOKEN` | WhatsApp Cloud API token | Yes |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta phone-number ID | Yes |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token | Yes |
| `WHATSAPP_WEBHOOK_SECRET` | Webhook signature verification | No |
| `GOOGLE_API_KEY` | Gemini API key (for FAQ embeddings + image/PDF analysis with Gemini 3.1 Flash Lite) | Yes |
| `OPENROUTER_API_KEY` | OpenRouter key (for RAG answer drafting — Gemma 4 31B free) | Yes |
| `GOOGLE_APPS_SCRIPT_URL` | Google Apps Script for booking submission | Optional |
| `ENROLLMENT_SHEET_ID` / `ENROLLMENT_SHEET_GID` | Payments sheet for pre-paid enrollment verification (phone + "Paid" → instant enrollment) | Optional |
| `CASHFREE_APP_ID` / `CASHFREE_SECRET_KEY` | Cashfree API credentials | Optional — enables auto payment verification |
| `CASHFREE_ENV` | `sandbox` or `production` | Optional (default `sandbox`) |
| `CASHFREE_WEBHOOK_SECRET` | Verifies incoming Cashfree webhooks | Optional (falls back to secret key) |

---

## Medical Safety

- **Emergencies** are routed immediately to **+917666320828** — no answers attempted.
- Every response carries the disclaimer: *"Please consult a qualified doctor at Kayakalp for your personal medical decisions."*
- Kaya only answers topics covered by the FAQ knowledge base; otherwise it politely declines or escalates to a human.

---

## Deployment

The service is a stateless FastAPI app. Deploy to Railway / Render / Fly.io / a VPS behind HTTPS (Meta requires HTTPS webhooks). Keep `.env` secrets out of source control.
