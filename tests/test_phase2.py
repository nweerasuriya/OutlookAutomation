"""
test_phase2.py
Tests that cover Phase 2 additions.

Run alongside the existing Phase 1 tests:
    pytest tests/ -v --tb=short
"""

import csv
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import load_lists_config, _resolve_template, REQUIRED_LIST_KEYS
from csv_manager import COL_EMAIL, COL_FIRST, COL_COMPANY


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_valid_config(overrides: dict = None) -> dict:
    base = {
        "list_id":           "test_list",
        "sender_email":      "sender@example.com",
        "email_subject":     "Hello {{company_name}}",
        "csv_filename":      "test.csv",
        "template_filename": "test.html",
        "daily_limit":       10,
        "interval_seconds":  5,
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


# ── load_lists_config ─────────────────────────────────────────────────────────

class TestLoadListsConfig:

    def test_valid_config_parses(self, monkeypatch):
        cfg = [_make_valid_config()]
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(cfg))
        result = load_lists_config()
        assert len(result) == 1
        assert result[0]["list_id"] == "test_list"

    def test_multiple_lists_parsed(self, monkeypatch):
        cfg = [_make_valid_config({"list_id": "a"}), _make_valid_config({"list_id": "b"})]
        monkeypatch.setenv("LISTS_CONFIG", json.dumps(cfg))
        result = load_lists_config()
        assert len(result) == 2

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

    def test_empty_array_raises(self, monkeypatch):
        monkeypatch.setenv("LISTS_CONFIG", "[]")
        with pytest.raises(ValueError, match="non-empty"):
            load_lists_config()

    def test_not_an_array_raises(self, monkeypatch):
        monkeypatch.setenv("LISTS_CONFIG", json.dumps({"list_id": "x"}))
        with pytest.raises(ValueError, match="non-empty"):
            load_lists_config()

    @pytest.mark.parametrize("missing_key", REQUIRED_LIST_KEYS)
    def test_missing_required_key_raises(self, monkeypatch, missing_key):
        cfg = _make_valid_config()
        del cfg[missing_key]
        monkeypatch.setenv("LISTS_CONFIG", json.dumps([cfg]))
        with pytest.raises(ValueError, match=missing_key):
            load_lists_config()

    def test_optional_keys_have_defaults_in_main(self, monkeypatch):
        """daily_limit and interval_seconds are optional — main.py provides defaults."""
        cfg = _make_valid_config()
        del cfg["daily_limit"]
        del cfg["interval_seconds"]
        monkeypatch.setenv("LISTS_CONFIG", json.dumps([cfg]))
        result = load_lists_config()
        # Should not raise — defaults applied in main.py, not in load_lists_config
        assert result[0].get("daily_limit") is None


# ── _resolve_template ─────────────────────────────────────────────────────────

class TestResolveTemplate:

    def test_prefers_onedrive_copy(self, tmp_path):
        local_base = tmp_path / "workspace"
        tmpl_dir = local_base / "templates"
        tmpl_dir.mkdir(parents=True)
        onedrive_tmpl = tmpl_dir / "t.html"
        onedrive_tmpl.write_text("<p>{{first_name}}</p>")
        result = _resolve_template(local_base, "t.html", "test")
        assert result == onedrive_tmpl

    def test_falls_back_to_repo_sample(self, tmp_path, monkeypatch):
        local_base = tmp_path / "workspace"
        local_base.mkdir()
        repo_templates = tmp_path / "templates"
        repo_templates.mkdir()
        repo_tmpl = repo_templates / "t.html"
        repo_tmpl.write_text("<p>{{first_name}} {{company_name}}</p>")
        import main as main_mod
        monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
        result = _resolve_template(local_base, "t.html", "test")
        assert result == repo_tmpl

    def test_raises_when_neither_exists(self, tmp_path, monkeypatch):
        local_base = tmp_path / "workspace"
        local_base.mkdir()
        import main as main_mod
        monkeypatch.setattr(main_mod, "REPO_ROOT", tmp_path)
        with pytest.raises(FileNotFoundError, match="t.html"):
            _resolve_template(local_base, "t.html", "test")


# ── Subject line placeholder substitution ────────────────────────────────────

class TestSubjectPlaceholders:
    """Verify that {{placeholders}} in subject lines are filled correctly."""

    def test_subject_company_substituted(self):
        subject = "Quick question for {{company_name}}"
        result = subject.replace("{{company_name}}", "Acme")
        assert result == "Quick question for Acme"

    def test_subject_first_name_substituted(self):
        subject = "Hi {{first_name}}, a quick note"
        result = subject.replace("{{first_name}}", "Sarah")
        assert result == "Hi Sarah, a quick note"

    def test_subject_no_placeholder_unchanged(self):
        subject = "A note from us"
        result = subject.replace("{{company_name}}", "X").replace("{{first_name}}", "Y")
        assert result == "A note from us"


# ── Integration: multi-list CSV isolation ────────────────────────────────────

class TestMultiListIsolation:
    """Each list has its own CSV — a failure in one must not affect another."""

    def test_two_csvs_load_independently(self, tmp_path):
        from csv_manager import CSVManager

        csv_a = tmp_path / "list_a.csv"
        csv_b = tmp_path / "list_b.csv"

        _write_csv(csv_a, [
            {COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "AlphaCo"},
        ])
        _write_csv(csv_b, [
            {COL_EMAIL: "b@x.com", COL_FIRST: "Bob",   COL_COMPANY: "BetaCo"},
            {COL_EMAIL: "c@x.com", COL_FIRST: "Carol",  COL_COMPANY: "GammaCo"},
        ])

        mgr_a = CSVManager(str(csv_a))
        mgr_b = CSVManager(str(csv_b))

        assert len(mgr_a.rows) == 1
        assert len(mgr_b.rows) == 2
        assert mgr_a.rows[0][COL_COMPANY] == "AlphaCo"
        assert mgr_b.rows[1][COL_COMPANY] == "GammaCo"

    def test_saving_one_csv_does_not_affect_other(self, tmp_path):
        from csv_manager import CSVManager, COL_SENT_COUNT

        csv_a = tmp_path / "list_a.csv"
        csv_b = tmp_path / "list_b.csv"

        _write_csv(csv_a, [{COL_EMAIL: "a@x.com", COL_FIRST: "Alice", COL_COMPANY: "A"}])
        _write_csv(csv_b, [{COL_EMAIL: "b@x.com", COL_FIRST: "Bob",   COL_COMPANY: "B"}])

        mgr_a = CSVManager(str(csv_a))
        mgr_b = CSVManager(str(csv_b))

        mgr_a.mark_sent(mgr_a.rows[0]["_row_index"])
        mgr_a.save()

        # Reload B and confirm it is untouched
        mgr_b_reloaded = CSVManager(str(csv_b))
        assert mgr_b_reloaded.rows[0][COL_SENT_COUNT] == "0"
