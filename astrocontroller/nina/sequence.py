"""
Flatten NINA's sequence tree into something a dashboard can render.

`/sequence/json` returns an array whose entries are containers::

    {"Name": ..., "Status": ..., "Conditions": [...], "Items": [...], "Triggers": [...]}

`Items` holds both leaf instructions *and* nested containers, so the structure
is recursive and of unbounded depth. One entry in the top-level array is a
`{"GlobalTriggers": [...]}` object rather than a container, which is skipped.

NINA emits no per-step websocket event, so the caller must poll this; the
websocket only announces coarse SEQUENCE-STARTING / SEQUENCE-FINISHED.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

RUNNING_STATES = frozenset({"RUNNING", "Running"})
FINISHED_STATES = frozenset({"FINISHED", "Finished"})
SKIPPED_STATES = frozenset({"SKIPPED", "Skipped"})
FAILED_STATES = frozenset({"FAILED", "Failed"})


@dataclass
class SequenceStep:
    """One node of the sequence tree, flattened with its depth."""

    id: str
    name: str
    status: str
    depth: int
    is_container: bool
    parent_id: Optional[str] = None
    description: Optional[str] = None
    conditions: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status in RUNNING_STATES

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_STATES

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "depth": self.depth,
            "is_container": self.is_container,
            "parent_id": self.parent_id,
            "description": self.description,
            "conditions": self.conditions,
            "triggers": self.triggers,
        }


@dataclass
class SequenceTree:
    steps: list[SequenceStep] = field(default_factory=list)
    global_triggers: list[str] = field(default_factory=list)
    available: bool = False
    error: Optional[str] = None

    @property
    def current(self) -> Optional[SequenceStep]:
        """
        The deepest running leaf.

        Containers stay RUNNING while their children run, so the innermost
        running node is the step actually executing -- that is what belongs in
        the "currently doing" line of the dashboard.
        """
        running = [s for s in self.steps if s.running]
        if not running:
            return None
        leaves = [s for s in running if not s.is_container]
        return leaves[-1] if leaves else running[-1]

    @property
    def progress(self) -> tuple[int, int]:
        """(finished-or-skipped leaves, total leaves)."""
        leaves = [s for s in self.steps if not s.is_container]
        done = sum(
            1 for s in leaves
            if s.status in FINISHED_STATES or s.status in SKIPPED_STATES
        )
        return done, len(leaves)

    def as_dict(self) -> dict:
        done, total = self.progress
        current = self.current
        return {
            "available": self.available,
            "error": self.error,
            "steps": [s.as_dict() for s in self.steps],
            "global_triggers": self.global_triggers,
            "current_id": current.id if current else None,
            "current_name": current.name if current else None,
            "done": done,
            "total": total,
        }


def _label(entry: Any) -> str:
    if isinstance(entry, dict):
        for key in ("Name", "Type", "SequenceItemType"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "(unnamed)"
    return str(entry)


def _labels(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    return [_label(e) for e in entries]


def parse_sequence(payload: Any) -> SequenceTree:
    """
    Build a flat, depth-annotated step list from `/sequence/json`.

    Unknown or unexpected shapes degrade to an empty tree with `available`
    False rather than raising: a sequencer that has not been initialised is a
    normal state, not an error.
    """
    tree = SequenceTree()
    if payload is None:
        return tree
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        tree.error = f"unexpected sequence payload: {type(payload).__name__}"
        return tree

    counter = _Counter()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if "GlobalTriggers" in entry:
            tree.global_triggers = _labels(entry.get("GlobalTriggers"))
            continue
        _walk(entry, depth=0, parent_id=None, out=tree.steps, counter=counter)

    tree.available = bool(tree.steps)
    return tree


class _Counter:
    """Stable synthetic ids: NINA does not give sequence items an id."""

    def __init__(self) -> None:
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"s{self.n}"


def _walk(
    entry: dict,
    depth: int,
    parent_id: Optional[str],
    out: list[SequenceStep],
    counter: _Counter,
) -> None:
    items = entry.get("Items")
    is_container = isinstance(items, list)
    step_id = counter.next()

    out.append(
        SequenceStep(
            id=step_id,
            name=_label(entry),
            status=str(entry.get("Status", "") or ""),
            depth=depth,
            is_container=is_container,
            parent_id=parent_id,
            description=entry.get("Description") or None,
            conditions=_labels(entry.get("Conditions")),
            triggers=_labels(entry.get("Triggers")),
        )
    )

    if is_container:
        for child in items:
            if isinstance(child, dict):
                _walk(child, depth + 1, step_id, out, counter)


def iter_steps(tree: SequenceTree) -> Iterator[SequenceStep]:
    yield from tree.steps
