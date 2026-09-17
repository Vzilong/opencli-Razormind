from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from backend import browser_pool
from backend.browser_pool import LocalBrowserPool, NoReadyBrowserSlotError, RedisBrowserPool


@pytest.mark.asyncio
async def test_unrouted_tasks_never_use_account_profile(monkeypatch):
    pool = LocalBrowserPool(["account", "public"])
    monkeypatch.setattr(browser_pool, "_account_reservations", AsyncMock(return_value={"account"}))
    async with pool.acquire() as endpoint:
        assert endpoint == "public"
    with pytest.raises(NoReadyBrowserSlotError):
        async with pool.acquire("account"):
            pytest.fail("a task received a login Profile")


@pytest.mark.asyncio
async def test_waiting_task_rechecks_ownership_and_releases_slot(monkeypatch):
    pool = LocalBrowserPool(["account"])
    monkeypatch.setattr(
        browser_pool, "_account_reservations", AsyncMock(side_effect=[set(), {"account"}])
    )
    with pytest.raises(NoReadyBrowserSlotError):
        async with pool.acquire("account"):
            pytest.fail("ownership changed while acquiring")
    assert pool.available_for("account")


@pytest.mark.asyncio
async def test_waiting_task_rechecks_new_space_reservation_and_releases_slot(monkeypatch):
    pool = LocalBrowserPool(["space"])
    monkeypatch.setattr(browser_pool, "_account_reservations", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        browser_pool, "_space_reservations", AsyncMock(side_effect=[set(), {"space"}])
    )
    with pytest.raises(NoReadyBrowserSlotError):
        async with pool.acquire("space"):
            pytest.fail("Space reservation changed while acquiring")
    assert pool.available_for("space")


@pytest.mark.asyncio
async def test_redis_account_reservations_fail_before_leasing(monkeypatch):
    pool = RedisBrowserPool(["account"], "redis://unused")
    monkeypatch.setattr(browser_pool, "_account_reservations", AsyncMock(return_value={"account"}))
    for endpoint in (None, "account"):
        with pytest.raises(NoReadyBrowserSlotError):
            async with pool.acquire(endpoint):
                pytest.fail("reserved endpoint was leased")


@pytest.mark.asyncio
async def test_redis_space_reservation_cannot_be_bypassed_by_account_allowance(monkeypatch):
    pool = RedisBrowserPool(["space"], "redis://unused")
    monkeypatch.setattr(browser_pool, "_account_reservations", AsyncMock(return_value=set()))
    monkeypatch.setattr(browser_pool, "_space_reservations", AsyncMock(return_value={"space"}))
    for endpoint in (None, "space"):
        with pytest.raises(NoReadyBrowserSlotError):
            async with pool.acquire(endpoint):
                pytest.fail("reserved Space endpoint was leased")


@pytest.mark.asyncio
async def test_redis_rechecks_new_space_reservation_after_leasing_and_releases(monkeypatch):
    endpoint = "space"
    pool = RedisBrowserPool([endpoint], "redis://unused")
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.set = AsyncMock(return_value=True)
    redis.eval = AsyncMock(return_value=1)
    redis_cm = AsyncMock()
    redis_cm.__aenter__ = AsyncMock(return_value=redis)
    redis_cm.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(browser_pool, "_account_reservations", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        browser_pool, "_space_reservations", AsyncMock(side_effect=[set(), {endpoint}])
    )
    with patch.object(pool, "_client", return_value=redis_cm):
        with pytest.raises(NoReadyBrowserSlotError):
            async with pool.acquire(endpoint):
                pytest.fail("a post-lease Space reservation was ignored")
    assert any("del" in call.args[0] for call in redis.eval.await_args_list)


@pytest.mark.asyncio
async def test_reservations_are_read_from_durable_database(db_session, monkeypatch):
    from backend import database
    from backend.models.browser import BrowserInstance
    from backend.models.browser_account import BrowserAccount

    instance = BrowserInstance(endpoint="account", profile_name="account")
    db_session.add(instance)
    await db_session.flush()
    account = BrowserAccount(
        browser_instance_id=instance.id, platform="douyin", label="one", profile_name="account"
    )
    db_session.add(account)
    await db_session.commit()

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr(database, "AsyncSessionLocal", session)
    pool = LocalBrowserPool(["account"])
    pool.enforce_account_reservations = True
    assert await browser_pool._account_reservations(pool) == {"account"}
    account.status = "archived"
    await db_session.commit()
    assert await browser_pool._account_reservations(pool) == {"account"}
    await db_session.delete(account)
    await db_session.commit()
    assert await browser_pool._account_reservations(pool) == set()
    instance.login_reserved = True
    await db_session.commit()
    assert await browser_pool._account_reservations(pool) == {"account"}
