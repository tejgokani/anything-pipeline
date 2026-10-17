"""Deterministic project-type detection. Reads files only; never executes repo code."""
from .plan import detect, render_text
from .rules import DEFAULT_RULES_PATH, Rules, RulesError, load_rules

__all__ = ["detect", "render_text", "load_rules", "Rules", "RulesError", "DEFAULT_RULES_PATH"]
