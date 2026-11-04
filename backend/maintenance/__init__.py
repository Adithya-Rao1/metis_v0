"""Scheduled maintenance (purges). Run with `python -m backend.maintenance`."""

from backend.maintenance.runner import run_once

__all__ = ["run_once"]
