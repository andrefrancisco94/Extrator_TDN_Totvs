"""Helpers compartilhados de Playwright (browser session management).

Antes essas funcoes eram duplicadas em crawler.py e pdf_exporter.py. Centralizar
aqui elimina ~80 linhas duplicadas e garante que crawler/export usem o mesmo
comportamento (anti-fingerprinting, storage_state, recovery).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .utils import build_browser_context_args

_MAX_BROWSER_LAUNCH_ATTEMPTS = 3


@dataclass
class BrowserSession:
    """Container mutavel para browser/context/page (permite recovery in-place)."""
    browser: object = None
    context: object = None
    page: Page | None = None
    state_path: Path | None = None


async def new_page(context, timeout_ms: int) -> Page:
    """Cria uma nova page e configura timeout default."""
    page = await context.new_page()
    page.set_default_timeout(timeout_ms)
    return page


async def safe_close_page(page: Page | None) -> None:
    """Fecha page sem levantar excecao (idempotente)."""
    if page is None:
        return
    try:
        if not page.is_closed():
            await page.close()
    except PlaywrightError:
        pass


def is_browser_alive(session: BrowserSession) -> bool:
    """Wrapper safe para is_connected (pode levantar se processo ja morreu)."""
    if session.browser is None:
        return False
    try:
        return bool(session.browser.is_connected())
    except (PlaywrightError, Exception):  # noqa: BLE001 - defensive
        return False


async def launch_browser_session(
    playwright, headless: bool, timeout_ms: int, logger=None,
    state_path: Path | None = None, proxy: str | None = None,
) -> BrowserSession:
    """Lança browser com retry. Usa UA realista, viewport variavel, storage_state.

    Levanta RuntimeError se nao conseguir lancar apos _MAX_BROWSER_LAUNCH_ATTEMPTS.
    """
    last_error: Exception | None = None
    context_args = build_browser_context_args(state_path=state_path, proxy_url=proxy)
    for attempt in range(1, _MAX_BROWSER_LAUNCH_ATTEMPTS + 1):
        try:
            browser = await playwright.chromium.launch(
                headless=headless, channel="chromium",
            )
            context = await browser.new_context(**context_args)
            page = await new_page(context, timeout_ms)
            return BrowserSession(
                browser=browser, context=context, page=page, state_path=state_path,
            )
        except PlaywrightError as exc:
            last_error = exc
            if logger is not None:
                logger.warning(
                    "Falha ao lancar browser (tentativa %d/%d): %s",
                    attempt, _MAX_BROWSER_LAUNCH_ATTEMPTS, exc,
                )
    raise RuntimeError(
        "Nao foi possivel iniciar o navegador apos "
        f"{_MAX_BROWSER_LAUNCH_ATTEMPTS} tentativas. "
        "Verifique se o Chromium esta instalado (rode 'playwright install chromium' "
        f"ou use a opcao [6] do menu). Ultimo erro: {last_error}"
    )


async def save_storage_state(session: BrowserSession, logger) -> None:
    """Salva cookies/storage no caminho configurado (se houver)."""
    if session.state_path is None or session.context is None:
        return
    try:
        session.state_path.parent.mkdir(parents=True, exist_ok=True)
        await session.context.storage_state(path=str(session.state_path))
        logger.debug("Storage state salvo em %s", session.state_path)
    except (PlaywrightError, OSError) as exc:
        logger.warning("Falha ao salvar storage_state: %s", exc)


async def teardown_session(session: BrowserSession, logger) -> None:
    """Encerra context + browser de forma segura. Salva storage state antes."""
    await save_storage_state(session, logger)
    if session.context is not None:
        try:
            await session.context.close()
        except PlaywrightError as exc:
            logger.warning("Falha ao fechar context: %s", exc)
    if session.browser is not None:
        try:
            await session.browser.close()
        except PlaywrightError as exc:
            logger.warning("Falha ao fechar browser: %s", exc)
