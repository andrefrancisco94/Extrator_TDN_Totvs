from __future__ import annotations

import asyncio
import os
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
    MAX_RETRY_ATTEMPTS,
    RateLimitConfig,
    RateLimiter,
    SlowPageRecord,
    build_browser_context_args,
    build_pdf_file_name,
    count_pdf_pages,
    ensure_output_dirs,
    get_console,
    is_blocking_error,
    is_cloudflare_challenge,
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


class CloudflareChallengeError(RuntimeError):
    """Pagina retornou desafio do Cloudflare (anti-bot)."""


@dataclass
class ExportConfig:
    dom_budget_ms: int
    timeout_ms: int
    slow_threshold_seconds: float
    headless: bool
    # Se True, regenera PDFs mesmo que ja existam no checkpoint.
    force_reexport: bool = False
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    max_workers: int = 1
    state_path: Path | None = None
    proxy: str | None = None


@dataclass
class ExportAccumulators:
    pages_dir: Path
    checkpoint: Checkpoint
    used_names: set[str] = field(default_factory=set)
    # (path, title)
    exported: list[tuple[Path, str]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    slow_records: list[SlowPageRecord] = field(default_factory=list)
    # Lock para mutacoes em paralelo (used_names, exported, failures, slow_records).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Circuit breaker: conta failures consecutivos (login/CF) para abortar
    # quando a sessao/proteção fica em estado ruim e nao adianta seguir.
    consecutive_session_failures: int = 0
    abort_requested: bool = False


# Apos N failures consecutivos do tipo login ou CF challenge, aborta o pipeline.
_SESSION_FAILURE_CIRCUIT_BREAKER = 5


@dataclass
class BrowserSession:
    """Container mutavel para browser/context/page (permite recovery in-place)."""
    browser: object = None
    context: object = None
    page: Page | None = None
    state_path: Path | None = None


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


async def _is_cf_challenge_page(page: Page) -> bool:
    """Verifica se a pagina retornou desafio do Cloudflare."""
    try:
        content = await page.content()
    except PlaywrightError:
        return False
    return is_cloudflare_challenge(content.lower())


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
    content_budget = max(5_000, min(int(timeout_ms - elapsed_ms), 120_000))

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


async def _validate_generated_pdf(pdf_path: Path, max_retries: int = 8) -> int:
    """Valida PDF recem-gerado e retorna size_bytes. Retry pra contornar antivirus lock.

    Espera ate ~3.6s no total (8 tentativas: 0, 0.2, 0.4, ... 1.4s). Cobre o
    cenario de antivirus corporativo escaneando PDFs grandes apos write.
    Alem de header/EOF/tamanho minimo, valida que tem pelo menos 1 pagina
    (descarta PDFs corrompidos que passariam na validacao basica).
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
            if not is_valid_pdf(pdf_path):
                last_error = f"estrutura PDF invalida (size={size} bytes)"
                continue
            # Valida que tem pelo menos 1 pagina (evita PDF corrompido)
            page_count = count_pdf_pages(pdf_path)
            if page_count == 0:
                last_error = f"PDF sem paginas legiveis (size={size} bytes)"
                continue
            return size
        except OSError as exc:
            last_error = f"erro de IO ({exc})"
    raise RuntimeError(f"PDF gerado nao e valido: {last_error}")


async def _allocate_filename(
    index: int, title: str, acc: ExportAccumulators,
) -> str:
    """Aloca nome de arquivo unico de forma thread-safe (com lock)."""
    async with acc.lock:
        file_name = build_pdf_file_name(index, title, acc.used_names)
        acc.used_names.add(file_name)
    return file_name


async def _record_export_result(
    url: str, pdf_path: Path, title: str, file_name: str,
    size_bytes: int, elapsed_total: float, acc: ExportAccumulators, cfg: ExportConfig,
    logger,
) -> None:
    """Registra um PDF exportado com sucesso (thread-safe)."""
    async with acc.lock:
        acc.exported.append((pdf_path, title))
    acc.checkpoint.record_export(
        url=url, filename=file_name, title=title,
        size_bytes=size_bytes, elapsed_seconds=elapsed_total,
    )
    if elapsed_total >= cfg.slow_threshold_seconds:
        async with acc.lock:
            acc.slow_records.append(
                SlowPageRecord(url=url, elapsed_seconds=elapsed_total, phase="pdf")
            )
        logger.warning("Pagina lenta na exportacao (%.1fs): %s", elapsed_total, url)


async def _record_export_failure(
    url: str, error: str, acc: ExportAccumulators, phase: str | None = None,
    elapsed: float = 0.0,
) -> None:
    """Registra falha de export (thread-safe)."""
    async with acc.lock:
        acc.failures.append((url, error))
        if phase:
            acc.slow_records.append(
                SlowPageRecord(url=url, elapsed_seconds=elapsed, phase=phase)
            )
    acc.checkpoint.record_failure(url, error)


def _bump_session_failures(acc: ExportAccumulators, logger) -> None:
    """Incrementa contador de falhas de sessao (login/CF) e aciona circuit breaker."""
    acc.consecutive_session_failures += 1
    if acc.consecutive_session_failures >= _SESSION_FAILURE_CIRCUIT_BREAKER:
        if not acc.abort_requested:
            logger.error(
                "Circuit breaker: %d falhas consecutivas de sessao (login/CF). "
                "Abortando export. Resolva o problema e rode novamente — o "
                "resume retomara as URLs restantes.",
                acc.consecutive_session_failures,
            )
        acc.abort_requested = True


async def _generate_pdf(
    page: Page, pdf_path: Path,
) -> int:
    """Gera o PDF da pagina atual e valida o arquivo gerado. Retorna size_bytes.

    Escreve em arquivo temporario primeiro e so renomeia para o destino final
    se a validacao passar. Isto evita que Ctrl+C/crash no meio da geracao
    deixe um PDF parcial que parece valido (header+footer ok, miolo truncado).
    """
    tmp_path = pdf_path.with_suffix(pdf_path.suffix + ".tmp")
    # Limpa tmp residual de execucao anterior interrompida
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except OSError:
        pass
    try:
        await page.pdf(
            path=str(tmp_path),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            margin={"top": "12mm", "right": "10mm", "bottom": "12mm", "left": "10mm"},
        )
        size_bytes = await _validate_generated_pdf(tmp_path)
        # Rename atomico apenas se valido
        os.replace(str(tmp_path), str(pdf_path))
        return size_bytes
    except BaseException:
        # Inclui KeyboardInterrupt — limpa tmp em qualquer interrupcao
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


async def _export_one(
    page: Page,
    context,
    url: str,
    index: int,
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    limiter: RateLimiter,
) -> Page:
    """Exporta uma URL. Retorna a page (possivelmente recriada). Erros vao para acc.failures."""
    page_start = time.monotonic()
    try:
        await _goto_dom_ready(page, url, cfg.dom_budget_ms)

        # Detecta Cloudflare challenge antes de checar login (CF intercepta tudo)
        if await _is_cf_challenge_page(page):
            raise CloudflareChallengeError(
                "Cloudflare challenge detectado — provavel rate limit"
            )

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

        file_name = await _allocate_filename(index, title, acc)
        pdf_path = acc.pages_dir / file_name
        size_bytes = await _generate_pdf(page, pdf_path)
        elapsed_total = time.monotonic() - page_start

        await _record_export_result(
            url, pdf_path, title, file_name, size_bytes, elapsed_total, acc, cfg, logger,
        )
        limiter.report_success()
        acc.consecutive_session_failures = 0  # sucesso reseta circuit breaker
        logger.info(
            "PDF gerado (%s/%s) em %.1fs (%.1f KB): %s",
            index, total, elapsed_total, size_bytes / 1024, file_name,
        )
        return page

    except LoginPageError as exc:
        logger.error("Login detectado em %s: %s", url, exc)
        await _record_export_failure(url, "Pagina de login (Confluence privado)", acc)
        _bump_session_failures(acc, logger)
        return page

    except CloudflareChallengeError as exc:
        elapsed = time.monotonic() - page_start
        logger.error("Cloudflare challenge em %s: %s", url, exc)
        await _record_export_failure(
            url, "Cloudflare challenge (anti-bot ativo)", acc,
            phase="pdf-cf-challenge", elapsed=elapsed,
        )
        # CF challenge = sinal forte de bloqueio. Aciona cooldown longo.
        await limiter.report_block(logger, "Cloudflare challenge")
        _bump_session_failures(acc, logger)
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)

    except PlaywrightTimeoutError:
        elapsed = time.monotonic() - page_start
        msg = f"TIMEOUT apos {elapsed:.1f}s (limite {cfg.timeout_ms/1000:.0f}s)"
        logger.error("%s: %s", msg, url)
        await _record_export_failure(url, msg, acc, phase="pdf-timeout", elapsed=elapsed)
        await limiter.report_block(logger, "timeout no PDF")
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)

    except PlaywrightError as exc:
        elapsed = time.monotonic() - page_start
        logger.error("Erro Playwright em %s apos %.1fs: %s", url, elapsed, exc)
        await _record_export_failure(url, str(exc), acc)
        if is_blocking_error(exc):
            await limiter.report_block(logger, f"Playwright: {exc}")
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)

    except TransientHTTPError as exc:
        elapsed = time.monotonic() - page_start
        logger.error("HTTP transitorio em %s apos %.1fs: %s", url, elapsed, exc)
        await _record_export_failure(url, str(exc), acc)
        await limiter.report_block(logger, str(exc))
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)

    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - page_start
        logger.exception("Erro ao gerar PDF de %s apos %.1fs", url, elapsed)
        await _record_export_failure(url, str(exc), acc)
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms)


def _expand_urls_with_retries(
    urls: list[str], checkpoint: Checkpoint, logger,
) -> list[str]:
    """Adiciona URLs com failures ao fim da lista de export para nova tentativa.

    URLs ja em `urls` (mapped_urls) sao priorizadas. URLs com failures que
    ainda tem tentativas disponiveis (< MAX_RETRY_ATTEMPTS) sao incluidas
    no fim para serem re-tentadas — independentemente de force_reexport,
    porque essas URLs nunca tiveram PDF gerado (so falhas).
    """
    pending = checkpoint.urls_pending_retry(MAX_RETRY_ATTEMPTS)
    if not pending:
        return urls
    seen = set(urls)
    extra = [u for u in pending if u not in seen]
    if extra:
        logger.info(
            "Re-tentando %d URL(s) que falharam em runs anteriores "
            "(tentativas < %d).",
            len(extra), MAX_RETRY_ATTEMPTS,
        )
    return urls + extra


async def export_pages_to_pdf(
    urls: list[str],
    output_dir: Path,
    checkpoint: Checkpoint,
    logger,
    headless: bool = True,
    timeout_ms: int = 120_000,
    slow_threshold_seconds: float = 60.0,
    force_reexport: bool = False,
    rate_limit: RateLimitConfig | None = None,
    max_workers: int = 1,
    limiter: RateLimiter | None = None,
    state_path: Path | None = None,
    proxy: str | None = None,
) -> tuple[list[tuple[Path, str]], list[tuple[str, str]], list[SlowPageRecord]]:
    """Exporta URLs para PDF. Retorna (exported_entries, failures, slow_records).

    exported_entries: list of (path, title) tuples (para passar ao merger).
    Usa checkpoint para skip de URLs ja exportadas em runs anteriores.
    URLs com failures de runs anteriores sao re-tentadas (ate MAX_RETRY_ATTEMPTS).
    """
    pages_dir, _ = ensure_output_dirs(output_dir)
    # DOM tem ate metade do orcamento, capado em 120s (paginas TDN pesadas).
    dom_budget_ms = min(120_000, timeout_ms // 2)
    console = get_console()

    cfg = ExportConfig(
        dom_budget_ms=dom_budget_ms,
        timeout_ms=timeout_ms,
        slow_threshold_seconds=slow_threshold_seconds,
        headless=headless,
        force_reexport=force_reexport,
        rate_limit=rate_limit or RateLimitConfig(),
        max_workers=max(1, max_workers),
        state_path=state_path,
        proxy=proxy,
    )
    acc = ExportAccumulators(pages_dir=pages_dir, checkpoint=checkpoint)

    # Inclui URLs com failures pendentes para nova tentativa
    urls = _expand_urls_with_retries(urls, checkpoint, logger)
    total = len(urls)

    logger.info(
        "Exportador PDF: %.0fs total/pagina | %.0fs DOM | slow >= %.0fs",
        timeout_ms / 1000, dom_budget_ms / 1000, slow_threshold_seconds,
    )
    logger.info(
        "Rate limit: %.1fs entre requests | cooldown inicial %.0fs | workers=%d",
        cfg.rate_limit.base_delay_seconds,
        cfg.rate_limit.backoff_initial_seconds,
        cfg.max_workers,
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

    # Reutiliza limiter se fornecido (preserva cooldown de fase anterior).
    active_limiter = limiter if limiter is not None else RateLimiter(cfg.rate_limit)
    limiter = active_limiter  # alias para nao quebrar usos internos abaixo

    async with async_playwright() as playwright:
        if cfg.max_workers > 1:
            await _run_parallel(
                playwright, urls, total, acc, cfg, logger, console, limiter,
            )
        else:
            session = await _launch_browser_session(
                playwright, headless, timeout_ms, logger,
                state_path=state_path, proxy=proxy,
            )
            try:
                await _run_export_loop(
                    session, urls, total, playwright, acc, cfg, logger, console, limiter,
                )
                await _safe_close_page(session.page)
            finally:
                await _teardown_session(session, logger)

    return acc.exported, acc.failures, acc.slow_records


async def _launch_browser_session(
    playwright, headless: bool, timeout_ms: int, logger=None,
    state_path: Path | None = None, proxy: str | None = None,
) -> BrowserSession:
    """Lança browser com retry. Usa UA realista, viewport variavel, storage_state."""
    last_error: Exception | None = None
    context_args = build_browser_context_args(state_path=state_path, proxy_url=proxy)
    for attempt in range(1, _MAX_BROWSER_LAUNCH_ATTEMPTS + 1):
        try:
            browser = await playwright.chromium.launch(
                headless=headless, channel="chromium",
            )
            context = await browser.new_context(**context_args)
            page = await _new_page(context, timeout_ms)
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


def _is_browser_alive(session: BrowserSession) -> bool:
    if session.browser is None:
        return False
    try:
        return bool(session.browser.is_connected())
    except (PlaywrightError, Exception):  # noqa: BLE001 - defensive
        return False


async def _save_storage_state(session: BrowserSession, logger) -> None:
    """Salva cookies/storage no caminho configurado (se houver)."""
    if session.state_path is None or session.context is None:
        return
    try:
        session.state_path.parent.mkdir(parents=True, exist_ok=True)
        await session.context.storage_state(path=str(session.state_path))
        logger.debug("Storage state salvo em %s", session.state_path)
    except (PlaywrightError, OSError) as exc:
        logger.warning("Falha ao salvar storage_state: %s", exc)


async def _teardown_session(session: BrowserSession, logger) -> None:
    await _save_storage_state(session, logger)
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
    limiter: RateLimiter,
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
            if acc.abort_requested:
                logger.warning("Export interrompido por circuit breaker.")
                break
            await _process_export_iteration(
                session, url, index, total, playwright, acc, cfg, logger,
                progress, task, limiter,
            )
            progress.advance(task)


async def _run_parallel(
    playwright,
    urls: list[str],
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    console,
    limiter: RateLimiter,
) -> None:
    """Paralelizacao com N workers, cada um com sua propria page.

    Compartilham 1 browser + 1 context para manter cookies/sessao. Cada worker
    consome URLs de uma fila comum. O limiter serializa o intervalo entre
    requests entre todos os workers.
    """
    session = await _launch_browser_session(
        playwright, cfg.headless, cfg.timeout_ms, logger,
        state_path=cfg.state_path, proxy=cfg.proxy,
    )
    queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
    for idx, url in enumerate(urls, start=1):
        queue.put_nowait((idx, url))
    for _ in range(cfg.max_workers):
        queue.put_nowait(None)  # sentinela para cada worker

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
    try:
        with progress:
            task = progress.add_task(
                f"Gerando PDFs (workers={cfg.max_workers})", total=total,
            )
            workers = [
                asyncio.create_task(
                    _worker_loop(
                        wid, session.context, queue, total,
                        acc, cfg, logger, limiter, progress, task,
                    )
                )
                for wid in range(cfg.max_workers)
            ]
            await asyncio.gather(*workers)
    finally:
        await _teardown_session(session, logger)


async def _worker_loop(
    worker_id: int,
    context,
    queue: asyncio.Queue,
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    limiter: RateLimiter,
    progress,
    task,
) -> None:
    """Loop de um worker paralelo: pega URL da fila, exporta, repete."""
    page = await _new_page(context, cfg.timeout_ms)
    processed = 0
    try:
        while True:
            if acc.abort_requested:
                break
            item = await queue.get()
            if item is None:
                break
            index, url = item
            # _process_worker_item retorna a page atual (possivelmente nova
            # se houve erro/timeout em _export_one). Propagar para a proxima
            # iteracao senao a page recriada nunca eh usada.
            page = await _process_worker_item(
                page, context, url, index, total, acc, cfg, logger,
                progress, task, limiter, worker_id,
            )
            progress.advance(task)
            processed += 1
            # Rotacao periodica para liberar memoria
            if processed % _PAGE_ROTATION_INTERVAL == 0:
                logger.info(
                    "Worker %d: rotacionando page apos %d URLs",
                    worker_id, processed,
                )
                await _safe_close_page(page)
                page = await _new_page(context, cfg.timeout_ms)
    finally:
        await _safe_close_page(page)


async def _process_worker_item(
    page: Page,
    context,
    url: str,
    index: int,
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    progress,
    task,
    limiter: RateLimiter,
    worker_id: int,
) -> Page:
    """Processa uma URL no worker. Retorna a page atual (possivelmente nova)."""
    short_url = url if len(url) <= 50 else url[:47] + "..."

    if not cfg.force_reexport and acc.checkpoint.is_exported(url, acc.pages_dir):
        await _resume_existing(url, short_url, index, total, acc, progress, task)
        return page

    progress.update(
        task,
        description=f"W{worker_id} {index}/{total}: {short_url}",
    )
    await limiter.wait()
    return await _export_one(
        page, context, url, index, total, acc, cfg, logger, limiter,
    )


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
    limiter: RateLimiter,
) -> None:
    """Processa uma URL: resume, recovery de browser, rotacao, export."""
    short_url = url if len(url) <= 60 else url[:57] + "..."

    if not cfg.force_reexport and acc.checkpoint.is_exported(url, acc.pages_dir):
        await _resume_existing(url, short_url, index, total, acc, progress, task)
        return

    progress.update(task, description=f"PDF {index}/{total}: {short_url}")

    await _ensure_browser_alive(session, playwright, cfg, logger)
    await _maybe_rotate_page(session, index, cfg, logger)
    await limiter.wait()

    session.page = await _export_one(
        session.page, session.context, url, index, total, acc, cfg, logger, limiter,
    )


async def _resume_existing(
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
    async with acc.lock:
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
        state_path=session.state_path, proxy=cfg.proxy,
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
