/**
 * KayaKalp - Weight Loss Program ENROLLMENT FORM -> WhatsApp bot webhook
 * =====================================================================
 * Paste this into the ENROLLMENT Google Form
 * (https://forms.gle/KFwguS13cwEbrSFn8):
 *   Extensions -> Apps Script -> paste -> Save.
 *
 * When a patient submits the form, this POSTs {phone, name, email} to the
 * bot server, which then sends them the [ Pay Now ] payment button.
 *
 * SETUP (all 3 steps are REQUIRED - the most common reason for
 * "no message received" is skipping step 2 or 3):
 *
 * 1. Set WEBHOOK_URL below to your PUBLIC APP_BASE_URL - the same URL that
 *    receives WhatsApp webhooks (e.g. https://xyz.up.railway.app or your
 *    ngrok domain). It must NOT be localhost - Apps Script runs on Google's
 *    servers and cannot reach your machine.
 *    Quick check: open https://YOUR-DOMAIN/health in a browser first. If it
 *    does not load, no Apps Script will ever reach it either.
 *
 * 2. Add the trigger: Triggers -> Add Trigger ->
 *      function: onFormSubmit | deployment: Head
 *      event source: From form | event type: On form submit
 *    -> Save. (Apps Script asks for permission on first run - approve it.)
 *
 * 3. Make sure the form has a "WhatsApp number" / "Phone number" question,
 *    and patients enter the SAME number they chat on. That is how the bot
 *    matches the submission to their chat.
 *
 * DEBUGGING a silent failure:
 *   Submit a test response yourself, then open the script editor:
 *   Executions (clock icon, left sidebar). Every run is listed with its log
 *   output (View -> Logs or the execution detail). You will see exactly how
 *   far it got: question dump -> payload -> webhook HTTP status.
 *   - No run listed at all          -> trigger missing (step 2)
 *   - Webhook HTTP 000 / exception  -> wrong/unreachable WEBHOOK_URL (step 1)
 *   - HTTP 404                      -> path must end in /webhook/form
 *   - HTTP 200 but patient got nothing -> see "delivery failed" note below;
 *     the doctor hotline gets an alert to follow up manually.
 */

const WEBHOOK_URL = 'https://YOUR-DOMAIN/webhook/form';

function onFormSubmit(e) {
  try {
    const responses = e.response.getItemResponses();

    Logger.log('--- Form submission received ---');
    responses.forEach(function (r, i) {
      Logger.log('Q' + (i + 1) + ' "' + r.getItem().getTitle() + '" => ' + r.getResponse());
    });

    // Match questions by title so re-ordering the form never breaks this.
    // Falls back to the FIRST answer for phone if no titled match is found.
    const payload = {
      phone: pick(responses, /whatsapp|phone|mobile|contact\s*no|number/) || get(responses, 0),
      name: pick(responses, /full\s*name|^name|patient\s*name|your\s*name/),
      email: pick(responses, /mail/),
    };

    if (!payload.phone && !payload.email) {
      Logger.log('ERROR: could not find a phone/email answer. Check question titles above.');
      return;
    }

    Logger.log('Posting to bot: ' + JSON.stringify(payload));
    const res = UrlFetchApp.fetch(WEBHOOK_URL, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });

    const code = res.getResponseCode();
    Logger.log('Webhook HTTP status: ' + code);
    if (code !== 200) {
      Logger.log('Webhook response body: ' + res.getContentText());
    }
  } catch (err) {
    // Usually an unreachable URL (DNS refused = WEBHOOK_URL not public).
    Logger.log('FATAL error sending webhook: ' + err);
  }
}

/** First response whose question title matches the regex. */
function pick(responses, titleRegex) {
  for (let i = 0; i < responses.length; i++) {
    const title = String(responses[i].getItem().getTitle() || '').toLowerCase();
    if (titleRegex.test(title)) return String(responses[i].getResponse() || '').trim();
  }
  return '';
}

/** Response at a positional index (fallback). */
function get(responses, index) {
  if (index < responses.length) return String(responses[index].getResponse() || '').trim();
  return '';
}
