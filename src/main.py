"""
main.py
Orchestrates the full Phase 1 workflow:
  1. Sync down from OneDrive
  2. Run email sends
  3. Sync updated CSV + logs back up to OneDrive

Required environment variables (set as GitHub Actions secrets):
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET
    ONEDRIVE_USER_ID       — UPN of the OneDrive owner
    SENDER_EMAIL           — Outlook address to send from
    EMAIL_SUBJECT          — Subject line for this list
    ONEDRIVE_REMOTE_BASE   — Remote folder path, e.g. "email-automation"
    CSV_FILENAME           — e.g. "contacts_list1.csv"
    TEMPLATE_FILENAME      — e.g. "outreach_template.html"

Optional:
    DAILY_LIMIT            — default 25
    INTERVAL_SECONDS       — default 300 (5 min)
    LOG_DIR                — default "logs"
"""

import logging
import os
import sys
from pathlib import Path

# Ensure src/ is on the path when run from repo root
sys.path.insert(0, str(Path(__file__).parent))

from onedrive_sync import sync_down, sync_up
from email_sender import run as send_emails

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)


def main() -> None:
    # ── Config from env ───────────────────────────────────────────────────────
    remote_base       = os.environ["ONEDRIVE_REMOTE_BASE"]
    csv_filename      = os.environ["CSV_FILENAME"]
    template_filename = os.environ["TEMPLATE_FILENAME"]
    sender_email      = os.environ["SENDER_EMAIL"]
    email_subject     = os.environ["EMAIL_SUBJECT"]
    daily_limit       = int(os.environ.get("DAILY_LIMIT", "25"))
    interval_seconds  = int(os.environ.get("INTERVAL_SECONDS", "300"))

    local_base        = Path("workspace")
    csv_path          = local_base / "csv" / csv_filename
    template_path     = local_base / "templates" / template_filename
    log_dir           = Path(os.environ.get("LOG_DIR", "logs"))

    # Ensure log directory exists
    log_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Sync down ─────────────────────────────────────────────────────
    log.info("── Step 1: OneDrive sync (download) ──")
    sync_down(remote_base, str(local_base))

    # ── Step 2: Send emails ───────────────────────────────────────────────────
    log.info("── Step 2: Sending emails ──")
    send_emails(
        csv_path=str(csv_path),
        template_path=str(template_path),
        sender_address=sender_email,
        email_subject=email_subject,
        daily_limit=daily_limit,
        interval_seconds=interval_seconds,
    )

    # ── Step 3: Sync up ───────────────────────────────────────────────────────
    log.info("── Step 3: OneDrive sync (upload) ──")
    sync_up(remote_base, str(local_base))

    log.info("Workflow complete.")


if __name__ == "__main__":
    main()