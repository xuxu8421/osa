"""
Minimal synchronous event bus used across the pipeline.

Events are plain dataclasses with a `kind` string topic. Subscribers register
per-topic callbacks; '*' matches every event.

Design notes:
  * Pub/sub is synchronous (callback runs on the publisher's thread). That's
    fine because our producers already run in dedicated threads (BLE loop,
    audio thread); the GUI consumes via the per-frame tick.
  * We deliberately avoid asyncio here — too many of our producers are plain
    threads, and mixing the two has cost us enough time already.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Deque


@dataclass
class Event:
    t: float                  # monotonic-ish unix time
    kind: str                 # dotted topic: "chestband.data", "posture.change"
    payload: Any = None       # topic-specific object
    src: str = ''             # producer name (e.g. 'chestband', 'analyzer.posture')


Subscriber = Callable[[Event], None]


class EventBus:
    """Synchronous topic pub/sub with a bounded audit log."""

    def __init__(self, history: int = 1000):
        self._subs: dict[str, list[Subscriber]] = {}
        self._lock = Lock()
        self.history: Deque[Event] = deque(maxlen=history)

    def subscribe(self, kind: str, cb: Subscriber) -> Callable[[], None]:
        """Register a subscriber. Returns an unsubscribe() function."""
        with self._lock:
            self._subs.setdefault(kind, []).append(cb)

        def _unsub():
            with self._lock:
                lst = self._subs.get(kind, [])
                if cb in lst:
                    lst.remove(cb)
        return _unsub

    def publish(self, ev: Event):
        self.history.append(ev)
        subs = list(self._subs.get(ev.kind, ())) + list(self._subs.get('*', ()))
        for cb in subs:
            try:
                cb(ev)
            except Exception as e:
                # Never let a bad subscriber break the bus.
                print(f"[bus] subscriber error on {ev.kind}: {e}")

    def emit(self, kind: str, payload: Any = None, src: str = ''):
        self.publish(Event(time.time(), kind, payload, src))
