"""Stable JSON, Markdown and self-contained HTML reports."""
from .html import render_audit_html
from .json_report import dumps_json, write_json
from .markdown import render_audit_markdown
__all__ = ["dumps_json", "render_audit_html", "render_audit_markdown", "write_json"]
