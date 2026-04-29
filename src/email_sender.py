"""
email_sender.py  —  Phase 2
Core email sending module using Microsoft Graph API (OAuth2).

Changes from Phase 1
--------------------
- `run()` now accepts an optional `list_id` parameter used to tag every log
  line with which list is being processed. This makes multi-list log files
  easy to read and filter.
- `get_access_token()` is unchanged — all lists share the same Azure app
  registration credentials, so one token covers all senders.
- All other logic is identical to Phase 1.
"""

import os
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

import msal
import requests

from csv_manager import CSVManager
from template_engine import TemplateEngine

LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(list_id)s]  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"email_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Microsoft Graph helpers ───────────────────────────────────────────────────

def get_access_token() -> str:
    """
    Acquire an OAuth2 token via client-credentials flow.
    One token covers all sender addresses within the same tenant.
    """
    tenant_id     = os.environ["AZURE_TENANT_ID"]
    client_id     = os.environ["AZURE_CLIENT_ID"]
    client_secret = os.environ["AZURE_CLIENT_SECRET"]

    authority = f"https://login.microsoftonline.com/{tenant_id}"
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=authority,
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        raise RuntimeError(f"Could not obtain token: {result.get('error_description')}")
    return result["access_token"]


def send_email_via_graph(
    token: str,
    sender_address: str,
    to_address: str,
    subject: str,
    html_body: str,
    list_id: str = "unknown",
) -> bool:
    """Send one email. Returns True on success, False on failure."""
    extra = {"list_id": list_id}
    url = f"https://graph.microsoft.com/v1.0/users/{sender_address}/sendMail"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [{"emailAddress": {"address": to_address}}],
        },
        "saveToSentItems": "true",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code == 202:
        log.info("✓ Sent to %s", to_address, extra=extra)
        return True

    log.error(
        "✗ Failed to send to %s — HTTP %s: %s",
        to_address, resp.status_code, resp.text[:300],
        extra=extra,
    )
    return False


# ── Main run ──────────────────────────────────────────────────────────────────

def run(
    csv_path: str,
    template_path: str,
    sender_address: str,
    email_subject: str,
    daily_limit: int = 25,
    interval_seconds: int = 300,
    list_id: str = "default",        # NEW in Phase 2
) -> None:
    """
    Send emails for one list.

    Args:
        csv_path:         Path to the contact CSV.
        template_path:    Path to the HTML template.
        sender_address:   The Outlook address to send FROM.
        email_subject:    Subject line (may include {{placeholders}}).
        daily_limit:      Max emails to send in this run.
        interval_seconds: Pause between sends.
        list_id:          Identifier for this list, used in log tagging.
    """
    extra = {"list_id": list_id}
    log.info(
        "=== Starting list '%s' | CSV: %s | Sender: %s ===",
        list_id, csv_path, sender_address,
        extra=extra,
    )

    csv_mgr  = CSVManager(csv_path)
    tmpl_eng = TemplateEngine(template_path)
    token    = get_access_token()

    contacts = csv_mgr.get_due_contacts(limit=daily_limit)
    log.info("%d contact(s) due.", len(contacts), extra=extra)

    sent_count = 0

    for i, contact in enumerate(contacts):
        email_addr   = contact["email_address"]
        first_name   = contact["first_name"]
        company_name = contact["company_name"]
        row_index    = contact["_row_index"]

        # Subject lines can also use {{placeholders}}
        rendered_subject = email_subject \
            .replace("{{first_name}}", first_name) \
            .replace("{{company_name}}", company_name)

        html_body = tmpl_eng.render(
            first_name=first_name,
            company_name=company_name,
        )

        success = send_email_via_graph(
            token=token,
            sender_address=sender_address,
            to_address=email_addr,
            subject=rendered_subject,
            html_body=html_body,
            list_id=list_id,
        )

        if success:
            csv_mgr.mark_sent(row_index)
            sent_count += 1
        else:
            csv_mgr.flag_failure(row_index)

        if i < len(contacts) - 1:
            log.info("Waiting %ds…", interval_seconds, extra=extra)
            time.sleep(interval_seconds)

    csv_mgr.save()
    log.info(
        "=== List '%s' complete — %d/%d sent ===",
        list_id, sent_count, len(contacts),
        extra=extra,
    )
