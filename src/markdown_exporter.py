"""Exportacao de paginas Confluence/TDN para Markdown (com imagens/anexos localizados).

Pipeline sequencial (sem paralelismo) e independente da exportacao em PDF:
usa o namespace `exported_md` do Checkpoint, entao o resume de um formato
nao interfere no do outro — os dois comandos podem compartilhar o mesmo
output_dir e o mesmo crawl (mapped_urls) sem conflito.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify as _html_to_markdown
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

from .blocking import is_transient_network_error, parse_retry_after, short_error
from .browser import (
    BrowserSession,
    launch_browser_session as _launch_browser_session_helper,
    new_page as _new_page,
    note_exit_failure,
    note_page_timeout,
    rotate_exit_after_block,
    safe_close_page as _safe_close_page,
    teardown_session as _teardown_session,
)
from .pdf_exporter import (
    CloudflareChallengeError,
    LoginPageError,
    PageHTTPError,
    RateLimitedError,
    TransientHTTPError,
    TransientNetworkError,
    _CONTENT_SELECTORS,
    _ensure_browser_alive,
    _goto_dom_ready,
    _is_login_page,
    _maybe_rotate_page,
    _prepare_page_for_pdf,
    _verify_page_assets,
)
from .proxy import ProxyPool
from .utils import (
    Checkpoint,
    RateLimitConfig,
    RateLimiter,
    SlowPageRecord,
    atomic_replace_with_retry,
    build_file_name,
    get_console,
    is_blocking_error,
    slugify,
)

_launch_browser_session = _launch_browser_session_helper

_ATTACHMENT_DIRNAME = "attachments"
# Apos N falhas consecutivas de sessao (login/CF), aborta o pipeline.
_SESSION_FAILURE_CIRCUIT_BREAKER = 5
# Protecao contra download de asset gigante (video, zip grande, etc).
_MAX_ASSET_BYTES = 25 * 1024 * 1024
_ASSET_TIMEOUT_MS = 30_000
# Pausa entre downloads de imagens/anexos da mesma pagina: cada um e um
# request a mais no rate limit do servidor.
_ASSET_PAUSE_SECONDS = 0.2

# Tentativas por URL no mesmo run (bloqueio = repete a MESMA pagina apos cooldown).
_MAX_ATTEMPTS_PER_URL = 4
_FINAL_RETRY_ROUNDS = 1

_OK = "ok"
_BLOCKED = "blocked"
_TRANSIENT = "transient"
_FAILED = "failed"
_TIMEOUT = "timeout"      # sem nova tentativa imediata; entra na repescagem
_PERMANENT = "permanent"  # login / HTTP 4xx: repetir agora nao resolve

_ATTACHMENT_HREF_HINTS = (
    "/download/attachments/",
    "/download/thumbnails/",
    "/rest/api/content/",
)
_SKIP_HREF_PREFIXES = ("mailto:", "javascript:", "#")


@dataclass
class MarkdownExportConfig:
    dom_budget_ms: int
    timeout_ms: int
    slow_threshold_seconds: float
    headless: bool
    force_reexport: bool = False
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    state_path: Path | None = None
    proxy: str | None = None
    pool: ProxyPool | None = None


@dataclass
class MarkdownAccumulators:
    md_dir: Path
    attachments_dir: Path
    checkpoint: Checkpoint
    used_names: set[str] = field(default_factory=set)
    # indice na lista de URLs -> (path, title, url): o indice garante a ordem
    # da arvore mesmo quando uma pagina so e gerada na repescagem.
    exported: dict[int, tuple[Path, str, str]] = field(default_factory=dict)
    # url -> ultimo erro (removida se a pagina for gerada numa nova tentativa)
    failures: dict[str, str] = field(default_factory=dict)
    permanent: set[str] = field(default_factory=set)
    names_by_url: dict[str, str] = field(default_factory=dict)
    slow_records: list[SlowPageRecord] = field(default_factory=list)
    consecutive_session_failures: int = 0
    abort_requested: bool = False

    def ordered_exports(self) -> list[tuple[Path, str, str]]:
        return [self.exported[index] for index in sorted(self.exported)]


@dataclass
class _Outcome:
    status: str
    error: str = ""
    retry_after: float | None = None
    phase: str | None = None
    elapsed: float = 0.0
    session_failure: bool = False
    # Erro de conexao na navegacao (pode ser o proxy fora do ar).
    exit_failure: bool = False


async def export_pages_to_markdown(
    urls: list[str],
    output_dir: Path,
    checkpoint: Checkpoint,
    logger,
    headless: bool = True,
    timeout_ms: int = 120_000,
    slow_threshold_seconds: float = 60.0,
    force_reexport: bool = False,
    rate_limit: RateLimitConfig | None = None,
    limiter: RateLimiter | None = None,
    state_path: Path | None = None,
    proxy: str | None = None,
    pool: ProxyPool | None = None,
) -> tuple[list[tuple[Path, str, str]], list[tuple[str, str]], list[SlowPageRecord]]:
    """Exporta URLs para Markdown. Retorna (exported_entries, failures, slow_records).

    exported_entries: list of (path, title, url) tuples, na ordem do crawl
    (usada pelo markdown_merge para montar o indice/hierarquia). Paginas
    bloqueadas sao repetidas no proprio run, apos o cooldown.
    """
    md_dir = output_dir / "markdown" / "pages"
    attachments_dir = output_dir / "markdown" / _ATTACHMENT_DIRNAME
    md_dir.mkdir(parents=True, exist_ok=True)
    attachments_dir.mkdir(parents=True, exist_ok=True)

    checkpoint.reconcile_md_with_disk(md_dir, logger)

    dom_budget_ms = min(120_000, timeout_ms // 2)
    console = get_console()
    cfg = MarkdownExportConfig(
        dom_budget_ms=dom_budget_ms,
        timeout_ms=timeout_ms,
        slow_threshold_seconds=slow_threshold_seconds,
        headless=headless,
        force_reexport=force_reexport,
        rate_limit=rate_limit or RateLimitConfig(),
        state_path=state_path,
        proxy=proxy,
        pool=pool,
    )
    acc = MarkdownAccumulators(
        md_dir=md_dir, attachments_dir=attachments_dir, checkpoint=checkpoint,
    )
    total = len(urls)

    logger.info(
        "Exportador Markdown: %.0fs total/pagina | %.0fs DOM | slow >= %.0fs",
        timeout_ms / 1000, dom_budget_ms / 1000, slow_threshold_seconds,
    )

    if force_reexport:
        logger.info("Re-export Markdown forcado: arquivos existentes serao regenerados.")
    else:
        skip_count = sum(1 for u in urls if checkpoint.is_exported_md(u, md_dir))
        if skip_count:
            logger.info(
                "Resume: %d paginas Markdown ja existem do run anterior, serao reutilizadas.",
                skip_count,
            )

    active_limiter = limiter if limiter is not None else RateLimiter(cfg.rate_limit, logger)

    try:
        async with async_playwright() as playwright:
            session = await _launch_browser_session(
                playwright, headless, timeout_ms, logger,
                state_path=state_path, proxy=proxy, pool=pool, block_extras=True,
            )
            try:
                await _run_markdown_export_loop(
                    session, urls, total, playwright, acc, cfg, logger, console,
                    active_limiter,
                )
                await _safe_close_page(session.page)
            finally:
                await _teardown_session(session, logger)
    finally:
        try:
            checkpoint.flush()
        except OSError as exc:
            logger.warning("Falha ao gravar manifest ao final do export: %s", exc)

    return acc.ordered_exports(), list(acc.failures.items()), acc.slow_records


async def _run_markdown_export_loop(
    session: BrowserSession,
    urls: list[str],
    total: int,
    playwright,
    acc: MarkdownAccumulators,
    cfg: MarkdownExportConfig,
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
    items = list(enumerate(urls, start=1))
    with progress:
        task = progress.add_task("Gerando Markdown", total=total)
        for index, url in items:
            if acc.abort_requested:
                logger.warning("Export Markdown interrompido por circuit breaker.")
                break
            await _process_markdown_iteration(
                session, url, index, total, playwright, acc, cfg, logger, progress, task,
                limiter,
            )
            progress.advance(task)

        for round_number in range(1, _FINAL_RETRY_ROUNDS + 1):
            pending = [
                (index, url) for index, url in items
                if url in acc.failures and url not in acc.permanent
            ]
            if not pending or acc.abort_requested:
                break
            logger.info(
                "Repescagem %d/%d: %d pagina(s) Markdown que falharam serao tentadas de novo.",
                round_number, _FINAL_RETRY_ROUNDS, len(pending),
            )
            retry_task = progress.add_task(f"Repescagem {round_number}", total=len(pending))
            for index, url in pending:
                if acc.abort_requested:
                    break
                await _process_markdown_iteration(
                    session, url, index, total, playwright, acc, cfg, logger, progress,
                    retry_task, limiter, max_attempts=2,
                )
                progress.advance(retry_task)


async def _process_markdown_iteration(
    session: BrowserSession,
    url: str,
    index: int,
    total: int,
    playwright,
    acc: MarkdownAccumulators,
    cfg: MarkdownExportConfig,
    logger,
    progress,
    task,
    limiter: RateLimiter,
    max_attempts: int = _MAX_ATTEMPTS_PER_URL,
) -> None:
    short_url = url if len(url) <= 60 else url[:57] + "..."

    if not cfg.force_reexport and acc.checkpoint.is_exported_md(url, acc.md_dir):
        entry = acc.checkpoint.manifest.exported_md.get(url) or {}
        filename = entry.get("filename", "")
        title = entry.get("title", "")
        acc.exported[index] = (acc.md_dir / filename, title, url)
        if filename:
            acc.used_names.add(filename)
        progress.update(
            task, description=f"MD {index}/{total}: [cyan]resume[/cyan] {short_url}",
        )
        return

    progress.update(task, description=f"MD {index}/{total}: {short_url}")

    outcome = _Outcome(_FAILED, "nao processada")
    for attempt in range(1, max_attempts + 1):
        if acc.abort_requested:
            return
        await _ensure_browser_alive(session, playwright, cfg, logger)
        if attempt == 1:
            await _maybe_rotate_page(session, index, cfg, logger)
        await limiter.wait()
        started_at = time.monotonic()
        generation = session.generation
        last_attempt = attempt == max_attempts
        session.page, outcome = await _export_one_markdown(
            session.page, session, url, index, total, acc, cfg, logger, limiter,
            lenient_assets=attempt >= 3 or last_attempt,
        )
        limiter.mark_done()

        if outcome.status == _OK:
            await limiter.report_success()
            if session.pool is not None:
                session.pool.mark_ok()
            acc.consecutive_session_failures = 0
            acc.failures.pop(url, None)
            acc.permanent.discard(url)
            return

        if outcome.session_failure:
            acc.consecutive_session_failures += 1
            _maybe_abort_on_session_failures(acc, logger)

        if outcome.status == _BLOCKED:
            cooldown = await limiter.report_block(
                logger, outcome.error, retry_after=outcome.retry_after,
                started_at=started_at,
            )
            await rotate_exit_after_block(
                session, limiter, cooldown, logger, generation=generation,
            )
            continue
        if outcome.status in (_TRANSIENT, _FAILED):
            if outcome.exit_failure:
                await note_exit_failure(
                    session, logger, limiter=limiter, generation=generation,
                )
            if last_attempt or (outcome.status == _FAILED and attempt >= 2):
                break
            await asyncio.sleep(min(20.0, 4.0 * attempt))
            continue
        break

    acc.failures[url] = outcome.error
    if outcome.status == _PERMANENT:
        acc.permanent.add(url)
    if outcome.phase:
        acc.slow_records.append(
            SlowPageRecord(url=url, elapsed_seconds=outcome.elapsed, phase=outcome.phase)
        )


def _maybe_abort_on_session_failures(acc: MarkdownAccumulators, logger) -> None:
    if acc.consecutive_session_failures >= _SESSION_FAILURE_CIRCUIT_BREAKER and not acc.abort_requested:
        logger.error(
            "Circuit breaker: %d falhas consecutivas de sessao (login/CF). "
            "Abortando export Markdown. Resolva o problema e rode novamente — "
            "o resume retomara as URLs restantes.",
            acc.consecutive_session_failures,
        )
        acc.abort_requested = True


def _allocate_md_filename(index: int, title: str, acc: MarkdownAccumulators) -> str:
    file_name = build_file_name(index, title, acc.used_names, ext=".md")
    acc.used_names.add(file_name)
    return file_name


def _write_markdown_file(path: Path, content: str) -> int:
    """Escreve markdown atomicamente (tmp + replace). Retorna tamanho em bytes."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    data = content.encode("utf-8")
    try:
        with tmp_path.open("wb") as fp:
            fp.write(data)
        atomic_replace_with_retry(str(tmp_path), str(path))
        return len(data)
    except BaseException:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


async def _extract_main_content_html(page: Page, url: str, logger) -> str | None:
    """Retorna o innerHTML do primeiro seletor de conteudo principal encontrado."""
    for sel in _CONTENT_SELECTORS:
        try:
            locator = page.locator(sel)
            if await locator.count() > 0:
                return await locator.first.inner_html()
        except PlaywrightError:
            continue
    logger.warning("Nenhum seletor de conteudo encontrado em %s", url)
    return None


async def _download_asset(
    abs_url: str,
    page: Page,
    page_attachments_dir: Path,
    page_slug: str,
    downloaded: dict[str, str],
    used_local_names: set[str],
    logger,
) -> str | None:
    """Baixa uma imagem/anexo via sessao autenticada do Playwright (cookies do context).

    Cacheia por URL absoluta (evita baixar 2x o mesmo asset na mesma pagina).
    Retorna caminho relativo (a partir de markdown/pages/) ou None se falhou.
    Levanta RateLimitedError em HTTP 429: a pagina inteira e repetida depois
    do cooldown, em vez de ficar com a imagem apontando para o servidor.
    """
    if abs_url in downloaded:
        return downloaded[abs_url]

    parsed = urlparse(abs_url)
    if parsed.scheme not in ("http", "https"):
        return None

    raw_name = Path(parsed.path).name or "asset"
    stem = slugify(Path(raw_name).stem) or "asset"
    ext = Path(raw_name).suffix[:10]
    candidate = f"{stem}{ext}"
    n = 2
    while candidate in used_local_names:
        candidate = f"{stem}-{n}{ext}"
        n += 1
        if n > 100:
            break
    used_local_names.add(candidate)

    try:
        response = await page.request.get(abs_url, timeout=_ASSET_TIMEOUT_MS)
        if response.status == 429:
            headers = getattr(response, "headers", None) or {}
            raise RateLimitedError(
                "download de imagem/anexo bloqueado com HTTP 429",
                retry_after=parse_retry_after(headers.get("retry-after")),
            )
        if not response.ok:
            logger.debug("Asset HTTP %s em %s", response.status, abs_url)
            return None
        body = await response.body()
        if len(body) > _MAX_ASSET_BYTES:
            logger.warning(
                "Asset ignorado (> %dMB): %s", _MAX_ASSET_BYTES // (1024 * 1024), abs_url,
            )
            return None
        page_attachments_dir.mkdir(parents=True, exist_ok=True)
        (page_attachments_dir / candidate).write_bytes(body)
    except PlaywrightError as exc:
        logger.debug("Falha ao baixar asset %s: %s", abs_url, exc)
        return None
    except OSError as exc:
        logger.warning("Falha ao gravar asset %s: %s", candidate, exc)
        return None

    rel_path = f"../{_ATTACHMENT_DIRNAME}/{page_slug}/{candidate}"
    downloaded[abs_url] = rel_path
    return rel_path


async def _localize_and_convert(
    content_html: str,
    base_url: str,
    page: Page,
    attachments_dir: Path,
    page_slug: str,
    logger,
) -> str:
    """Baixa imagens/anexos referenciados, reescreve links para caminhos locais
    e converte o HTML resultante para Markdown."""
    soup = BeautifulSoup(content_html, "html.parser")
    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    page_attachments_dir = attachments_dir / page_slug
    downloaded: dict[str, str] = {}
    used_local_names: set[str] = set()

    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        abs_url = urljoin(base_url, src)
        if abs_url not in downloaded:
            await asyncio.sleep(_ASSET_PAUSE_SECONDS)
        rel_path = await _download_asset(
            abs_url, page, page_attachments_dir, page_slug, downloaded, used_local_names, logger,
        )
        if rel_path:
            img["src"] = rel_path
            img.attrs.pop("srcset", None)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(_SKIP_HREF_PREFIXES):
            continue
        if not any(hint in href for hint in _ATTACHMENT_HREF_HINTS):
            continue
        abs_url = urljoin(base_url, href)
        rel_path = await _download_asset(
            abs_url, page, page_attachments_dir, page_slug, downloaded, used_local_names, logger,
        )
        if rel_path:
            a["href"] = rel_path

    markdown_text = _html_to_markdown(
        str(soup), heading_style="ATX", bullets="-", strip=["script", "style"],
    )
    return re.sub(r"\n{3,}", "\n\n", markdown_text)


async def _export_one_markdown(
    page: Page,
    session: BrowserSession,
    url: str,
    index: int,
    total: int,
    acc: MarkdownAccumulators,
    cfg: MarkdownExportConfig,
    logger,
    limiter: RateLimiter,
    lenient_assets: bool = False,
) -> tuple[Page, _Outcome]:
    """UMA tentativa de exportar a URL para Markdown. Retorna (page, resultado)."""
    page_start = time.monotonic()

    def elapsed() -> float:
        return time.monotonic() - page_start

    try:
        await _goto_dom_ready(page, url, cfg.dom_budget_ms)

        if await _is_login_page(page):
            raise LoginPageError(
                "Pagina retornou tela de login (Confluence privado ou sessao expirada)"
            )

        await _prepare_page_for_pdf(page, page_start, cfg.timeout_ms, logger, url)
        await _verify_page_assets(page, logger, url, lenient=lenient_assets)

        try:
            title = await page.title()
        except PlaywrightError:
            title = ""
        if not title:
            title = f"pagina-{index}"

        content_html = await _extract_main_content_html(page, url, logger)
        if content_html is None:
            raise RuntimeError("Conteudo principal nao encontrado na pagina")

        page_slug = slugify(title)
        markdown_body = await _localize_and_convert(
            content_html, page.url, page, acc.attachments_dir, page_slug, logger,
        )

        file_name = acc.names_by_url.get(url) or _allocate_md_filename(index, title, acc)
        acc.names_by_url[url] = file_name
        md_path = acc.md_dir / file_name
        full_text = f"# {title}\n\nFonte: {url}\n\n{markdown_body.strip()}\n"
        size_bytes = _write_markdown_file(md_path, full_text)
        elapsed_total = elapsed()

        acc.exported[index] = (md_path, title, url)
        acc.checkpoint.record_export_md(
            url=url, filename=file_name, title=title,
            size_bytes=size_bytes, elapsed_seconds=elapsed_total,
        )
        if elapsed_total >= cfg.slow_threshold_seconds:
            acc.slow_records.append(
                SlowPageRecord(url=url, elapsed_seconds=elapsed_total, phase="md")
            )
            logger.warning("Pagina lenta na exportacao MD (%.1fs): %s", elapsed_total, url)

        logger.info(
            "Markdown gerado (%s/%s) em %.1fs (%.1f KB): %s",
            index, total, elapsed_total, size_bytes / 1024, file_name,
        )
        return page, _Outcome(_OK, elapsed=elapsed_total)

    except LoginPageError as exc:
        logger.error("Login detectado em %s: %s", url, exc)
        return page, _Outcome(
            _PERMANENT, "Pagina de login (Confluence privado)", session_failure=True,
        )

    except RateLimitedError as exc:
        logger.warning("Rate limit em %s apos %.1fs: %s", url, elapsed(), exc)
        outcome = _Outcome(_BLOCKED, str(exc), retry_after=exc.retry_after)

    except CloudflareChallengeError as exc:
        logger.error("Bloqueio do Cloudflare em %s: %s", url, exc)
        outcome = _Outcome(
            _BLOCKED, f"Cloudflare: {exc}", retry_after=exc.retry_after,
            phase="md-cf-challenge", elapsed=elapsed(), session_failure=True,
        )

    except TransientHTTPError as exc:
        logger.warning("HTTP transitorio em %s apos %.1fs: %s", url, elapsed(), exc)
        outcome = _Outcome(_BLOCKED, str(exc), retry_after=exc.retry_after)

    except TransientNetworkError as exc:
        logger.warning("Erro de rede em %s apos %.1fs: %s", url, elapsed(), exc)
        outcome = _Outcome(_TRANSIENT, str(exc))

    except PageHTTPError as exc:
        logger.error("%s ao abrir %s", exc, url)
        return page, _Outcome(_PERMANENT, str(exc))

    except PlaywrightTimeoutError:
        msg = f"TIMEOUT apos {elapsed():.1f}s (limite {cfg.timeout_ms / 1000:.0f}s)"
        logger.error("%s: %s", msg, url)
        # Direto: pagina pesada ou servidor lento, desacelera. Por proxy: a
        # falha conta contra a saida. Em ambos, fica para a repescagem.
        await note_page_timeout(session, limiter, logger, "timeout no Markdown")
        outcome = _Outcome(_TIMEOUT, msg, phase="md-timeout", elapsed=elapsed())

    except PlaywrightError as exc:
        logger.error(
            "Erro Playwright em %s apos %.1fs: %s", url, elapsed(), short_error(exc),
        )
        transient = is_transient_network_error(exc) or is_blocking_error(exc)
        outcome = _Outcome(
            _TRANSIENT if transient else _FAILED, short_error(exc), exit_failure=transient,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erro ao gerar Markdown de %s apos %.1fs", url, elapsed())
        outcome = _Outcome(_FAILED, str(exc) or type(exc).__name__)

    await _safe_close_page(page)
    try:
        return await _new_page(session.context, cfg.timeout_ms), outcome
    except PlaywrightError:
        return page, outcome
