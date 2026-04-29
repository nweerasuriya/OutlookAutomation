"""
main.py  —  Phase 2 (global daily limit)
Orchestrates multi-list, multi-account email outreach with a single
daily send cap shared across all lists.

How it works
------------
All due contacts from every list are collected, merged, and sorted globally
by next_scheduled_email (earliest first, blanks first). The global daily_limit
is then applied to that combined pool — so the most overdue contacts across
all industries are prioritised, regardless of which list they come from.

LISTS_CONFIG format
-------------------
Note the top-level object wrapper (not a bare array) so daily_limit can
sit alongside the lists:

    {
      "daily_limit": 24,
      "interval_seconds": 300,
      "lists": [
        {
          "list_id":           "industry_a",
          "sender_email":      "alice@company.com",
          "email_subject":     "Quick question for {{company_name}}",
          "csv_filename":      "industry_a.csv",
          "template_filename": "industry_a.html"
        },
        {
          "list_id":           "industry_b",
          "sender_email":      "bob@company.com",
          "email_subject":     "Reaching out to {{company_name}}",
          "csv_filename":      "industry_b.csv",
          "template_filename": "industry_b.html"
        }
      ]
    }

Required GitHub secrets
-----------------------
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET
    ONEDRIVE_USER_ID        — UPN of the OneDrive account
    ONEDRIVE_REMOTE_BASE    — top-level OneDrive folder, e.g. "email-automation"
    LISTS_CONFIG            — JSON object as shown above

Optional
--------
    LOG_DIR                 — default "logs"
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onedrive_sync import sync_down, sync_up
from email_sender import get_access_token, send_email_via_graph
from template_engine import TemplateEngine
from csv_manager import CSVManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(list_id)s]  %(message)s",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent

# Keys required in every list entry
REQUIRED_LIST_KEYS = [
    "list_id", "sender_email", "email_subject",
    "csv_filename", "template_filename",
]


# ── Config helpers ────────────────────────────────────────────────────────────

def load_lists_config() -> dict:
    """
    Parse LISTS_CONFIG from the environment.
    Returns a dict with keys: daily_limit, interval_seconds, lists.
    """
    raw = os.environ.get("LISTS_CONFIG", "")
    if not raw:
        raise ValueError(
            "LISTS_CONFIG environment variable is missing or empty. "
            "Set it as a GitHub secret containing a JSON object with "
            "'daily_limit', 'interval_seconds', and 'lists'."
        )
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LISTS_CONFIG is not valid JSON: {exc}") from exc

    if not isinstance(config, dict):
        raise ValueError(
            "LISTS_CONFIG must be a JSON object with a 'lists' key, "
            "not a bare array. See the docstring for the expected format."
        )

    if "lists" not in config or not isinstance(config["lists"], list) or len(config["lists"]) == 0:
        raise ValueError("LISTS_CONFIG must contain a non-empty 'lists' array.")

    if "daily_limit" not in config:
        raise ValueError("LISTS_CONFIG must contain a top-level 'daily_limit' value.")

    for i, cfg in enumerate(config["lists"]):
        missing = [k for k in REQUIRED_LIST_KEYS if k not in cfg]
        if missing:
            raise ValueError(f"List config #{i} is missing keys: {missing}")

    return config


def _resolve_template(local_base: Path, filename: str, list_id: str) -> Path:
    """
    Prefer OneDrive-synced template; fall back to repo sample for local testing.
    """
    onedrive_copy = local_base / "templates" / filename
    repo_sample   = REPO_ROOT / "templates" / filename

    if onedrive_copy.exists():
        return onedrive_copy

    if repo_sample.exists():
        log.warning(
            "OneDrive template not found for list '%s' — using repo sample: %s",
            list_id, repo_sample,
            extra={"list_id": list_id},
        )
        return repo_sample

    raise FileNotFoundError(
        f"[{list_id}] Template '{filename}' not found in either:\n"
        f"  {onedrive_copy}  (OneDrive)\n"
        f"  {repo_sample}  (repo sample)"
    )


# ── Single-contact send ───────────────────────────────────────────────────────

def send_one(
    token: str,
    contact: dict,
    cfg: dict,
    tmpl_eng: TemplateEngine,
    mgr: CSVManager,
) -> bool:
    """
    Render and send one email for a single contact.
    Updates the CSV manager row on success or failure.
    Returns True if sent successfully.
    """
    list_id      = cfg["list_id"]
    extra        = {"list_id": list_id}
    first_name   = contact["first_name"]
    company_name = contact["company_name"]
    email_addr   = contact["email_address"]
    row_index    = contact["_row_index"]

    rendered_subject = cfg["email_subject"] \
        .replace("{{first_name}}", first_name) \
        .replace("{{company_name}}", company_name)

    html_body = tmpl_eng.render(
        first_name=first_name,
        company_name=company_name,
    )

    success = send_email_via_graph(
        token=token,
        sender_address=cfg["sender_email"],
        to_address=email_addr,
        subject=rendered_subject,
        html_body=html_body,
        list_id=list_id,
    )

    if success:
        mgr.mark_sent(row_index)
    else:
        mgr.flag_failure(row_index)

    return success


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    remote_base = os.environ["ONEDRIVE_REMOTE_BASE"]
    local_base  = Path("workspace")
    sys_extra   = {"list_id": "system"}

    log.info("── Phase 2 run starting ──", extra=sys_extra)

    # Step 1 — Sync everything down once
    log.info("Step 1: OneDrive sync (download)", extra=sys_extra)
    sync_down(remote_base, str(local_base))

    # Step 2 — Load config
    config           = load_lists_config()
    daily_limit      = int(config["daily_limit"])
    interval_seconds = int(config.get("interval_seconds", 300))
    lists            = config["lists"]

    log.info(
        "Loaded %d list(s) | global daily limit: %d | interval: %ds",
        len(lists), daily_limit, interval_seconds,
        extra=sys_extra,
    )

    # Step 3 — Load all CSVs and collect every due contact across all lists
    managers: dict[str, dict] = {}   # list_id -> {mgr, cfg, tmpl_eng}

    for cfg in lists:
        list_id  = cfg["list_id"]
        csv_path = local_base / "csv" / cfg["csv_filename"]
        tmpl_path = _resolve_template(local_base, cfg["template_filename"], list_id)

        managers[list_id] = {
            "mgr":      CSVManager(str(csv_path)),
            "cfg":      cfg,
            "tmpl_eng": TemplateEngine(str(tmpl_path)),
        }

    # Collect all due contacts with no per-list cap (limit=None)
    all_due: list[dict] = []
    for list_id, entry in managers.items():
        contacts = entry["mgr"].get_due_contacts(limit=None)
        for contact in contacts:
            all_due.append({**contact, "_list_id": list_id})
        log.info(
            "%d due contact(s) in list '%s'",
            len(contacts), list_id,
            extra={"list_id": list_id},
        )

    # Sort globally by next_scheduled_email — earliest first, blanks first
    all_due.sort(key=lambda r: r.get("next_scheduled_email") or "")

    # Apply global daily cap
    queue = all_due[:daily_limit]
    log.info(
        "Global queue: %d contact(s) selected from %d total due (limit %d)",
        len(queue), len(all_due), daily_limit,
        extra=sys_extra,
    )

    # Step 4 — Send
    log.info("Step 4: Sending emails", extra=sys_extra)
    token      = get_access_token()
    sent_count = 0

    for i, contact in enumerate(queue):
        list_id = contact["_list_id"]
        entry   = managers[list_id]

        success = send_one(
            token=token,
            contact=contact,
            cfg=entry["cfg"],
            tmpl_eng=entry["tmpl_eng"],
            mgr=entry["mgr"],
        )
        if success:
            sent_count += 1

        if i < len(queue) - 1:
            log.info("Waiting %ds…", interval_seconds, extra={"list_id": list_id})
            time.sleep(interval_seconds)

    # Step 5 — Save all CSVs (only those that were touched)
    for list_id, entry in managers.items():
        entry["mgr"].save()

    # Step 6 — Sync everything back up
    log.info("Step 6: OneDrive sync (upload)", extra=sys_extra)
    sync_up(remote_base, str(local_base))

    log.info(
        "── Run complete: %d/%d sent ──",
        sent_count, len(queue),
        extra=sys_extra,
    )


if __name__ == "__main__":
    main()