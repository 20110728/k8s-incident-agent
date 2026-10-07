"""Per-thread read budget; do not start another request after the deadline."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar("observation_deadline", default=None)


@contextmanager
def read_budget(seconds):
    end = time.monotonic() + seconds
    current = _deadline.get()
    token = _deadline.set(min(current, end) if current is not None else end)
    try:
        yield
    finally:
        _deadline.reset(token)


class BoundedApi:
    def __init__(self, api):
        self.api = api

    def __getattr__(self, name):
        method = getattr(self.api, name)
        if not callable(method):
            return method

        def call(*args, **kwargs):
            end = _deadline.get()
            if end is not None:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("OBSERVATION_DEADLINE_EXCEEDED")
                connect, read = kwargs.get("_request_timeout", (3, 10))
                # Connect + read timeouts share the remaining request budget.
                kwargs["_request_timeout"] = (min(connect, remaining / 2), min(read, remaining / 2))
            return method(*args, **kwargs)
        return call
