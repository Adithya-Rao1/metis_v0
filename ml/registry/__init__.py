"""Model registry entry point used by the API (backend/jobs/deps.py)."""
from .registry import default_registry as get_registry

__all__ = ["get_registry"]
