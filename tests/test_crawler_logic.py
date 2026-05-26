"""Testes de logica isolada do crawler (sem Playwright)."""
from __future__ import annotations

import json
import logging
import tempfile
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.crawler import (
    CrawlState,
    _enqueue_new_links,
    _maybe_requeue_timed_out,
    _restore_state_from_checkpoint,
)
from src.utils import (
    Checkpoint,
    MANIFEST_FILENAME,
    MAX_RETRY_ATTEMPTS,
    CrawlScope,
    canonicalize_url,
    parse_scope,
)


@pytest.fixture
def logger():
    return logging.getLogger("test_crawler")


# =============================================================================
# CrawlState basics
# =============================================================================

def test_crawl_state_defaults():
    state = CrawlState(queue=deque(["x"]), queued={"x"})
    assert "x" in state.queued
    assert state.api_cache == {}
    assert state.attempts == {}


# =============================================================================
# _restore_state_from_checkpoint
# =============================================================================

def test_restore_empty_returns_false(logger):
    """Sem nenhum estado salvo, retorna False (nao resumiu)."""
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        state = CrawlState(queue=deque(), queued=set())
        assert not _restore_state_from_checkpoint(state, ck, "https://x.com/a", logger)


def test_restore_with_mapped_only(logger):
    """Restaura mapped_urls mesmo sem queue/seen explicitos."""
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.mapped_urls = ["url1", "url2"]
        state = CrawlState(queue=deque(), queued=set())
        result = _restore_state_from_checkpoint(state, ck, "url1", logger)
        assert result is True
        assert state.ordered_urls == ["url1", "url2"]


def test_restore_re_enqueues_failures(logger):
    """URLs em failures (com attempts < MAX) voltam pra fila."""
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.mapped_urls = ["url1"]
        ck.manifest.failures["url2"] = {
            "error": "timeout", "attempts": 1, "last_attempt": "",
        }
        state = CrawlState(queue=deque(), queued=set())
        _restore_state_from_checkpoint(state, ck, "url1", logger)
        assert "url2" in state.queued


def test_restore_ignores_exhausted_failures(logger):
    """URLs com failures.attempts >= MAX nao sao re-enfileiradas."""
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.mapped_urls = ["url1"]
        ck.manifest.failures["dead"] = {
            "error": "X", "attempts": MAX_RETRY_ATTEMPTS, "last_attempt": "",
        }
        state = CrawlState(queue=deque(), queued=set())
        _restore_state_from_checkpoint(state, ck, "url1", logger)
        assert "dead" not in state.queued


# =============================================================================
# _enqueue_new_links
# =============================================================================

def test_enqueue_filters_javascript_urls(logger):
    """URLs javascript: ou # devem ser filtradas."""
    scope = parse_scope("https://x.com/display/PROT/Page")
    state = CrawlState(queue=deque(), queued=set(), seen={"https://x.com/display/PROT/Page"})
    _enqueue_new_links(
        ["javascript:void(0)", "#section", "/display/PROT/Other"],
        "https://x.com/display/PROT/Page",
        state, scope,
    )
    assert all("javascript" not in q for q in state.queued)
    assert all("#" != q for q in state.queued)


def test_enqueue_skips_out_of_scope(logger):
    """URLs fora do escopo (outro dominio) sao filtradas."""
    scope = parse_scope("https://tdn.totvs.com/display/PROT/Page")
    state = CrawlState(queue=deque(), queued=set(), seen=set())
    _enqueue_new_links(
        ["https://other-domain.com/page"],
        "https://tdn.totvs.com/display/PROT/Page",
        state, scope,
    )
    assert "https://other-domain.com/page" not in state.queued


def test_enqueue_skips_already_seen(logger):
    """URLs ja em seen nao re-enfileiram."""
    scope = parse_scope("https://x.com/display/PROT/Page")
    seen_url = canonicalize_url("https://x.com/display/PROT/Other")
    state = CrawlState(queue=deque(), queued=set(), seen={seen_url})
    _enqueue_new_links(
        ["/display/PROT/Other"],
        "https://x.com/display/PROT/Page",
        state, scope,
    )
    assert len(state.queue) == 0  # nao re-enfileirou


# =============================================================================
# _maybe_requeue_timed_out (async)
# =============================================================================

def test_requeue_timed_out_under_limit():
    """Primeira tentativa de timeout: re-enfileira."""
    import asyncio
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        state = CrawlState(queue=deque(), queued=set(), seen={"url1"})
        logger = logging.getLogger("t")
        asyncio.run(_maybe_requeue_timed_out("url1", state, logger, ck))
        # Deve voltar para a fila
        assert "url1" in state.queued
        assert state.attempts["url1"] == 1


def test_requeue_timed_out_over_limit_records_failure():
    """Apos esgotar tentativas intra-run, registra em failures."""
    import asyncio
    from src.crawler import _INTRA_RUN_RETRY_LIMIT

    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        state = CrawlState(queue=deque(), queued=set(), seen={"url1"})
        # Esgota tentativas
        state.attempts["url1"] = _INTRA_RUN_RETRY_LIMIT - 1
        logger = logging.getLogger("t")
        asyncio.run(_maybe_requeue_timed_out("url1", state, logger, ck))
        # NAO voltou pra fila (excedeu)
        assert "url1" not in state.queued
        # Registrou em failures
        assert "url1" in ck.manifest.failures
