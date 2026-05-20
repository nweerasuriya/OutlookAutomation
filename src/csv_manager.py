"""
csv_manager.py
Handles all CSV read/write operations, scheduling logic, and monthly backups.

Expected CSV columns (case-insensitive headers are normalised on load):
    email_address, first_name, company_name,
    next_scheduled_email, last_sent_datetime,
    emails_sent_count, response_count,
    unsubscribed, bounce_flagged, send_failed
"""

import csv
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any

log = logging.getLogger(__name__)

# Days between follow-up emails
RESCHEDULE_DAYS = 30

# CSV column names (what we write / expect)
COL_EMAIL      = "Email"
COL_FIRST      = "First Name"
COL_COMPANY    = "Company Name"
COL_NEXT       = "next_scheduled_email"
COL_LAST_SENT  = "last_sent_datetime"
COL_SENT_COUNT = "emails_sent_count"
COL_RESP_COUNT = "response_count"
COL_UNSUB      = "unsubscribed"
COL_BOUNCE     = "bounce_flagged"
COL_FAILED     = "send_failed"
COL_HOLD       = "hold_for_review" 

REQUIRED_COLS = [COL_EMAIL, COL_FIRST, COL_COMPANY]


class CSVManager:
    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)
        self.rows: List[Dict[str, Any]] = []
        self.fieldnames: List[str] = []
        self._load()
        self._maybe_backup()

    # ── Load ──────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"CSV not found: {self.csv_path}")

        with open(self.csv_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            raw_fieldnames = reader.fieldnames or []
            # Normalise header names to lowercase with underscores
            self.fieldnames = [h.strip().lower().replace(" ", "_") for h in raw_fieldnames]
            for i, row in enumerate(reader):
                normalised = {
                    k.strip().lower().replace(" ", "_"): v.strip()
                    for k, v in row.items()
                }
                normalised["_row_index"] = i   # internal tracking key
                self._ensure_tracking_cols(normalised)
                self.rows.append(normalised)

        # Make sure tracking columns exist in fieldnames list
        for col in [COL_NEXT, COL_LAST_SENT, COL_SENT_COUNT,
                    COL_RESP_COUNT, COL_UNSUB, COL_BOUNCE, COL_FAILED, COL_HOLD]:
            if col not in self.fieldnames:
                self.fieldnames.append(col)

        self._validate_required_cols()
        log.info("Loaded %d rows from %s", len(self.rows), self.csv_path)

    def _ensure_tracking_cols(self, row: Dict) -> None:
        defaults = {
            COL_NEXT:       "",
            COL_LAST_SENT:  "",
            COL_SENT_COUNT: "0",
            COL_RESP_COUNT: "0",
            COL_UNSUB:      "false",
            COL_BOUNCE:     "false",
            COL_FAILED:     "false",
            COL_HOLD:       "false",
        }
        for col, default in defaults.items():
            row.setdefault(col, default)

    def _validate_required_cols(self) -> None:
        missing = [c for c in REQUIRED_COLS if c not in self.fieldnames]
        if missing:
            raise ValueError(f"CSV is missing required columns: {missing}")

    # ── Monthly backup ────────────────────────────────────────────────────────

    def _maybe_backup(self) -> None:
        """Create a monthly backup on the 1st of each month."""
        today = datetime.now(timezone.utc)
        if today.day != 1:
            return

        backup_dir = self.csv_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = today.strftime("%Y_%m")
        backup_path = backup_dir / f"{self.csv_path.stem}_{stamp}{self.csv_path.suffix}"

        if not backup_path.exists():
            shutil.copy2(self.csv_path, backup_path)
            log.info("Monthly backup created: %s", backup_path)

    # ── Querying ──────────────────────────────────────────────────────────────

    def get_due_contacts(self, limit: int | None = 25) -> List[Dict]:
        """
        Return due contacts whose next_scheduled_email is on or before today
        and who are not unsubscribed, bounced, or previously failed.
        Sorted earliest-scheduled first.

        Args:
            limit: Maximum contacts to return. Pass None to return all due
                   contacts without a cap — used by the global queue in main.py
                   so the daily limit can be applied across all lists together.
        """
        today = datetime.now(timezone.utc).date()
        due: List[Dict] = []

        for row in self.rows:
            # Skip excluded contacts
            if row.get(COL_UNSUB, "").lower() in ("true", "1", "yes"):
                continue
            if row.get(COL_BOUNCE, "").lower() in ("true", "1", "yes"):
                continue
            # Don't retry ones that failed last time (needs manual review)
            if row.get(COL_FAILED, "").lower() in ("true", "1", "yes"):
                continue
            # Skip ones manually flagged for hold (e.g. pending review)
            if row.get(COL_HOLD,   "").lower() in ("true", "1", "yes"):
                continue

            scheduled_raw = row.get(COL_NEXT, "").strip()

            # If never scheduled, treat as immediately due
            if not scheduled_raw:
                due.append(row)
                continue

            try:
                scheduled_date = datetime.fromisoformat(scheduled_raw).date()
            except ValueError:
                log.warning("Unparseable date '%s' for %s — treating as due",
                            scheduled_raw, row.get(COL_EMAIL))
                due.append(row)
                continue

            if scheduled_date <= today:
                due.append(row)

        # Sort earliest-scheduled first (empty dates sort first)
        due.sort(key=lambda r: r.get(COL_NEXT) or "")

        if limit is None:
            log.info("Found %d due contact(s) (no cap)", len(due))
            return due

        log.info("Found %d due contact(s); capping at %d", len(due), limit)
        return due[:limit]

    # ── Updating ──────────────────────────────────────────────────────────────

    def mark_sent(self, row_index: int, hold_for_review: bool = False) -> None:
        """
        Record a successful send and schedule next contact in 21 days.

        Args:
            row_index:       Internal row identifier.
            hold_for_review: If True, set hold_for_review = true on the row
                             so it won't re-enter the queue until manually
                             cleared. The next_scheduled_email is still written
                             so the date is ready when the hold is lifted.
        """
        row = self._find_row(row_index)
        now       = datetime.now(timezone.utc).isoformat()
        next_date = (
            datetime.now(timezone.utc) + timedelta(days=RESCHEDULE_DAYS)
        ).date().isoformat()

        row[COL_LAST_SENT]  = now
        row[COL_NEXT]       = next_date
        row[COL_SENT_COUNT] = str(int(row.get(COL_SENT_COUNT, "0") or 0) + 1)
        row[COL_FAILED]     = "false"
        row[COL_HOLD]       = "true" if hold_for_review else "false"   # NEW

    def flag_failure(self, row_index: int) -> None:
        """Flag a row as failed so it can be manually reviewed."""
        row = self._find_row(row_index)
        row[COL_FAILED] = "true"
        log.warning("Flagged send failure for %s", row.get(COL_EMAIL))

    def _find_row(self, row_index: int) -> Dict:
        for row in self.rows:
            if row["_row_index"] == row_index:
                return row
        raise KeyError(f"Row index {row_index} not found")

    # ── Save ──────────────────────────────────────────────────────────────────

    def save(self) -> None:
        """Write updated rows back to the CSV."""
        # Strip internal keys before writing
        clean_rows = [
            {k: v for k, v in row.items() if not k.startswith("_")}
            for row in self.rows
        ]
        with open(self.csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=self.fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(clean_rows)
        log.info("CSV saved: %s", self.csv_path)