"""Test-only API stand-ins for the security browser tests (built on tests/web/mockapi.py; data is for layout checks only)."""
from __future__ import annotations

import json

from mockapi import MockApi

PAYLOADS = ['<img src=x onerror="window.__xss=1">', '"><svg onload="window.__xss=1">', "javascript:window.__xss=1", "</script><script>window.__xss=1</script>"]
# Display strings the pages render from API answers. Identifiers (ids, endpoint names) are poisoned separately.
DISPLAY_KEYS = {"name", "label", "unit", "units", "dataset", "project_name", "smiles", "canonical_smiles", "method_note", "limitations", "note",
                "source", "license", "model", "title", "site", "method", "message", "version", "split_type", "formula", "error"}


def poison(obj, payload, keys=DISPLAY_KEYS):
    if isinstance(obj, dict):
        return {k: (payload + v if k in keys and isinstance(v, str) else poison(v, payload, keys)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [poison(x, payload, keys) for x in obj]
    return obj


class PoisonApi(MockApi):
    """Every display string the API returns carries a hostile payload."""

    def __init__(self, payload, **kw):
        super().__init__(**kw)
        self.payload = payload

    def dispatch(self, method, path, body):
        status, out, headers = super().dispatch(method, path, body)
        if isinstance(out, (dict, list)):
            out = poison(out, self.payload)
        return status, out, headers


class RichApi(MockApi):
    """MockApi with three nearest training compounds and fragment ranges, as a real uncertainty answer has."""

    def dispatch(self, method, path, body):
        status, out, headers = super().dispatch(method, path, body)
        if isinstance(out, dict) and "nearest_training" in out:
            out = {**out, "nearest_training": [{"smiles": s, "similarity": 0.5 - i * 0.05, "label": 1.0 + i} for i, s in enumerate(["CC", "CCC", "CCCC"])]}
        return status, out, headers


class CardApi(MockApi):
    """Serves model cards given as JSON (e.g. cards published by the synthetic registry) instead of the mock cards."""

    def __init__(self, cards: list[dict], **kw):
        super().__init__(**kw)
        self.cards = cards

    def dispatch(self, method, path, body):
        if path == "/models":
            return 200, {"models": self.cards}, {}
        if path.startswith("/models/"):
            for c in self.cards:
                if path == "/models/" + c["endpoint"]:
                    return 200, c, {}
            return 404, {"detail": "not found"}, {}
        return super().dispatch(method, path, body)


def card_json(registry, endpoint) -> dict:
    return json.loads(registry.model_card(endpoint).model_dump_json())
