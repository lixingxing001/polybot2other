"""Wire formats used by the dashboard's read-only HTTP endpoints."""

from __future__ import annotations

import json
import time
from typing import Any


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def accepts_gzip(header: str) -> bool:
    qualities = {}
    for item in header.lower().split(","):
        encoding, *parameters = item.split(";")
        quality = 1.0
        for parameter in parameters:
            name, _, value = parameter.strip().partition("=")
            if name == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        qualities[encoding.strip()] = quality
    return 0 < qualities.get("gzip", qualities.get("*", 0.0)) <= 1


def _changes(previous: Any, current: Any, path: list, updates: list, removals: list) -> None:
    if type(previous) is type(current):
        if isinstance(current, dict):
            for key in previous.keys() - current.keys():
                removals.append([*path, key])
            for key, value in current.items():
                if key in previous:
                    _changes(previous[key], value, [*path, key], updates, removals)
                else:
                    updates.append([[*path, key], value])
            return
        if isinstance(current, list) and len(previous) == len(current):
            for index, value in enumerate(current):
                _changes(previous[index], value, [*path, index], updates, removals)
            return
        if previous == current:
            return
    updates.append([path, current])


class StatusStream:
    """One connection holds only its last emitted snapshot. Reconnect starts fresh."""

    def __init__(self) -> None:
        self.previous: dict | None = None
        self.sequence = 0

    def frame(self, raw_snapshot: bytes) -> bytes:
        # Decode the actual wire representation to detach potentially shared bot
        # objects and normalize JSON keys and arrays before comparing them.
        current = json.loads(raw_snapshot)
        timestamp = time.time_ns() // 1_000_000
        sequence = self.sequence + 1
        encoded = None
        if self.previous is not None:
            updates, removals = [], []
            _changes(self.previous, current, [], updates, removals)
            if not updates and not removals:
                return b"data: " + json_bytes({"type": "ping", "seq": self.sequence, "sent_at_ms": timestamp}) + b"\n\n"
            delta = json_bytes({
                "type": "delta", "seq": sequence, "base": self.sequence,
                "sent_at_ms": timestamp, "set": updates, "remove": removals,
            })
            if len(delta) < len(raw_snapshot):
                encoded = delta
        if encoded is None:
            header = json_bytes({"type": "full", "seq": sequence, "sent_at_ms": timestamp})
            encoded = header[:-1] + b',"data":' + raw_snapshot + b"}"
        self.sequence = sequence
        self.previous = current
        return b"data: " + encoded + b"\n\n"
