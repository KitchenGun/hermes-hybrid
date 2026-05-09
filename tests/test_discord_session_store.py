"""Tests for SessionStore (P2 — Discord auto-resume)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from src.state import DiscordSession, SessionStore, make_session_key


async def _make(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "state.db")
    await store.init()
    return store


def test_make_session_key_dm_prefix():
    key = make_session_key(user_id="42", channel_id="100", guild_id=None)
    assert key.startswith("dm:")
    assert "100" in key and "42" in key


def test_make_session_key_guild():
    key = make_session_key(
        user_id="42", channel_id="100", guild_id="999",
    )
    assert not key.startswith("dm:")
    assert key == "999:100:42"


def test_make_session_key_dm_vs_guild_with_same_channel_dont_collide():
    dm_key = make_session_key(user_id="1", channel_id="50", guild_id=None)
    guild_key = make_session_key(
        user_id="1", channel_id="50", guild_id="50",
    )
    assert dm_key != guild_key


def test_make_session_key_empty_guild_string_treated_as_dm():
    key = make_session_key(user_id="1", channel_id="2", guild_id="")
    assert key.startswith("dm:")


def test_make_session_key_rejects_missing_required():
    with pytest.raises(ValueError):
        make_session_key(user_id="", channel_id="100")
    with pytest.raises(ValueError):
        make_session_key(user_id="1", channel_id="")


@pytest.mark.asyncio
async def test_init_is_idempotent(tmp_path: Path):
    store = SessionStore(tmp_path / "state.db")
    await store.init()
    await store.init()  # second call must not fail
    assert (tmp_path / "state.db").exists()


@pytest.mark.asyncio
async def test_save_then_get_round_trip(tmp_path: Path):
    store = await _make(tmp_path)
    key = make_session_key(user_id="1", channel_id="100", guild_id="g")
    await store.save(
        DiscordSession(
            session_key=key,
            user_id="1",
            channel_id="100",
            guild_id="g",
            profile="researcher",
            forced_profile=None,
            last_task_id="t_abc",
            context={"session_id": "s_123"},
        )
    )
    got = await store.get(key)
    assert got is not None
    assert got.session_key == key
    assert got.profile == "researcher"
    assert got.last_task_id == "t_abc"
    assert got.context == {"session_id": "s_123"}
    assert got.updated_at  # auto-stamped


@pytest.mark.asyncio
async def test_save_same_key_upserts(tmp_path: Path):
    store = await _make(tmp_path)
    key = make_session_key(user_id="1", channel_id="100", guild_id="g")
    await store.save(
        DiscordSession(
            session_key=key, user_id="1", channel_id="100", guild_id="g",
            last_task_id="t_first",
            context={"session_id": "s_first"},
        )
    )
    await store.save(
        DiscordSession(
            session_key=key, user_id="1", channel_id="100", guild_id="g",
            last_task_id="t_second",
            context={"session_id": "s_second"},
        )
    )
    got = await store.get(key)
    assert got is not None
    assert got.last_task_id == "t_second"
    assert got.context == {"session_id": "s_second"}
    # Only one row exists (upsert semantics).
    rows = await store.list_all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_multiple_users_and_channels_are_separated(tmp_path: Path):
    store = await _make(tmp_path)
    a_key = make_session_key(user_id="1", channel_id="10", guild_id="g")
    b_key = make_session_key(user_id="2", channel_id="10", guild_id="g")
    c_key = make_session_key(user_id="1", channel_id="11", guild_id="g")
    for k, uid, cid in [
        (a_key, "1", "10"),
        (b_key, "2", "10"),
        (c_key, "1", "11"),
    ]:
        await store.save(
            DiscordSession(
                session_key=k, user_id=uid, channel_id=cid,
                guild_id="g",
                context={"session_id": f"s_{uid}_{cid}"},
            )
        )
    user1_rows = await store.list_for_user("1")
    assert {r.channel_id for r in user1_rows} == {"10", "11"}
    user2_rows = await store.list_for_user("2")
    assert len(user2_rows) == 1
    assert user2_rows[0].channel_id == "10"


@pytest.mark.asyncio
async def test_corrupt_context_json_does_not_crash(tmp_path: Path):
    store = await _make(tmp_path)
    # Manually inject a row with broken JSON in context_json.
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute(
            "INSERT INTO discord_sessions("
            "session_key, user_id, channel_id, guild_id, profile, "
            "forced_profile, last_task_id, context_json, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            ("dm:1:1", "1", "1", None, None, None, None,
             "{not valid json", "2026-05-07T00:00:00+00:00"),
        )
        await db.commit()
    rows = await store.list_all()
    assert len(rows) == 1
    assert rows[0].context == {}


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown_key(tmp_path: Path):
    store = await _make(tmp_path)
    assert await store.get("dm:never:never") is None


@pytest.mark.asyncio
async def test_delete(tmp_path: Path):
    store = await _make(tmp_path)
    key = make_session_key(user_id="1", channel_id="2", guild_id=None)
    await store.save(
        DiscordSession(session_key=key, user_id="1", channel_id="2")
    )
    assert await store.delete(key) is True
    assert await store.get(key) is None
    # second delete returns False
    assert await store.delete(key) is False


@pytest.mark.asyncio
async def test_prune_older_than_removes_stale(tmp_path: Path):
    store = await _make(tmp_path)
    fresh_key = make_session_key(user_id="1", channel_id="1", guild_id=None)
    stale_key = make_session_key(user_id="2", channel_id="2", guild_id=None)
    await store.save(
        DiscordSession(
            session_key=fresh_key, user_id="1", channel_id="1",
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
    )
    long_ago = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat(timespec="seconds")
    await store.save(
        DiscordSession(
            session_key=stale_key, user_id="2", channel_id="2",
            updated_at=long_ago,
        )
    )
    removed = await store.prune_older_than(days=7)
    assert removed == 1
    assert await store.get(fresh_key) is not None
    assert await store.get(stale_key) is None


@pytest.mark.asyncio
async def test_prune_rejects_negative_days(tmp_path: Path):
    store = await _make(tmp_path)
    with pytest.raises(ValueError):
        await store.prune_older_than(days=-1)


# ---- hydrate_user_session_map ----------------------------------------


@pytest.mark.asyncio
async def test_hydrate_empty_returns_empty(tmp_path: Path):
    store = await _make(tmp_path)
    assert await store.hydrate_user_session_map() == {}


@pytest.mark.asyncio
async def test_hydrate_returns_int_user_id_to_session_id(tmp_path: Path):
    store = await _make(tmp_path)
    await store.save(
        DiscordSession(
            session_key="g:c1:42", user_id="42", channel_id="c1",
            guild_id="g",
            context={"session_id": "s_42"},
        )
    )
    got = await store.hydrate_user_session_map()
    assert got == {42: "s_42"}


@pytest.mark.asyncio
async def test_hydrate_picks_most_recent_session_per_user(tmp_path: Path):
    store = await _make(tmp_path)
    older = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat(timespec="seconds")
    newer = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await store.save(
        DiscordSession(
            session_key="g:c1:1", user_id="1", channel_id="c1",
            guild_id="g", context={"session_id": "s_old"},
            updated_at=older,
        )
    )
    await store.save(
        DiscordSession(
            session_key="g:c2:1", user_id="1", channel_id="c2",
            guild_id="g", context={"session_id": "s_new"},
            updated_at=newer,
        )
    )
    got = await store.hydrate_user_session_map()
    assert got == {1: "s_new"}


@pytest.mark.asyncio
async def test_hydrate_skips_non_int_user_id(tmp_path: Path):
    store = await _make(tmp_path)
    await store.save(
        DiscordSession(
            session_key="dm:c1:nonint", user_id="not-an-int",
            channel_id="c1",
            context={"session_id": "s_x"},
        )
    )
    got = await store.hydrate_user_session_map()
    assert got == {}


@pytest.mark.asyncio
async def test_hydrate_skips_rows_without_session_id(tmp_path: Path):
    store = await _make(tmp_path)
    await store.save(
        DiscordSession(
            session_key="dm:c1:1", user_id="1", channel_id="c1",
            context={},  # no session_id
        )
    )
    got = await store.hydrate_user_session_map()
    assert got == {}


@pytest.mark.asyncio
async def test_session_does_not_persist_secrets(tmp_path: Path):
    """Smoke check: nothing about the API should encourage saving raw
    user message text or tokens. Verified by the model: save() takes
    only session_key + ids + small context dict, no message field."""
    store = await _make(tmp_path)
    key = make_session_key(user_id="1", channel_id="2", guild_id=None)
    await store.save(
        DiscordSession(session_key=key, user_id="1", channel_id="2")
    )
    # Inspect the row directly — context_json should be "{}" not contain
    # secret-looking data.
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute(
            "SELECT context_json FROM discord_sessions WHERE session_key=?",
            (key,),
        ) as cur:
            row = await cur.fetchone()
    assert row is not None
    payload = json.loads(row[0]) if row[0] else {}
    assert payload == {}
