# ruff: noqa: E501, E702, E741, B007, B008, B905, E402, SIM105, SIM108, SIM115, S603, S310
"""Docking preparation (Agent 7). Prepares files for Vina/GNINA; never runs docking. Exposes `router`."""
from .router import router

__all__ = ["router"]
