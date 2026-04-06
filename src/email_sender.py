"""
email_sender.py
Core email sending module using Microsoft Graph API (OAuth2).
Reads from CSV, sends templated emails, updates tracking fields.
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

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = Path(os.environ.get("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"email_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ── Microsoft Graph helpers ───────────────────────────────────────────────────

def get_access_token() -> str:
    """
    Acquire an OAuth2 access token via the client-credentials flow.
    Required env vars:
        AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET
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
) -> bool:
    """
    Send one email through the Microsoft Graph /sendMail endpoint.
    Returns True on success, False on failure.
    """
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
        log.info("✓ Sent to %s", to_address)
        return True

    log.error(
        "✗ Failed to send to %s — HTTP %s: %s",
        to_address, resp.status_code, resp.text[:300],
    )
    return False


# ── Main run ─────────────────────────────────────────────────────────────────

def run(
    csv_path: str,
    template_path: str,
    sender_address: str,
    email_subject: str,
    daily_limit: int = 25,
    interval_seconds: int = 300,   # 5 minutes between sends
) -> None:
    """
    Main entry point called by the GitHub Actions workflow.

    Args:
        csv_path:         Path to the contact CSV file.
        template_path:    Path to the HTML email template.
        sender_address:   The Outlook address to send FROM.
        email_subject:    Subject line for every email in this run.
        daily_limit:      Maximum emails to send in one run.
        interval_seconds: Pause between sends (seconds).
    """
    log.info("=== Email Automation Run Starting ===")
    log.info("CSV: %s | Template: %s | Sender: %s", csv_path, template_path, sender_address)

    csv_mgr  = CSVManager(csv_path)
    tmpl_eng = TemplateEngine(template_path)
    token    = get_access_token()

    contacts = csv_mgr.get_due_contacts(limit=daily_limit)
    log.info("%d contact(s) due for email today.", len(contacts))

    sent_count = 0

    for i, contact in enumerate(contacts):
        email_addr   = contact["email_address"]
        first_name   = contact["first_name"]
        company_name = contact["company_name"]
        row_index    = contact["_row_index"]

        # Build the personalised email body
        html_body = tmpl_eng.render(
            first_name=first_name,
            company_name=company_name,
        )

        success = send_email_via_graph(
            token=token,
            sender_address=sender_address,
            to_address=email_addr,
            subject=email_subject,
            html_body=html_body,
        )

        if success:
            csv_mgr.mark_sent(row_index)
            sent_count += 1
        else:
            csv_mgr.flag_failure(row_index)

        # Wait between sends (skip pause after the last one)
        if i < len(contacts) - 1:
            log.info("Waiting %ds before next send…", interval_seconds)
            time.sleep(interval_seconds)

    csv_mgr.save()
    log.info("=== Run complete — %d/%d sent ===", sent_count, len(contacts))


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Send outreach emails via Microsoft Graph.")
    parser.add_argument("--csv",      required=True,  help="Path to contact CSV")
    parser.add_argument("--template", required=True,  help="Path to HTML template")
    parser.add_argument("--sender",   required=True,  help="From address (Outlook)")
    parser.add_argument("--subject",  required=True,  help="Email subject line")
    parser.add_argument("--limit",    type=int, default=25, help="Max emails per run")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between sends")
    args = parser.parse_args()

    run(
        csv_path=args.csv,
        template_path=args.template,
        sender_address=args.sender,
        email_subject=args.subject,
        daily_limit=args.limit,
        interval_seconds=args.interval,
    )