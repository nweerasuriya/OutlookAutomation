"""
template_engine.py
Loads an HTML email template and performs variable substitution.

Supported placeholders (case-insensitive, double-brace syntax):
    {{first_name}}    — recipient's first name
    {{company_name}}  — recipient's company
    {{sender_name}}   — configured sender display name (optional)
    {{date}}          — today's date (e.g. April 5, 2026)
"""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


class TemplateEngine:
    def __init__(self, template_path: str):
        self.template_path = Path(template_path)
        self.template_html = self._load()

    def _load(self) -> str:
        if not self.template_path.exists():
            raise FileNotFoundError(f"Template not found: {self.template_path}")
        content = self.template_path.read_text(encoding="utf-8")
        log.info("Template loaded: %s (%d chars)", self.template_path, len(content))
        return content

    def render(
        self,
        first_name: str,
        company_name: str,
        sender_name: str = "",
        **extra_vars,
    ) -> str:
        """
        Return the template with all placeholders replaced.
        Unknown placeholders are left as-is and logged as warnings.
        """
        today = datetime.now(timezone.utc).strftime("%B %-d, %Y")

        variables: dict = {
            "first_name":   first_name.strip(),
            "company_name": company_name.strip(),
            "sender_name":  sender_name.strip(),
            "date":         today,
            **extra_vars,
        }

        html = self.template_html

        for key, value in variables.items():
            # Match {{key}} and {{ key }} (case-insensitive)
            pattern = re.compile(r"\{\{\s*" + re.escape(key) + r"\s*\}\}", re.IGNORECASE)
            html = pattern.sub(value, html)

        # Warn about any remaining unfilled placeholders
        remaining = re.findall(r"\{\{[^}]+\}\}", html)
        if remaining:
            log.warning("Unfilled template placeholders: %s", remaining)

        return html

    def validate(self) -> bool:
        """
        Check the template contains at minimum {{first_name}} and {{company_name}}.
        Returns True if valid, raises ValueError if not.
        """
        required = ["first_name", "company_name"]
        missing = []
        for var in required:
            pattern = re.compile(r"\{\{\s*" + re.escape(var) + r"\s*\}\}", re.IGNORECASE)
            if not pattern.search(self.template_html):
                missing.append(f"{{{{{var}}}}}")
        if missing:
            raise ValueError(f"Template missing required placeholders: {missing}")
        return True