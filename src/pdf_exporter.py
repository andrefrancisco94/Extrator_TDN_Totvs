from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .utils import (
    Checkpoint,
    SlowPageRecord,
    build_pdf_file_name,
    ensure_output_dirs,
    get_console,
    is_valid_pdf,
)

_CONTENT_SELECTORS = (
    "#main-content",
    "#content",
    ".wiki-content",
    "article",
    "main",
)

_PAGE_ROTATION_INTERVAL = 50

# Maximo de tentativas de relancar o browser antes de abortar.
_MAX_BROWSER_LAUNCH_ATTEMPTS = 3

# Score minimo para considerar pagina como tela de login (multi-indicador).
_LOGIN_SCORE_THRESHOLD = 2

_LOGIN_INDICATORS = (
    "#login-form",
    "#loginButton",
    "form[name='loginform']",
    "input[name='os_username']",
    "input[name='os_password']",
    "input#username",
    "input#email",
)

_LOGIN_TITLE_TOKENS = ("log in", "login", "sign in", "entrar")


_REVEAL_CSS = """
    .expand-content,
    .expand-container .expand-content { display: block !important; }
    .expand-container { border-left: 2px solid #ccc; padding-left: 8px; }

    .aui-tabs .tabs-pane,
    .aui-tabs .tab-pane,
    .tabs-pane,
    .tab-pane,
    [role="tabpanel"],
    .adf-tab-panel,
    .rw-ui-tabs__panel {
        display: block !important;
        visibility: visible !important;
        height: auto !important;
        max-height: none !important;
        overflow: visible !important;
    }
    .aui-tabs .tabs-menu,
    [role="tablist"] { display: none !important; }

    details { open: true; }
    details > * { display: block !important; }
"""


_REVEAL_JS = """
() => {
  document.querySelectorAll('.expand-content').forEach(el => {
    el.style.display = 'block';
  });
  document.querySelectorAll('.expand-container').forEach(el => {
    el.classList.add('expanded');
  });

  document.querySelectorAll('.aui-tabs').forEach(tabsRoot => {
    const tabs = Array.from(
      tabsRoot.querySelectorAll('.tabs-menu .menu-item, .tabs-menu li')
    );
    const panes = Array.from(tabsRoot.querySelectorAll('.tabs-pane, .tab-pane'));
    panes.forEach((pane, i) => {
      pane.style.display = 'block';
      const label = (tabs[i]?.textContent || '').trim();
      if (label && !pane.dataset.tabLabelInjected) {
        const h = document.createElement('h4');
        h.style.cssText =
          'border-left:4px solid #0052cc;padding:6px 10px;margin:16px 0 8px;' +
          'background:#f4f5f7;color:#0052cc;font-weight:bold;';
        h.textContent = 'Aba: ' + label;
        pane.insertBefore(h, pane.firstChild);
        pane.dataset.tabLabelInjected = '1';
      }
    });
  });

  const tabLists = document.querySelectorAll('[role="tablist"]');
  tabLists.forEach(list => {
    const tabs = Array.from(list.querySelectorAll('[role="tab"]'));
    const labelMap = new Map();
    tabs.forEach(t => {
      const id = t.getAttribute('aria-controls');
      if (id) labelMap.set(id, (t.textContent || '').trim());
    });
    document.querySelectorAll('[role="tabpanel"]').forEach(panel => {
      panel.style.display = 'block';
      panel.removeAttribute('hidden');
      const label = labelMap.get(panel.id);
      if (label && !panel.dataset.tabLabelInjected) {
        const h = document.createElement('h4');
        h.style.cssText =
          'border-left:4px solid #0052cc;padding:6px 10px;margin:16px 0 8px;' +
          'background:#f4f5f7;color:#0052cc;font-weight:bold;';
        h.textContent = 'Aba: ' + label;
        panel.insertBefore(h, panel.firstChild);
        panel.dataset.tabLabelInjected = '1';
      }
    });
  });

  document.querySelectorAll('details').forEach(d => d.open = true);

  document.querySelectorAll(
    '.expand-control, .aui-expander-trigger, [data-expand-trigger]'
  ).forEach(el => {
    try { el.click(); } catch (e) {}
  });
}
"""


class TransientHTTPError(RuntimeError):
    """HTTP 5xx que merece retry."""


class LoginPageError(RuntimeError):
    """Pagina retornou tela de login (Confluence privado / sessao expirada)."""


@dataclass
class ExportConfig:
    dom_budget_ms: int
    timeout_ms: int
    slow_threshold_seconds: float
    headless: bool
    # Se True, regenera PDFs mesmo que ja existam no checkpoint.
    force_reexport: bool = False


@dataclass
class ExportAccumulators:
    pages_dir: Path
    checkpoint: Checkpoint
    used_names: set[str] = field(default_factory=set)
    # (path, title)
    exported: list[tuple[Path, str]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    slow_records: list[SlowPageRecord] = field(default_factory=list)


@dataclass
class BrowserSession:
    """Container mutavel para browser/context/page (permite recovery in-place)."""
    browser: object = None
    context: object = None
    page: Page | None = None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (PlaywrightTimeoutError, LoginPageError)):
        return False
    if isinstance(exc, TransientHTTPError):
        return True
    return isinstance(exc, PlaywrightError)


async def _new_page(context, timeout_ms: int) -> Page:
    page = await context.new_page()
    page.set_default_timeout(timeout_ms)
    return page


async def _safe_close_page(page: Page | None) -> None:
    if page is None:
        return
    try:
        if not page.is_closed():
            await page.close()
    except PlaywrightError:
        pass


async def _is_login_page(page: Page) -> bool:
    """Detecta se Confluence redirecionou para tela de login.

    Usa multi-indicador para reduzir falsos positivos:
      * URL com /login.action vale 2 pontos (sinal forte)
      * Form de login no DOM vale 2 pontos
      * Titulo com "log in" vale 1 ponto (fraco - pode ser conteudo legitimo)
    Score >= _LOGIN_SCORE_THRESHOLD marca como login.
    """
    score = 0
    try:
        url = (page.url or "").lower()
        if "/login.action" in url or "loginpage" in url or "/dologin" in url:
            score += 2
    except PlaywrightError:
        pass

    if score >= _LOGIN_SCORE_THRESHOLD:
        return True

    for sel in _LOGIN_INDICATORS:
        try:
            if await page.locator(sel).count() > 0:
                score += 2
                break
        except PlaywrightError:
            continue

    if score >= _LOGIN_SCORE_THRESHOLD:
        return True

    try:
        title = (await page.title() or "").lower()
        if any(tok in title for tok in _LOGIN_TITLE_TOKENS):
            score += 1
    except PlaywrightError:
        pass

    return score >= _LOGIN_SCORE_THRESHOLD


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def _goto_dom_ready(page: Page, url: str, timeout_ms: int) -> None:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if response and response.status >= 500:
        raise TransientHTTPError(f"HTTP {response.status}")
    if response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}")


async def _prepare_page_for_pdf(
    page: Page, page_start: float, timeout_ms: int, logger, url: str,
) -> None:
    elapsed_ms = (time.monotonic() - page_start) * 1000
    content_budget = max(5_000, min(int(timeout_ms - elapsed_ms), 60_000))

    content_sel = ",".join(_CONTENT_SELECTORS)
    try:
        await page.wait_for_selector(content_sel, timeout=content_budget)
    except PlaywrightError:
        logger.warning("Conteudo principal nao detectado em %s (PDF pode ficar vazio)", url)

    try:
        await page.add_style_tag(content=_REVEAL_CSS)
        await page.evaluate(_REVEAL_JS)
    except PlaywrightError as exc:
        logger.warning("Falha ao expandir abas/secoes em %s: %s", url, exc)

    elapsed_ms = (time.monotonic() - page_start) * 1000
    remaining_ms = int(timeout_ms - elapsed_ms)
    if remaining_ms > 2_000:
        image_budget = min(remaining_ms, 20_000)
        try:
            await page.wait_for_load_state("load", timeout=image_budget)
        except PlaywrightError:
            pass

    try:
        await page.emulate_media(media="print")
    except PlaywrightError:
        pass


async def _validate_generated_pdf(pdf_path: Path, max_retries: int = 3) -> int:
    """Valida PDF recem-gerado e retorna size_bytes. Retry pra contornar antivirus lock.

    Levanta RuntimeError se apos os retries o PDF nao for valido.
    """
    last_error = "PDF nao encontrado"
    for attempt in range(max_retries):
        if attempt > 0:
            await asyncio.sleep(0.2 * attempt)
        try:
            if not pdf_path.exists():
                last_error = "arquivo nao foi criado"
                continue
            size = pdf_path.stat().st_size
            if size == 0:
                last_error = "arquivo com 0 bytes"
                continue
            if is_valid_pdf(pdf_path):
                return size
            last_error = f"estrutura PDF invalida (size={size} bytes)"
        except OSError as exc:
            last_error = f"erro de IO ({exc})"
    raise RuntimeError(f"PDF gerado nao e valido: {last_error}")


async def _export_one(
    page: Page,
    context,
    url: str,
    index: int,
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
) -> Page:
    """Exporta uma URL. Retorna a page (possivelmente recriada). Erros vao para acc.failures."""
    page_start = time.monotonic()
    try:
        await _goto_dom_ready(page, url, cfg.dom_budget_ms)

        if await _is_login_page(page):
            raise LoginPageError(
                "Pagina retornou tela de login (Confluence privado ou sessao expirada)"
            )

        await _prepare_page_for_pdf(page, page_start, cfg.timeout_ms, logger, url)

        try:
            title = await page.title()
        except PlaywrightError:
            title = ""
        if not title:
            title = f"pagina-{index}"

        file_name = build_pdf_file_name(index, title, acc.used_names)
        acc.used_names.add(file_name)

        pdf_path = acc.pages_dir / file_name
        await page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "12mm", "right": "10mm", "bottom": "12mm", "left": "10mm"},
        )

        # Antivirus pode segurar arquivo recem-criado momentaneamente.
        # Aceita ate 3 retries com pequenos delays antes de declarar invalido.
        size_bytes = await _validate_generated_pdf(pdf_path)
        elapsed_total = time.monotonic() - page_start

        acc.exported.append((pdf_path, title))
        acc.checkpoint.record_export(
            url=url, filename=file_name, title=title,
            size_bytes=size_bytes, elapsed_seconds=elapsed_total,
        )

        logger.info(
            "PDF gerado (%s/%s) em %.1fs (%.1f KB): %s",
            index, total, elapsed_total, size_bytes / 1024, file_name,
        )

        if elapsed_total >= cfg.slow_threshold_seconds:
            acc.slow_records.append(
                SlowPageRecord(url=url, elapsed_seconds=elapsed_total, phase="pdf")
            )
            logger.warning("Pagina lenta na exportacao (%.1fs): %s", elapsed_total, url)

        return page

    except LoginPageError as exc:
        logger.error("Login detectado em %s: %s", url, exc)
        acc.failures.append((url, "Pagina de login (Confluence privado)"))
        acc.checkpoint.record_failure(url, str(exc))
        return page

    except PlaywrightTimeoutError:
        elapsed = time.monotonic() - page_start
        msg = f"TIMEOUT apos {elapsed:.1f}s (limite {cfg.timeout_ms/1000:.0f}s)"
        logger.error("%s: %s", msg, url)
        acc.failures.append((url, msg))
        acc.slow_records.append(
            SlowPageRecord(url=url, elapsed_seconds=elapsed, phase="pdf-timeout")
        )
        acc.checkpoint.record_failure(url, msg)
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)

    except PlaywrightError as exc:
        elapsed = time.monotonic() - page_start
        logger.error("Erro Playwright em %s apos %.1fs: %s", url, elapsed, exc)
        acc.failures.append((url, str(exc)))
        acc.checkpoint.record_failure(url, str(exc))
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)

    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - page_start
        logger.exception("Erro ao gerar PDF de %s apos %.1fs", url, elapsed)
        acc.failures.append((url, str(exc)))
        acc.checkpoint.record_failure(url, str(exc))
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)


async def export_pages_to_pdf(
    urls: list[str],
    output_dir: Path,
    checkpoint: Checkpoint,
    logger,
    headless: bool = True,
    timeout_ms: int = 120_000,
    slow_threshold_seconds: float = 60.0,
    force_reexport: bool = False,
) -> tuple[list[tuple[Path, str]], list[tuple[str, str]], list[SlowPageRecord]]:
    """Exporta URLs para PDF. Retorna (exported_entries, failures, slow_records).

    exported_entries: list of (path, title) tuples (para passar ao merger).
    Usa checkpoint para skip de URLs ja exportadas em runs anteriores.
    """
    pages_dir, _ = ensure_output_dirs(output_dir)
    total = len(urls)
    # DOM tem ate metade do orcamento, capado em 60s.
    dom_budget_ms = min(60_000, timeout_ms // 2)
    console = get_console()

    cfg = ExportConfig(
        dom_budget_ms=dom_budget_ms,
        timeout_ms=timeout_ms,
        slow_threshold_seconds=slow_threshold_seconds,
        headless=headless,
        force_reexport=force_reexport,
    )
    acc = ExportAccumulators(pages_dir=pages_dir, checkpoint=checkpoint)

    logger.info(
        "Exportador PDF: %.0fs total/pagina | %.0fs DOM | slow >= %.0fs",
        timeout_ms / 1000, dom_budget_ms / 1000, slow_threshold_seconds,
    )

    if force_reexport:
        logger.info("Re-export forcado: PDFs existentes serao regenerados.")
    else:
        skip_count = sum(1 for u in urls if checkpoint.is_exported(u, pages_dir))
        if skip_count:
            logger.info(
                "Resume: %d PDFs ja existem do run anterior, serao reutilizados.",
                skip_count,
            )

    async with async_playwright() as playwright:
        session = await _launch_browser_session(playwright, headless, timeout_ms, logger)
        try:
            await _run_export_loop(session, urls, total, playwright, acc, cfg, logger, console)
            await _safe_close_page(session.page)
        finally:
            await _teardown_session(session, logger)

    return acc.exported, acc.failures, acc.slow_records


async def _launch_browser_session(
    playwright, headless: bool, timeout_ms: int, logger=None,
) -> BrowserSession:
    """Lança browser com retry; aborta se falhar repetidamente."""
    last_error: Exception | None = None
    for attempt in range(1, _MAX_BROWSER_LAUNCH_ATTEMPTS + 1):
        try:
            browser = await playwright.chromium.launch(
                headless=headless, channel="chromium",
            )
            context = await browser.new_context()
            page = await _new_page(context, timeout_ms)
            return BrowserSession(browser=browser, context=context, page=page)
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


def _is_browser_alive(session: BrowserSession) -> bool:
    if session.browser is None:
        return False
    try:
        return bool(session.browser.is_connected())
    except (PlaywrightError, Exception):  # noqa: BLE001 - defensive
        return False


async def _teardown_session(session: BrowserSession, logger) -> None:
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


async def _run_export_loop(
    session: BrowserSession,
    urls: list[str],
    total: int,
    playwright,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    console,
) -> None:
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold green]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn("|"),
        TimeElapsedColumn(),
        TextColumn("|"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )

    with progress:
        task = progress.add_task("Gerando PDFs", total=total)
        for index, url in enumerate(urls, start=1):
            await _process_export_iteration(
                session, url, index, total, playwright, acc, cfg, logger, progress, task,
            )
            progress.advance(task)


async def _process_export_iteration(
    session: BrowserSession,
    url: str,
    index: int,
    total: int,
    playwright,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    progress,
    task,
) -> None:
    """Processa uma URL: resume, recovery de browser, rotacao, export."""
    short_url = url if len(url) <= 60 else url[:57] + "..."

    if not cfg.force_reexport and acc.checkpoint.is_exported(url, acc.pages_dir):
        _resume_existing(url, short_url, index, total, acc, progress, task)
        return

    progress.update(task, description=f"PDF {index}/{total}: {short_url}")

    await _ensure_browser_alive(session, playwright, cfg, logger)
    await _maybe_rotate_page(session, index, cfg, logger)

    session.page = await _export_one(
        session.page, session.context, url, index, total, acc, cfg, logger,
    )


def _resume_existing(
    url: str,
    short_url: str,
    index: int,
    total: int,
    acc: ExportAccumulators,
    progress,
    task,
) -> None:
    entry = acc.checkpoint.get_exported_entry(url) or {}
    filename = entry.get("filename", "")
    title = entry.get("title", "")
    acc.exported.append((acc.pages_dir / filename, title))
    if filename:
        acc.used_names.add(filename)
    progress.update(
        task, description=f"PDF {index}/{total}: [cyan]resume[/cyan] {short_url}",
    )


async def _ensure_browser_alive(
    session: BrowserSession, playwright, cfg: ExportConfig, logger,
) -> None:
    if _is_browser_alive(session):
        return
    logger.warning("Browser desconectou. Relancando...")
    try:
        if session.browser is not None:
            await session.browser.close()
    except PlaywrightError:
        pass
    new_session = await _launch_browser_session(
        playwright, cfg.headless, cfg.timeout_ms, logger,
    )
    session.browser = new_session.browser
    session.context = new_session.context
    session.page = new_session.page


async def _maybe_rotate_page(
    session: BrowserSession, index: int, cfg: ExportConfig, logger,
) -> None:
    if index <= 1 or (index - 1) % _PAGE_ROTATION_INTERVAL != 0:
        return
    logger.info("Rotacionando pagina apos %s URLs", index - 1)
    await _safe_close_page(session.page)
    session.page = await _new_page(session.context, cfg.timeout_ms)
