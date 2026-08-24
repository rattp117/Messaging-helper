"""SPEC-v1.7.md R-G2 (AC-3): `core/registry_provider.py:RegistryProvider` --
the process-global per-user `HabitRegistry` cache. `.for_user(user_id)` is
cached (a DB read + registry build only happens once per user, until that
user's entry is invalidated); `.invalidate(user_id)` drops exactly one
user's cached entry so the very next call rebuilds it, with no restart --
and never touches another user's own already-cached entry. Fail-open: a
build error falls back to the base registry, logged, and NOT cached.
"""

from __future__ import annotations

import logging

import pytest

from habit_assistant.config import Config
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.storage.db import Database


def _add_reading(db: Database, user_id: str) -> None:
    db.add_user_habit(
        user_id,
        {
            "id": "reading",
            "type": "duration",
            "label_en": "reading",
            "label_th": "อ่านหนังสือ",
            "unit_en": "min",
            "unit_th": "นาที",
            "goal": 30.0,
            "unit_aliases": None,
        },
    )


def test_for_user_with_no_custom_habits_matches_from_config(tmp_path):
    db = Database(tmp_path / "provider1.db")
    config = Config()
    provider = RegistryProvider(config, db)

    registry = provider.for_user("u1")

    assert registry.ids() == HabitRegistry.from_config(config).ids()
    db.close()


def test_for_user_caches_across_calls_even_after_a_direct_db_write(tmp_path):
    """Proves the cache is real, not an accidental always-fresh-build: a
    `user_habits` row inserted directly (bypassing the provider) after the
    first `.for_user()` call must NOT appear until `.invalidate()` runs."""
    db = Database(tmp_path / "provider2.db")
    config = Config()
    provider = RegistryProvider(config, db)

    first = provider.for_user("u1")
    assert "reading" not in first.ids()

    _add_reading(db, "u1")
    still_cached = provider.for_user("u1")
    assert "reading" not in still_cached.ids()
    assert still_cached is first  # the exact same cached object, not rebuilt

    provider.invalidate("u1")
    rebuilt = provider.for_user("u1")
    assert "reading" in rebuilt.ids()
    db.close()


def test_invalidate_is_scoped_to_exactly_one_user(tmp_path):
    """AC-3's own explicit guarantee: invalidating one user's cache entry
    never rebuilds -- or otherwise disturbs -- another user's own cached
    registry."""
    db = Database(tmp_path / "provider3.db")
    config = Config()
    provider = RegistryProvider(config, db)

    u1_first = provider.for_user("u1")
    u2_first = provider.for_user("u2")

    _add_reading(db, "u1")
    provider.invalidate("u1")

    u1_second = provider.for_user("u1")
    u2_second = provider.for_user("u2")

    assert "reading" in u1_second.ids()
    assert u1_second is not u1_first
    assert u2_second is u2_first  # u2's cache entry untouched by u1's invalidation
    assert "reading" not in u2_second.ids()
    db.close()


def test_invalidate_on_a_never_cached_user_is_a_no_op(tmp_path):
    db = Database(tmp_path / "provider4.db")
    provider = RegistryProvider(Config(), db)
    provider.invalidate("ghost")  # must not raise
    registry = provider.for_user("ghost")
    assert registry.ids() == HabitRegistry.from_config(Config()).ids()
    db.close()


def test_cache_starts_empty_and_builds_lazily(tmp_path):
    db = Database(tmp_path / "provider5.db")
    provider = RegistryProvider(Config(), db)
    assert provider._cache == {}
    provider.for_user("u1")
    assert "u1" in provider._cache
    db.close()


def test_for_user_fails_open_to_the_base_registry_on_a_build_error(tmp_path, monkeypatch, caplog):
    """R-G2's own explicit fail-open contract: a build error (e.g. a
    corrupt row / DB hiccup) never raises to the caller -- it logs and
    falls back to `HabitRegistry.from_config(config)`, and does NOT cache
    the fallback (so the next call retries the real per-user build)."""
    db = Database(tmp_path / "provider6.db")
    config = Config()
    provider = RegistryProvider(config, db)

    def _boom(cls, cfg, database, user_id):
        raise RuntimeError("simulated corrupt user_habits row")

    monkeypatch.setattr(HabitRegistry, "for_user", classmethod(_boom))

    with caplog.at_level(logging.ERROR):
        result = provider.for_user("u1")

    assert result.ids() == HabitRegistry.from_config(config).ids()
    assert "u1" not in provider._cache  # the fallback is never cached
    assert any("u1" in record.getMessage() for record in caplog.records)
    db.close()


def test_for_user_retries_the_real_build_after_a_failed_attempt(tmp_path, monkeypatch):
    """Because a failed build is never cached, the very next call (once
    the underlying problem is gone) gets the real per-user registry, not a
    fallback pinned for the rest of the process's life."""
    db = Database(tmp_path / "provider7.db")
    config = Config()
    provider = RegistryProvider(config, db)
    real_for_user = HabitRegistry.for_user

    calls = {"n": 0}

    def _flaky(cls, cfg, database, user_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real_for_user(cfg, database, user_id)

    monkeypatch.setattr(HabitRegistry, "for_user", classmethod(_flaky))

    first = provider.for_user("u1")
    assert first.ids() == HabitRegistry.from_config(config).ids()  # fallback

    second = provider.for_user("u1")
    assert calls["n"] == 2  # retried the real build, not served from a cached fallback
    assert second.ids() == HabitRegistry.from_config(config).ids()
    db.close()
