"""Nexus AI — Browser package (all improvements included)."""
from src.browser.selector_healer import selector_healer
from src.browser.trust_scorer import source_trust_scorer
from src.browser.site_registry_v2 import select_sources, REGISTRY
from src.browser.screenshot_diff import screenshot_differ

__all__ = [
    "selector_healer", "source_trust_scorer",
    "select_sources", "REGISTRY", "screenshot_differ",
]
