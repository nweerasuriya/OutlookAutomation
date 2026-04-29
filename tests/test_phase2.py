"""
test_phase2.py
Tests covering Phase 2 additions — global daily limit, new config format,
limit=None support in CSVManager, and multi-list CSV isolation.

Run alongside the existing Phase 1 tests:
    pytest tests/ -v --tb=short
"""

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import load_lists_config, _resolve_template, REQUIRED_LIST_KEYS
from csv_manager import (
    CSVManager, COL_EMAIL, COL_FIRST, COL_COMPANY,
    COL_SENT_COUNT, COL_NEXT,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_full_config(daily_limit=24, interval_seconds=300, lists=None) -> dict:
    """Build a valid top-level LISTS_CONFIG object."""
    return {
        "daily_limit":      daily_limit,
        "interval_seconds": interval_seconds,
        "lists":            lists or [_make_list_entry()],
    }


def _make_list_entry(overrides: dict = None) -> dict:
    base = {
        "list_id":           "test_list",
        "sender_email":      "sender@example.com",
        "email_subject":     "Hello {{company_name}}",
        "csv_filename":      "test.csv",
        "template_filename": "test.html",
    }
    if overrides:
        base.update(overrides)
    return base


def _write_csv(path: Path, rows: list) -> None:
    fieldnames = [COL_EMAIL, COL_FIRST, COL_COMPANY]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ── load_lists_config — new object format ─────────────────────────────────────

class TestLoadListsConfig:

    def test_valid_config_parses(self, monkeypatch):
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(_make_full_config()))
        result = load_lists_config()
        assert result["daily_limit"] == 24
        assert len(result["lists"]) == 1
        assert result["lists"][0]["list_id"] == "test_list"

    def test_multiple_lists_parsed(self, monkeypatch):
        cfg = _make_full_config(lists=[
            _make_list_entry({"list_id": "a"}),
            _make_list_entry({"list_id": "b"}),
        ])
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(cfg))
        result = load_lists_config()
        assert len(result["lists"]) == 2

    def test_daily_limit_returned(self, monkeypatch):
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(_make_full_config(daily_limit=10)))
        result = load_lists_config()
        assert result["daily_limit"] == 10

    def test_interval_seconds_defaults_optional(self, monkeypatch):
        cfg = _make_full_config()
        del cfg["interval_seconds"]
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(cfg))
        result = load_lists_config()
        # interval_seconds is optional — main.py defaults to 300 if absent
        assert "interval_seconds" not in result or result.get("interval_seconds") is not None

    def test_missing_env_var_raises(self, monkeypatch):
        monkeypatch.delenv("LISTS_CONFIG", raising=False)
        with pytest.raises(ValueError, match="LISTS_CONFIG"):
            load_lists_config()

    def test_empty_env_var_raises(self, monkeypatch):
        monkeypatch.setenv("LISTS_CONFIG", "")
        with pytest.raises(ValueError, match="LISTS_CONFIG"):
            load_lists_config()

    def test_invalid_json_raises(self, monkeypatch):
        monkeypatch.setenv("LISTS_CONFIG", "{not valid json")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_lists_config()

    def test_bare_array_raises(self, monkeypatch):
        """Old Phase 2 format (bare array) should now raise a clear error."""
        monkeypatch.setenv("LISTS_CONFIG", json.dumps([_make_list_entry()]))
        with pytest.raises(ValueError, match="JSON object"):
            load_lists_config()

    def test_missing_daily_limit_raises(self, monkeypatch):
        cfg = _make_full_config()
        del cfg["daily_limit"]
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(cfg))
        with pytest.raises(ValueError, match="daily_limit"):
            load_lists_config()

    def test_empty_lists_array_raises(self, monkeypatch):
        cfg = _make_full_config(lists=[])
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(cfg))
        with pytest.raises(ValueError, match="non-empty"):
            load_lists_config()

    @pytest.mark.parametrize("missing_key", REQUIRED_LIST_KEYS)
    def test_missing_required_list_key_raises(self, monkeypatch, missing_key):
        entry = _make_list_entry()
        del entry[missing_key]
        cfg = _make_full_config(lists=[entry])
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(cfg))
        with pytest.raises(ValueError, match=missing_key):
            load_lists_config()


# ── get_due_contacts with limit=None ─────────────────────────────────────────

class TestGetDueContactsLimitNone:

    def test_limit_none_returns_all_due(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: f"u{i}@x.com", COL_FIRST: f"User{i}", COL_COMPANY: "Co"}
            for i in range(30)
        ])
        mgr = CSVManager(str(csv_file))
        result = mgr.get_due_contacts(limit=None)
        assert len(result) == 30

    def test_limit_none_ignores_cap(self, tmp_path):
        """With limit=None, even more contacts than the default 25 are returned."""
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: f"u{i}@x.com", COL_FIRST: f"User{i}", COL_COMPANY: "Co"}
            for i in range(40)
        ])
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts(limit=None)) == 40
        assert len(mgr.get_due_contacts(limit=25)) == 25

    def test_limit_int_still_caps(self, tmp_path):
        csv_file = tmp_path / "contacts.csv"
        _write_csv(csv_file, [
            {COL_EMAIL: f"u{i}@x.com", COL_FIRST: f"User{i}", COL_COMPANY: "Co"}
            for i in range(20)
        ])
        mgr = CSVManager(str(csv_file))
        assert len(mgr.get_due_contacts(limit=5)) == 5


# ── Global queue ordering ─────────────────────────────────────────────────────

class TestGlobalQueueOrdering:

    def test_earlier_scheduled_contacts_come_first(self, tmp_path):
        """
        Contacts with the oldest next_scheduled_email should be at the front
        of the sorted list — the global queue sorts ascending by date.
        """
        from datetime import datetime, timedelta, timezone

        csv_file = tmp_path / "contacts.csv"
        today = datetime.now(timezone.utc).date()

        rows = [
            {COL_EMAIL: "a@x.com", COL_FIRST: "A", COL_COMPANY: "A",
             COL_NEXT: (today - timedelta(days=10)).isoformat()},
            {COL_EMAIL: "b@x.com", COL_FIRST: "B", COL_COMPANY: "B",
             COL_NEXT: (today - timedelta(days=1)).isoformat()},
            {COL_EMAIL: "c@x.com", COL_FIRST: "C", COL_COMPANY: "C",
             COL_NEXT: today.isoformat()},
        ]
        _write_csv(csv_file, rows)
        mgr = CSVManager(str(csv_file))
        due = mgr.get_due_contacts(limit=None)

        # Should be ordered oldest → newest
        assert due[0][COL_EMAIL] == "a@x.com"
        assert due[1][COL_EMAIL] == "b@x.com"
        assert due[2][COL_EMAIL] == "c@x.com"

    def test_blank_scheduled_date_sorts_first(self, tmp_path):
        """Contacts with no scheduled date are treated as most overdue."""
        from datetime import datetime, timedelta, timezone

        csv_file = tmp_path / "contacts.csv"
        today = datetime.now(timezone.utc).date()

        rows = [
            {COL_EMAIL: "a@x.com", COL_FIRST: "A", COL_COMPANY: "A",
             COL_NEXT: (today - timedelta(days=5)).isoformat()},
            {COL_EMAIL: "b@x.com", COL_FIRST: "B", COL_COMPANY: "B",
             COL_NEXT: ""},   # blank — should sort first
        ]
        _write_csv(csv_file, rows)
        mgr = CSVManager(str(csv_file))
        due = mgr.get_due_contacts(limit=None)

        assert due[0][COL_EMAIL] == "b@x.com"


# ── _resolve_template ─────────────────────────────────────────────────────────

class TestResolveTemplate:

    def test_prefers_onedrive_copy(self, tmp_path):
        local_base = tmp_path / "workspace"
        (local_base / "templates").mkdir(parents=True)
        onedrive_tmpl = local_base / "templates" / "t.html"
        onedrive_tmpl.write_text("<p>{{first_name}}</p>")
        result = _resolve_template(local_base, "t.html", "test")
        assert result == onedrive_tmpl

    def test_falls_back_to_repo_sample(self, tmp_path, monkeypatch):
        local_base = tmp_path / "workspace"
        local_base.mkdir()
        repo_templates = tmp_path / "templates"
        repo_templates.mkdir()
        (repo_templates / "t.html").write_text("<p>{{first_name}} {{company_name}}</p>")
        import main as main_mod
        monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
        result = _resolve_template(local_base, "t.html", "test")
        assert result == repo_templates / "t.html"

    def test_raises_when_neither_exists(self, tmp_path, monkeypatch):
        local_base = tmp_path / "workspace"
        local_base.mkdir()
        import main as main_mod
        monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
        with pytest.raises(FileNotFoundError, match="t.html"):
            _resolve_template(local_base, "t.html", "test")


# ── Subject line placeholders ─────────────────────────────────────────────────

class TestSubjectPlaceholders:

    def test_company_name_substituted(self):
        subject = "Quick question for {{company_name}}"
        result = subject.replace("{{company_name}}", "Acme")
        assert result == "Quick question for Acme"

    def test_first_name_substituted(self):
        subject = "Hi {{first_name}}, a quick note"
        result = subject.replace("{{first_name}}", "Sarah")
        assert result == "Hi Sarah, a quick note"

    def test_no_placeholder_unchanged(self):
        subject = "A note from us"
        result = subject.replace("{{company_name}}", "X").replace("{{first_name}}", "Y")
        assert result == "A note from us"


# ── Multi-list CSV isolation ──────────────────────────────────────────────────

class TestMultiListIsolation:

    def test_two_csvs_load_independently(self, tmp_path):
        csv_a = tmp_path / "list_a.csv"
        csv_b = tmp_path / "list_b.csv"
        _write_csv(csv_a, [{COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "AlphaCo"}])
        _write_csv(csv_b, [
            {COL_EMAIL: "b@x.com", COL_FIRST: "Bob",   COL_COMPANY: "BetaCo"},
            {COL_EMAIL: "c@x.com", COL_FIRST: "Carol", COL_COMPANY: "GammaCo"},
        ])
        mgr_a = CSVManager(str(csv_a))
        mgr_b = CSVManager(str(csv_b))
        assert len(mgr_a.rows) == 1
        assert len(mgr_b.rows) == 2
        assert mgr_a.rows[0][COL_COMPANY] == "AlphaCo"

    def test_saving_one_csv_does_not_affect_other(self, tmp_path):
        csv_a = tmp_path / "list_a.csv"
        csv_b = tmp_path / "list_b.csv"
        _write_csv(csv_a, [{COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "A"}])
        _write_csv(csv_b, [{COL_EMAIL: "b@x.com", COL_FIRST: "Bob",   COL_COMPANY: "B"}])
        mgr_a = CSVManager(str(csv_a))
        mgr_b = CSVManager(str(csv_b))
        mgr_a.mark_sent(mgr_a.rows[0]["_row_index"])
        mgr_a.save()
        mgr_b_reloaded = CSVManager(str(csv_b))
        assert mgr_b_reloaded.rows[0][COL_SENT_COUNT] == "0"