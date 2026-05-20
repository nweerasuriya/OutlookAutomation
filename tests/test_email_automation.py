"""
test_email_automation.py
Automated tests for Phase 1.

Run with:  pytest tests/test_email_automation.py -v
"""

import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pandas as pd

import pytest

# Put src/ on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from csv_manager import (
    COL_BOUNCE, COL_FAILED,
    COL_NEXT, COL_LAST_SENT, COL_SENT_COUNT, COL_UNSUB,
    CSVManager,
)
from template_engine import TemplateEngine

COL_EMAIL = "Email"
COL_FIRST = "First Name"
COL_COMPANY = "Company Name"
COL_CITY = "City"
# ── Fixtures ──────────────────────────────────────────────────────────────────

def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        COL_EMAIL, COL_FIRST, COL_COMPANY, COL_CITY, COL_NEXT,
        COL_LAST_SENT, COL_SENT_COUNT, COL_UNSUB, COL_BOUNCE, COL_FAILED,
        "response_count",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            full_row = {f: row.get(f, "") for f in fieldnames}
            writer.writerow(full_row)


def _write_template(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ── CSV Manager Tests ─────────────────────────────────────────────────────────

class TestCSVManager:

    def test_loads_valid_csv(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {"Email": "a@x.com", "First Name": "Alice", "Company Name": "Acme"},
        ])
        print(f"CSV file content: {pd.read_csv(csv_file).columns.tolist()}")
        mgr = CSVManager(str(csv_file))
        assert len(mgr.rows) == 1
        assert mgr.rows[0]["first_name"] == "Alice"

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            CSVManager(str(tmp_path / "nonexistent.csv"))

    def test_raises_on_missing_required_columns(self, tmp_path):
        csv_file = tmp_path / "bad.csv"
        csv_file.write_text("email_address,first_name\na@x.com,Alice\n")
        with pytest.raises(ValueError, match="company_name"):
            CSVManager(str(csv_file))

    def test_due_contacts_no_scheduled_date(self, tmp_path):
        """Contacts with no next_scheduled_email are always due."""
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "Acme", COL_NEXT: ""},
        ])
        mgr = CSVManager(str(csv_file))
        due = mgr.get_due_contacts(limit=10)
        assert len(due) == 1

    def test_due_contacts_past_date(self, tmp_path):
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "Acme", COL_NEXT: yesterday},
        ])
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts()) == 1

    def test_due_contacts_future_date_excluded(self, tmp_path):
        future = (datetime.now(timezone.utc) + timedelta(days=5)).date().isoformat()
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "Acme", COL_NEXT: future},
        ])
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts()) == 0

    def test_unsubscribed_excluded(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "Acme",
             COL_UNSUB: "true", COL_NEXT: ""},
        ])
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts()) == 0

    def test_bounce_flagged_excluded(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "b@x.com", COL_FIRST: "Bob", COL_COMPANY: "Corp",
             COL_BOUNCE: "true", COL_NEXT: ""},
        ])
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts()) == 0

    def test_failed_excluded(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "c@x.com", COL_FIRST: "Carol", COL_COMPANY: "Ltd",
             COL_FAILED: "true", COL_NEXT: ""},
        ])
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts()) == 0

    def test_daily_limit_respected(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        rows = [
            {COL_EMAIL: f"u{i}@x.com", COL_FIRST: f"User{i}", COL_COMPANY: "Co"}
            for i in range(20)
        ]
        _write_csv(csv_file, rows)
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts(limit=5)) == 5

    def test_mark_sent_updates_fields(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "Acme",
             COL_SENT_COUNT: "2"},
        ])
        mgr = CSVManager(str(csv_file))
        row_idx = mgr.rows[0]["_row_index"]
        mgr.mark_sent(row_idx)
        row = mgr.rows[0]
        assert row[COL_SENT_COUNT] == "3"
        assert row[COL_LAST_SENT] != ""
        assert row[COL_NEXT] != ""
        assert row[COL_FAILED] == "false"

    def test_flag_failure_sets_flag(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "Acme"},
        ])
        mgr = CSVManager(str(csv_file))
        row_idx = mgr.rows[0]["_row_index"]
        mgr.flag_failure(row_idx)
        assert mgr.rows[0][COL_FAILED] == "true"

    def test_save_round_trips_correctly(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "Acme",
             COL_SENT_COUNT: "0"},
        ])
        mgr = CSVManager(str(csv_file))
        mgr.mark_sent(mgr.rows[0]["_row_index"])
        mgr.save()

        mgr2 = CSVManager(str(csv_file))
        assert mgr2.rows[0][COL_SENT_COUNT] == "1"
        assert mgr2.rows[0][COL_LAST_SENT] != ""


# ── Template Engine Tests ─────────────────────────────────────────────────────

class TestTemplateEngine:

    def test_renders_basic_variables(self, tmp_path):
        tmpl = tmp_path / "t.html"
        _write_template(tmpl, "<p>Hi {{first_name}}, from {{company_name}}.</p>")
        eng = TemplateEngine(str(tmpl))
        out = eng.render(first_name="Alice", company_name="Acme")
        assert "Alice" in out
        assert "Acme" in out
        assert "{{" not in out

    def test_renders_with_spaces_in_placeholder(self, tmp_path):
        tmpl = tmp_path / "t.html"
        _write_template(tmpl, "<p>Hi {{ first_name }}, of {{ company_name }}.</p>")
        eng = TemplateEngine(str(tmpl))
        out = eng.render(first_name="Bob", company_name="Corp")
        assert "Bob" in out and "Corp" in out

    def test_case_insensitive_placeholder(self, tmp_path):
        tmpl = tmp_path / "t.html"
        _write_template(tmpl, "<p>{{FIRST_NAME}} at {{Company_Name}}</p>")
        eng = TemplateEngine(str(tmpl))
        out = eng.render(first_name="Carol", company_name="Ltd")
        assert "Carol" in out and "Ltd" in out

    def test_unfilled_placeholder_warns(self, tmp_path, caplog):
        tmpl = tmp_path / "t.html"
        _write_template(tmpl, "<p>Hi {{first_name}}, {{company_name}}. {{unknown_var}}</p>")
        eng = TemplateEngine(str(tmpl))
        with caplog.at_level("WARNING"):
            out = eng.render(first_name="Dave", company_name="Co")
        assert "{{unknown_var}}" in out
        assert "unknown_var" in caplog.text

    def test_validate_raises_on_missing_first_name(self, tmp_path):
        tmpl = tmp_path / "t.html"
        _write_template(tmpl, "<p>Hello {{company_name}}</p>")
        eng = TemplateEngine(str(tmpl))
        with pytest.raises(ValueError, match="first_name"):
            eng.validate()

    def test_validate_raises_on_missing_company_name(self, tmp_path):
        tmpl = tmp_path / "t.html"
        _write_template(tmpl, "<p>Hello {{first_name}}</p>")
        eng = TemplateEngine(str(tmpl))
        with pytest.raises(ValueError, match="company_name"):
            eng.validate()

    def test_validate_passes_with_both_placeholders(self, tmp_path):
        tmpl = tmp_path / "t.html"
        _write_template(tmpl, "<p>{{first_name}} at {{company_name}}</p>")
        eng = TemplateEngine(str(tmpl))
        assert eng.validate() is True

    def test_missing_template_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            TemplateEngine(str(tmp_path / "missing.html"))


# ── Integration: CSV + Template ───────────────────────────────────────────────

class TestIntegration:

    def test_email_content_uses_contact_data(self, tmp_path):
        """Verify that rendered email body contains the correct contact's data."""
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: "eve@x.com", COL_FIRST: "Eve", COL_COMPANY: "EveCorps"},
        ])
        tmpl_file = tmp_path / "template.html"
        _write_template(
            tmpl_file,
            "<p>Dear {{first_name}}, we'd love to work with {{company_name}}.</p>"
        )
        mgr = CSVManager(str(csv_file))
        eng = TemplateEngine(str(tmpl_file))

        contact = mgr.get_due_contacts(limit=1)[0]
        html = eng.render(
            first_name=contact["first_name"],
            company_name=contact["company_name"],
        )
        assert "Eve" in html
        assert "EveCorps" in html
        assert "{{" not in html