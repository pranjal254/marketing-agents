from __future__ import annotations

import pytest

from shiftai_shared.resilience import (
    InMemoryIdempotencyStore,
    PermanentError,
    SqliteIdempotencyStore,
    TransientError,
    execute_idempotent,
    with_retries,
)


def test_retry_backoff_schedule() -> None:
    delays: list[float] = []
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise TransientError("boom")
        return "ok"

    result = with_retries(flaky, retries=3, base_delay_s=2.0, sleep=delays.append)
    assert result == "ok"
    assert delays == [2.0, 4.0, 8.0]


def test_retry_exhaustion_raises_last_error() -> None:
    def always_fails() -> None:
        raise TransientError("down")

    with pytest.raises(TransientError):
        with_retries(always_fails, retries=2, sleep=lambda _: None)


def test_permanent_error_not_retried() -> None:
    calls = {"n": 0}

    def fails_hard() -> None:
        calls["n"] += 1
        raise PermanentError("bad request")

    with pytest.raises(PermanentError):
        with_retries(fails_hard, retries=3, sleep=lambda _: None)
    assert calls["n"] == 1


def test_idempotent_execution_in_memory() -> None:
    store = InMemoryIdempotencyStore()
    effects = {"n": 0}

    def side_effect() -> dict[str, object]:
        effects["n"] += 1
        return {"ref": "item-1"}

    first, repeat1 = execute_idempotent("key-1", store, side_effect)
    second, repeat2 = execute_idempotent("key-1", store, side_effect)
    assert effects["n"] == 1
    assert first == second == {"ref": "item-1"}
    assert (repeat1, repeat2) == (False, True)


def test_idempotent_store_sqlite(tmp_path: object) -> None:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    store = SqliteIdempotencyStore(str(tmp_path / "idem.sqlite"))
    store.put("k", {"a": 1})
    store.put("k", {"a": 2})  # second write ignored — key results are immutable
    assert store.get("k") == {"a": 1}
    assert store.get("missing") is None
