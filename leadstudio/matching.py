"""Substructure matching under a wall-clock limit.

RDKit's matcher is exponential for some (valid) patterns on dense molecules and cannot be interrupted from outside.
The extra-atom-check callback runs for every candidate atom pair, so returning False once the deadline has passed
collapses the search within microseconds. A timed-out search reports "no match" (the safe answer for a penalty
pattern); callers that need to know can read the `timed_out` flag of the result.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from rdkit import Chem

DEFAULT_SECONDS = 0.25


@dataclass
class MatchResult:
    matches: tuple[tuple[int, ...], ...]
    timed_out: bool


def match(
    mol: Chem.Mol,
    query: Chem.Mol,
    seconds: float = DEFAULT_SECONDS,
    max_matches: int = 1,
    uniquify: bool = True,
) -> MatchResult:
    deadline = time.monotonic() + max(0.0, seconds)
    state = {"expired": False}

    def _check(*_args: object) -> bool:
        if time.monotonic() >= deadline:
            state["expired"] = True
            return False
        return True

    params = Chem.SubstructMatchParameters()
    params.maxMatches = max_matches  # type: ignore[assignment]  # rdkit stubs type this attribute wrongly
    params.uniquify = uniquify  # type: ignore[assignment]  # rdkit stubs type this attribute wrongly
    params.setExtraAtomCheckFunc(_check)
    found = mol.GetSubstructMatches(query, params)
    return MatchResult(tuple(found), state["expired"])


def first_match(mol: Chem.Mol, query: Chem.Mol, seconds: float = DEFAULT_SECONDS) -> tuple[int, ...]:
    res = match(mol, query, seconds)
    return res.matches[0] if res.matches else ()
