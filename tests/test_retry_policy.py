"""Retry policy. A client error fails identically every time; retrying it delays
the report and, on a paid API, can burn quota. Regression test for 2026-07-24,
when an exhausted credit balance returned 400 and each cycle retried it 3x."""

import pytest

from trading.resilience import RetryConfig, is_retryable, with_retry


class _HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def _counting(exc):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise exc
    return fn, calls


def test_400_is_not_retried():
    fn, calls = _counting(_HttpError(400))
    with pytest.raises(_HttpError):
        with_retry(fn, config=RetryConfig(retries=3), sleep=lambda _: None)
    assert calls["n"] == 1, "a 400 must fail fast, not burn every attempt"


def test_429_is_retried():
    fn, calls = _counting(_HttpError(429))
    with pytest.raises(_HttpError):
        with_retry(fn, config=RetryConfig(retries=2), sleep=lambda _: None)
    assert calls["n"] == 3


def test_500_is_retried():
    fn, calls = _counting(_HttpError(503))
    with pytest.raises(_HttpError):
        with_retry(fn, config=RetryConfig(retries=2), sleep=lambda _: None)
    assert calls["n"] == 3


def test_transport_error_without_status_is_retried():
    """A dropped connection has no status_code and is exactly what retry is for."""
    fn, calls = _counting(ConnectionError("connection reset"))
    with pytest.raises(ConnectionError):
        with_retry(fn, config=RetryConfig(retries=2), sleep=lambda _: None)
    assert calls["n"] == 3


def test_success_still_returns():
    assert with_retry(lambda: 42, sleep=lambda _: None) == 42


def test_is_retryable_classification():
    assert not is_retryable(_HttpError(400))
    assert not is_retryable(_HttpError(401))
    assert not is_retryable(_HttpError(404))
    assert is_retryable(_HttpError(408))
    assert is_retryable(_HttpError(409))
    assert is_retryable(_HttpError(429))
    assert is_retryable(_HttpError(500))
    assert is_retryable(ValueError("no status"))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
