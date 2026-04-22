"""
main.py  —  Phase 2
Orchestrates multi-list, multi-account email outreach.

How it works
------------
A single GitHub Actions run processes ALL lists configured in LISTS_CONFIG.
LISTS_CONFIG is a JSON string (stored as a GitHub secret) that defines every
list, its sender, subject, CSV file, and template. Example value:

    [
      {
        "list_id":         "industry_a",
        "sender_email":    "alice@company.com",
        "email_subject":   "Quick question for {{company_name}}",
        "csv_filename":    "industry_a.csv",
        "template_filename": "industry_a.html",
        "daily_limit":     25,
        "interval_seconds": 300
      },
      {
        "list_id":         "industry_b",
        "sender_email":    "bob@company.com",
        "email_subject":   "Reaching out to {{company_name}}",
        "csv_filename":    "industry_b.csv",
        "template_filename": "industry_b.html",
        "daily_limit":     10,
        "interval_seconds": 300
      }
    ]

Required GitHub secrets
-----------------------
    AZURE_TENANT_ID
    AZURE_CLIENT_ID
    AZURE_CLIENT_SECRET
    ONEDRIVE_USER_ID        — UPN of the OneDrive account
    ONEDRIVE_REMOTE_BASE    — top-level OneDrive folder, e.g. "email-automation"
    LISTS_CONFIG            — JSON array as shown above

Optional
--------
    LOG_DIR                 — default "logs"
"""

import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from onedrive_sync import sync_down, sync_up
from email_sender import run as send_emails

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(list_id)s]  %(message)s",
)
log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent

# Keys required in every list config entry
REQUIRED_LIST_KEYS = [
    "list_id", "sender_email", "email_subject",
    "csv_filename", "template_filename",
]


# ── Config helpers ────────────────────────────────────────────────────────────

def load_lists_config() -> list[dict]:
    raw = os.environ.get("LISTS_CONFIG", "")
    if not raw:
        raise ValueError(
            "LISTS_CONFIG environment variable is missing or empty. "
            "Set it as a GitHub secret containing a JSON array of list configs."
        )
    try:
        configs = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LISTS_CONFIG is not valid JSON: {exc}") from exc

    if not isinstance(configs, list) or len(configs) == 0:
        raise ValueError("LISTS_CONFIG must be a non-empty JSON array.")

    for i, cfg in enumerate(configs):
        missing = [k for k in REQUIRED_LIST_KEYS if k not in cfg]
        if missing:
            raise ValueError(f"List config #{i} is missing keys: {missing}")

    return configs


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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    remote_base = os.environ["ONEDRIVE_REMOTE_BASE"]
    local_base  = Path("workspace")

    log.info("── Phase 2 run starting ──", extra={"list_id": "system"})

    # Step 1 — Sync everything down once
    log.info("Step 1: OneDrive sync (download)", extra={"list_id": "system"})
    sync_down(remote_base, str(local_base))

    # Step 2 — Load list configs
    configs = load_lists_config()
    log.info("Loaded %d list(s) from LISTS_CONFIG", len(configs), extra={"list_id": "system"})

    results: list[dict] = []

    # Step 3 — Process each list in sequence
    for cfg in configs:
        list_id   = cfg["list_id"]
        extra     = {"list_id": list_id}
        csv_path  = local_base / "csv" / cfg["csv_filename"]
        tmpl_path = _resolve_template(local_base, cfg["template_filename"], list_id)

        log.info("Processing list: %s | sender: %s", list_id, cfg["sender_email"], extra=extra)

        try:
            send_emails(
                csv_path=str(csv_path),
                template_path=str(tmpl_path),
                sender_address=cfg["sender_email"],
                email_subject=cfg["email_subject"],
                daily_limit=int(cfg.get("daily_limit", 25)),
                interval_seconds=int(cfg.get("interval_seconds", 300)),
                list_id=list_id,
            )
            results.append({"list_id": list_id, "status": "ok"})
        except Exception as exc:
            log.error("List '%s' failed: %s", list_id, exc, extra=extra)
            results.append({"list_id": list_id, "status": "error", "error": str(exc)})
            # Continue with remaining lists — don't abort the whole run

    # Step 4 — Sync everything back up
    log.info("Step 4: OneDrive sync (upload)", extra={"list_id": "system"})
    sync_up(remote_base, str(local_base))

    # Summary
    ok  = [r for r in results if r["status"] == "ok"]
    err = [r for r in results if r["status"] == "error"]
    log.info(
        "── Run complete: %d/%d lists succeeded ──",
        len(ok), len(results),
        extra={"list_id": "system"},
    )
    if err:
        for r in err:
            log.error("  FAILED: %s — %s", r["list_id"], r.get("error"), extra={"list_id": "system"})


if __name__ == "__main__":
    main()
