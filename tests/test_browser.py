"""Testes do modulo src/browser.py (sem Playwright real)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.browser import (
    BrowserSession,
    is_browser_alive,
    new_page,
    safe_close_page,
    save_storage_state,
    teardown_session,
)


def test_browser_session_defaults():
    """BrowserSession aceita criacao sem args."""
    s = BrowserSession()
    assert s.browser is None
    assert s.context is None
    assert s.page is None
    assert s.state_path is None


def test_browser_session_with_state_path(tmp_path):
    s = BrowserSession(state_path=tmp_path / "state.json")
    assert s.state_path is not None


def test_is_browser_alive_none():
    """Browser None retorna False sem crashar."""
    s = BrowserSession()
    assert is_browser_alive(s) is False


def test_is_browser_alive_dead_browser():
    """Browser que levanta excecao em is_connected retorna False."""
    s = BrowserSession()
    s.browser = MagicMock()
    s.browser.is_connected.side_effect = Exception("morreu")
    assert is_browser_alive(s) is False


def test_is_browser_alive_ok():
    """Browser conectado retorna True."""
    s = BrowserSession()
    s.browser = MagicMock()
    s.browser.is_connected.return_value = True
    assert is_browser_alive(s) is True


def test_safe_close_page_none():
    """Fechar page None nao crasha."""
    asyncio.run(safe_close_page(None))


def test_safe_close_page_already_closed():
    """Fechar page ja fechada nao crasha."""
    page = MagicMock()
    page.is_closed.return_value = True
    asyncio.run(safe_close_page(page))
    page.close.assert_not_called()


def test_safe_close_page_open():
    """Page aberta deve ser fechada."""
    page = MagicMock()
    page.is_closed.return_value = False
    page.close = AsyncMock()
    asyncio.run(safe_close_page(page))
    page.close.assert_awaited_once()


def test_new_page_sets_timeout():
    """new_page configura timeout default."""
    context = MagicMock()
    page = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    result = asyncio.run(new_page(context, 30000))
    assert result is page
    page.set_default_timeout.assert_called_once_with(30000)


def test_save_storage_state_no_path(mock_logger):
    """save sem state_path nao faz nada."""
    s = BrowserSession()
    asyncio.run(save_storage_state(s, mock_logger))
    # Nao crasha


def test_save_storage_state_no_context(mock_logger, tmp_path):
    """save sem context nao faz nada."""
    s = BrowserSession(state_path=tmp_path / "x.json")
    asyncio.run(save_storage_state(s, mock_logger))


def test_save_storage_state_writes(mock_logger, tmp_path):
    """save com state_path + context chama context.storage_state."""
    s = BrowserSession(state_path=tmp_path / "state.json")
    s.context = MagicMock()
    s.context.storage_state = AsyncMock()
    asyncio.run(save_storage_state(s, mock_logger))
    s.context.storage_state.assert_awaited_once()


def test_teardown_session_closes_all(mock_logger):
    """teardown fecha context E browser."""
    s = BrowserSession()
    s.context = MagicMock()
    s.context.close = AsyncMock()
    s.browser = MagicMock()
    s.browser.close = AsyncMock()
    asyncio.run(teardown_session(s, mock_logger))
    s.context.close.assert_awaited_once()
    s.browser.close.assert_awaited_once()


def test_teardown_session_tolerates_close_errors(mock_logger):
    """teardown nao crasha se close levantar PlaywrightError."""
    from playwright.async_api import Error as PWError
    s = BrowserSession()
    s.context = MagicMock()
    s.context.close = AsyncMock(side_effect=PWError("oops"))
    s.browser = MagicMock()
    s.browser.close = AsyncMock(side_effect=PWError("oops"))
    # Nao deve crashar
    asyncio.run(teardown_session(s, mock_logger))
