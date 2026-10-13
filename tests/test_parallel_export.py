"""Concorrencia, redistribuicao de paginas e isolamento dos proxies."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import pdf_exporter as exporter
from src.browser import BrowserSession
from src.proxy import AllProxiesDeadError, ProxyPool
from src.utils import BlockedTooLongError, RateLimitConfig, RateLimiter


def test_partition_preserves_cooldowns_without_sharing_exits():
    pool = ProxyPool([f"http://proxy{i}:8080" for i in range(7)], clock=lambda: 100.0)
    pool.mark_blocked(60.0)
    pool.pick_best()
    pool.mark_failed()
    groups = pool.partition(3)
    assert groups[0].current == pool.current
    assert sum(map(len, groups)) == len(pool)
    assert len({label for group in groups for label in group.labels()}) == len(pool)
    blocked = next(state for group in groups for state in group._exits if state.blocks)
    assert blocked.blocked_until == 160.0
    groups[0].mark_failed()
    assert pool._exits[pool._index].consecutive_failures == 1


def test_partition_excludes_dead_exits():
    pool = ProxyPool(["http://dead:80", "http://live:80"])
    pool._exits[0].dead = True
    assert [group.current for group in pool.partition(8)] == ["http://live:80"]
    pool._exits[1].dead = True
    with pytest.raises(AllProxiesDeadError):
        pool.partition(8)


def _resources(tmp_path, logger):
    cfg = exporter.ExportConfig(
        dom_budget_ms=1000, timeout_ms=2000, slow_threshold_seconds=60,
        headless=True, max_workers=2,
        rate_limit=RateLimitConfig(base_delay_seconds=0, jitter_ratio=0),
    )
    acc = exporter.ExportAccumulators(pages_dir=tmp_path, checkpoint=MagicMock())
    acc.checkpoint.is_exported.return_value = False
    sessions = [BrowserSession(pool=ProxyPool([f"http://proxy{i}:80"])) for i in range(2)]
    limiters = [RateLimiter(cfg.rate_limit, logger) for _ in sessions]
    acc.network_workers = list(zip(sessions, limiters))
    return cfg, acc, sessions, limiters


def test_blocked_worker_returns_page_and_does_not_delay_the_queue(tmp_path, mock_logger, monkeypatch):
    async def run():
        cfg, acc, sessions, limiters = _resources(tmp_path, mock_logger)
        attempts = []

        async def export(slot, session, url, index, total, *args, **kwargs):
            attempts.append((url, session))
            if session is sessions[0]:
                await limiters[0].set_cooldown(3600)
                raise exporter._DeferExport()
            await asyncio.sleep(0.01)
            acc.exported[index] = (tmp_path / f"{index}.pdf", url)
            return True

        monkeypatch.setattr(exporter, "_export_with_retries", export)
        await asyncio.wait_for(exporter._run_parallel(
            sessions[0], [(1, "a"), (2, "b"), (3, "c")], 3,
            acc, cfg, mock_logger, MagicMock(), 0, limiters[0], 4,
        ), timeout=2)
        assert list(acc.ordered_exports()) == [(tmp_path / f"{i}.pdf", u) for i, u in enumerate("abc", 1)]
        assert ("a", sessions[1]) in attempts
        assert limiters[0].cooldown_remaining() > 3500
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())


def test_dead_worker_does_not_abort_healthy_worker(tmp_path, mock_logger, monkeypatch):
    async def run():
        cfg, acc, sessions, limiters = _resources(tmp_path, mock_logger)

        async def export(slot, session, url, index, total, *args, **kwargs):
            if session is sessions[0]:
                session.pool._exits[0].dead = True
                raise AllProxiesDeadError("morto")
            await asyncio.sleep(0.01)
            acc.exported[index] = (tmp_path / f"{index}.pdf", url)
            return True

        monkeypatch.setattr(exporter, "_export_with_retries", export)
        await asyncio.wait_for(exporter._run_parallel(
            sessions[0], [(1, "a"), (2, "b")], 2,
            acc, cfg, mock_logger, MagicMock(), 0, limiters[0], 4,
        ), timeout=2)
        assert len(acc.exported) == 2

    asyncio.run(run())


def test_fatal_error_cancels_other_workers(tmp_path, mock_logger, monkeypatch):
    async def run():
        cfg, acc, sessions, limiters = _resources(tmp_path, mock_logger)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def export(slot, session, *args, **kwargs):
            if session is sessions[0]:
                await started.wait()
                raise BlockedTooLongError(3600)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        monkeypatch.setattr(exporter, "_export_with_retries", export)
        with pytest.raises(BlockedTooLongError):
            await asyncio.wait_for(exporter._run_parallel(
                sessions[0], [(1, "a"), (2, "b")], 2,
                acc, cfg, mock_logger, MagicMock(), 0, limiters[0], 4,
            ), timeout=2)
        assert cancelled.is_set()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(run())


def test_deferral_preserves_attempt_limit(tmp_path, mock_logger, monkeypatch):
    async def run():
        cfg, acc, sessions, limiters = _resources(tmp_path, mock_logger)
        page = MagicMock()
        monkeypatch.setattr(exporter, "_ensure_browser_alive", AsyncMock())
        monkeypatch.setattr(exporter, "_slot_page", AsyncMock(return_value=page))
        monkeypatch.setattr(exporter, "rotate_exit_after_block", AsyncMock(return_value=False))
        blocked = exporter.ExportOutcome(exporter._BLOCKED, "429", retry_after=60)
        attempt = AsyncMock(return_value=(page, blocked))
        monkeypatch.setattr(exporter, "_export_one", attempt)
        slot = exporter._PageSlot(page, 0)
        with pytest.raises(exporter._DeferExport):
            await exporter._export_with_retries(
                slot, sessions[0], "url", 1, 1, acc, cfg, mock_logger, limiters[0], max_attempts=2,
            )
        assert acc.deferred_attempts["url"] == 1
        monkeypatch.setattr(limiters[0], "wait", AsyncMock())
        assert not await exporter._export_with_retries(
            slot, sessions[0], "url", 1, 1, acc, cfg, mock_logger, limiters[0], max_attempts=2,
        )
        assert attempt.await_count == 2
        assert acc.failures == {"url": "429"}
        assert acc.deferred_attempts == {}

    asyncio.run(run())


def test_navegacoes_em_paralelo_respeitam_limite_de_impressao(tmp_path, mock_logger, monkeypatch):
    async def run():
        cfg, acc, sessions, _limiters = _resources(tmp_path, mock_logger)
        acc.pdf_slots = asyncio.Semaphore(2)
        active = 0
        peak = 0

        async def print_pdf(page, path):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.03)
                return 4000
            finally:
                active -= 1

        monkeypatch.setattr(exporter, "_goto_dom_ready", AsyncMock())
        monkeypatch.setattr(exporter, "_is_login_page", AsyncMock(return_value=False))
        monkeypatch.setattr(exporter, "_prepare_page_for_pdf", AsyncMock())
        monkeypatch.setattr(exporter, "_verify_page_assets", AsyncMock())
        monkeypatch.setattr(exporter, "_generate_pdf", print_pdf)
        monkeypatch.setattr(exporter, "_record_export_result", AsyncMock())
        pages = [MagicMock(title=AsyncMock(return_value=f"Pagina {i}"), goto=AsyncMock()) for i in range(8)]
        outcomes = await asyncio.gather(*(
            exporter._export_one(page, sessions[0], f"url-{i}", i, 8, acc, cfg, mock_logger)
            for i, page in enumerate(pages, 1)
        ))
        assert peak == 2
        assert all(outcome.status == exporter._OK for _page, outcome in outcomes)

    asyncio.run(run())


def test_desafios_de_proxies_com_reserva_nao_acionam_breaker_global(tmp_path, mock_logger, monkeypatch):
    async def run():
        cfg, acc, sessions, limiters = _resources(tmp_path, mock_logger)
        sessions[0].pool = ProxyPool(["http://proxy1:80", "http://reserva1:80"])
        page = MagicMock()
        monkeypatch.setattr(exporter, "_ensure_browser_alive", AsyncMock())
        monkeypatch.setattr(exporter, "_slot_page", AsyncMock(return_value=page))
        blocked = exporter.ExportOutcome(exporter._BLOCKED, "Cloudflare 403", session_failure=True)
        one = AsyncMock(side_effect=[(page, blocked)] * 6 + [(page, exporter.ExportOutcome(exporter._OK))])
        monkeypatch.setattr(exporter, "_export_one", one)
        breaker = AsyncMock()
        monkeypatch.setattr(exporter, "_bump_session_failures", breaker)
        monkeypatch.setattr(limiters[0], "wait", AsyncMock())

        async def rotate(session, limiter, *args, **kwargs):
            await limiter.set_cooldown(0)
            return True

        monkeypatch.setattr(exporter, "rotate_exit_after_block", rotate)
        assert await exporter._export_with_retries(
            exporter._PageSlot(page, 0), sessions[0], "url", 1, 1,
            acc, cfg, mock_logger, limiters[0], max_attempts=7,
        )
        breaker.assert_not_awaited()
        assert one.await_count == 7
        assert not acc.abort_requested

    asyncio.run(run())
