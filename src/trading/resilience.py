"""Retry-with-backoff for flaky external calls (broker, Anthropic). Keeps the
daemon alive through transient network/API failures without masking real bugs:
only a configured set of exceptions is retried."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")


@dataclass
class RetryConfig:
    retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    factor: float = 2.0


def is_retryable(e: BaseException) -> bool:
    """A client error will fail identically every time — retrying it just delays
    the report. Matches the SDKs' own policy (408/409/429/5xx). Reads `status_code`
    duck-typed so this stays generic across the broker and Anthropic clients.

    This is not hypothetical: on 2026-07-24 an exhausted Anthropic credit balance
    returned 400 and every failing cycle burned three attempts plus backoff.
    """
    code = getattr(e, "status_code", None)
    if code is None:
        return True  # network/transport error with no HTTP status — worth a retry
    return code in (408, 409, 429) or code >= 500


def with_retry(
    fn: Callable[[], T],
    *,
    config: RetryConfig | None = None,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn`, retrying on `retry_on` with exponential backoff. Re-raises the
    last exception after exhausting retries. `sleep` is injectable for tests."""
    cfg = config or RetryConfig()
    last: BaseException | None = None
    for attempt in range(cfg.retries + 1):
        try:
            return fn()
        except retry_on as e:
            last = e
            if attempt >= cfg.retries or not is_retryable(e):
                break
            delay = min(cfg.base_delay * (cfg.factor ** attempt), cfg.max_delay)
            if on_retry:
                on_retry(attempt + 1, e, delay)
            sleep(delay)
    assert last is not None
    raise last
