"""What gets logged, and what deliberately does not.

People upload marksheets, identity documents and offer letters. A log line is
the easiest place in a system for that content to end up somewhere it was never
meant to go -- an aggregator, a third-party error tracker, a screenshot in a
support ticket. So the rule here is narrow and absolute: **log identifiers,
counts and timings; never document text.**

That means no extracted deadline values, no requirement names, no action
descriptions, no filename contents beyond the filename itself. If a field could
be read back to reconstruct part of somebody's document, it does not belong in
a log.

The one thing worth spending real effort on is model cost, because it is the
only number here that turns into money and the only one nobody notices until
the bill arrives.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock

logger = logging.getLogger("action_engine.telemetry")

__all__ = ["JsonFormatter", "Spend", "configure_logging", "spend", "timed"]


@dataclass
class Spend:
    """Running model cost for this process.

    Per-process and reset on restart, like the rate limiter, and for the same
    reason: it is a signal, not an accounting system. Anyone billing from this
    number instead of from the provider's own is doing it wrong.
    """

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, usage) -> None:
        """Add one response's usage. Tolerates a usage object of any shape."""
        with self._lock:
            self.requests += 1
            self.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.output_tokens += getattr(usage, "output_tokens", 0) or 0
            self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    @property
    def cache_hit_rate(self) -> float:
        """Zero here across repeated calls means the cached prefix is broken.

        The system prompt and schema are identical on every request, so they
        should be read from cache after the first. A rate stuck at zero means
        something volatile has leaked into the prefix and every request is
        paying full price for it.
        """
        total = self.input_tokens + self.cache_read_tokens
        return self.cache_read_tokens / total if total else 0.0

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            return {
                "requests": self.requests,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_hit_rate": round(self.cache_hit_rate, 3),
            }


spend = Spend()


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for anything that ingests logs.

    Only the fields explicitly attached to a record are emitted. There is no
    catch-all that sweeps up locals, because that is precisely how document
    text reaches a log without anyone deciding it should.
    """

    #: Everything the logging module puts on a record itself. Anything outside
    #: this set was attached deliberately by a caller.
    _BUILTIN = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(*, json_output: bool = True, level: int = logging.INFO) -> None:
    """Install the formatter. Called from the app, never at import."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        JsonFormatter()
        if json_output
        else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


@contextmanager
def timed(operation: str, **fields):
    """Log how long something took, and what it was for -- never what was in it."""
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.info(
            operation,
            extra={"operation": operation, "ms": round((time.perf_counter() - started) * 1000, 1), **fields},
        )
