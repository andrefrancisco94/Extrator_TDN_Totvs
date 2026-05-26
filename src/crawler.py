from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from .browser import (
    BrowserSession,
    is_browser_alive as _is_browser_alive_helper,
    launch_browser_session as _launch_browser_session_helper,
    new_page as _new_page,
    safe_close_page as _safe_close_page,
    teardown_session as _teardown_session,
)
from .utils import (
    Checkpoint,
    CrawlScope,
    MAX_RETRY_ATTEMPTS,
    RateLimitConfig,
    RateLimiter,
    SlowPageRecord,
    canonicalize_url,
    get_console,
    is_blocking_error,
    is_cloudflare_challenge,
    is_url_in_scope,
    parse_scope,
    storage_state_path,
)

__all__ = ["crawl_confluence_tree"]

_DISPLAY_PREFIX = "/display/"
_DISPLAY_PUBLIC_PREFIX = "/display/public/"

# Maximo de paginas/pagina REST API page (Confluence default 25, max 200)
_API_PAGE_SIZE = 200
# Hard cap de paginacao REST API (200 * 20 = 4000 filhos diretos, ja muito alem do real)
_API_MAX_PAGES = 20
_API_TIMEOUT_MS = 30_000

_TREE_ROOTS = (
    ".plugin_pagetree",
    ".ia-splitter-left",
    "#page-tree",
    "#sidebar",
    "#children-section",
    "#main-content .childpages-macro",
)

_LOAD_SELECTORS = (
    ".plugin_pagetree a[href]",
    ".ia-splitter-left a[href]",
    "#sidebar a[href]",
    "#page-tree a[href]",
    "#children-section a[href]",
    "#main-content",
)

from .utils import PAGE_ROTATION_INTERVAL as _PAGE_ROTATION_INTERVAL  # noqa: PLC0415

# Salva snapshot do crawl a cada N URLs processadas. Permite resume parcial
# se o processo for interrompido no meio do mapeamento.
_CRAWL_CHECKPOINT_EVERY = 10

# Se mais de X% das paginas crawladas deram timeout, nao marca crawl_complete
# (evita resume futuro com lista parcial confundida com completa).
_CRAWL_TIMEOUT_RATIO_THRESHOLD = 0.20

# Maximo de re-tentativas dentro do mesmo run para URLs que deram timeout.
# Diferente de MAX_RETRY_ATTEMPTS (que conta entre runs).
_INTRA_RUN_RETRY_LIMIT = 2

# Cap do cache REST API intra-run (evita memory leak em sites enormes)
_API_CACHE_MAX_ENTRIES = 5000


@dataclass
class CrawlState:
    """Estado mutavel do crawl (BFS + slow records)."""
    ordered_urls: list[str] = field(default_factory=list)
    slow_records: list[SlowPageRecord] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    queued: set[str] = field(default_factory=set)
    queue: deque = field(default_factory=deque)
    # Contador de tentativas dentro do run atual (intra-run, separado do manifest).
    # URLs com timeout voltam pra fila ate _INTRA_RUN_RETRY_LIMIT.
    attempts: dict[str, int] = field(default_factory=dict)
    # Cache de respostas REST API: key=(page_id, start) -> results
    # Evita re-fetch quando crawl re-visita uma URL apos timeout.
    # CAP: 5000 entries (suficiente pra sites grandes; depois desaloca em FIFO).
    api_cache: dict[tuple[str, int], list[dict]] = field(default_factory=dict)
    # Lock para api_cache e attempts (sincroniza se crawler for paralelizado).
    state_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class CrawlConfig:
    timeout_ms: int
    dom_budget_ms: int
    slow_threshold_seconds: float
    max_pages: int | None
    headless: bool
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    max_workers: int = 1
    state_path: Path | None = None
    proxy: str | None = None


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, PlaywrightTimeoutError):
        return False
    return isinstance(exc, PlaywrightError)


def _match_start_url_format(url: str, scope: CrawlScope) -> str:
    """Ajusta /display/ vs /display/public/ para bater com formato do start URL.

    REST API retorna webui sempre como /display/X/... sem /public/. Se o
    escopo veio de /display/public/X/..., normaliza pra ter /public/ tambem.
    """
    if _DISPLAY_PUBLIC_PREFIX in scope.path_prefix:
        if _DISPLAY_PREFIX in url and _DISPLAY_PUBLIC_PREFIX not in url:
            return url.replace(_DISPLAY_PREFIX, _DISPLAY_PUBLIC_PREFIX, 1)
    elif _DISPLAY_PUBLIC_PREFIX in url:
        return url.replace(_DISPLAY_PUBLIC_PREFIX, _DISPLAY_PREFIX, 1)
    return url


async def _get_page_id(page: Page) -> str | None:
    """Le meta ajs-page-id da pagina (Confluence Server/DC)."""
    try:
        return await page.evaluate(
            "() => document.querySelector('meta[name=\"ajs-page-id\"]')?.getAttribute('content') || null"
        )
    except PlaywrightError:
        return None


async def _fetch_api_page(
    page: Page, api_url: str, logger, limiter: RateLimiter | None = None,
) -> tuple[list[dict] | None, bool]:
    """Faz uma chamada REST API. Retorna (results, was_blocked).

    was_blocked=True indica que a chamada falhou com codigo 5xx/429.
    results=None indica falha generica (sem indicio de bloqueio).
    """
    try:
        response = await page.request.get(api_url, timeout=_API_TIMEOUT_MS)
    except PlaywrightError as exc:
        blocked = is_blocking_error(exc)
        if limiter is not None and blocked:
            await limiter.report_block(logger, f"REST API: {exc}")
        logger.debug("REST API falhou em %s: %s", api_url, exc)
        return None, blocked
    if not response.ok:
        # 5xx ou 429 = bloqueio. 4xx outros = pagina nao tem filhos / API negada.
        blocked = response.status >= 500 or response.status == 429
        if blocked and limiter is not None:
            await limiter.report_block(logger, f"REST API HTTP {response.status}")
        logger.debug("REST API status %s em %s", response.status, api_url)
        return None, blocked
    try:
        data = await response.json()
    except (PlaywrightError, ValueError) as exc:
        logger.debug("REST API JSON invalido: %s", exc)
        return None, False
    if not isinstance(data, dict):
        return None, False
    results = data.get("results", []) or []
    return (results if isinstance(results, list) else []), False


def _extract_urls_from_results(
    results: list[dict], base: str, scope: CrawlScope,
) -> list[str]:
    urls: list[str] = []
    for r in results:
        link = (r.get("_links") or {}).get("webui", "")
        if link:
            urls.append(_match_start_url_format(base + link, scope))
    return urls


async def _fetch_or_cache_api_page(
    page: Page, page_id: str, base: str, start: int, logger,
    limiter: RateLimiter | None,
    api_cache: dict[tuple[str, int], list[dict]] | None,
    cache_lock: asyncio.Lock | None = None,
) -> tuple[list[dict] | None, bool]:
    """Wrap _fetch_api_page com cache opcional por (page_id, start).

    Se cache_lock fornecido, protege leitura/escrita do api_cache.
    """
    cache_key = (page_id, start)
    if api_cache is not None:
        if cache_lock is not None:
            async with cache_lock:
                cached = api_cache.get(cache_key)
        else:
            cached = api_cache.get(cache_key)
        if cached is not None:
            logger.debug("REST API cache hit: %s start=%d", page_id, start)
            return cached, False
    api_url = (
        f"{base}/rest/api/content/{page_id}/child/page"
        f"?limit={_API_PAGE_SIZE}&start={start}"
    )
    results, blocked = await _fetch_api_page(page, api_url, logger, limiter)
    if results is not None and api_cache is not None:
        if cache_lock is not None:
            async with cache_lock:
                _bounded_cache_set(api_cache, cache_key, results)
        else:
            _bounded_cache_set(api_cache, cache_key, results)
    return results, blocked


def _bounded_cache_set(cache: dict, key, value) -> None:
    """Insere em cache com cap (_API_CACHE_MAX_ENTRIES). Drop FIFO se cheio.

    Dict do Python 3.7+ preserva ordem de insercao, entao o "primeiro" eh
    o mais antigo. Drop simples por ordem (nao LRU verdadeiro, mas barato).
    """
    if len(cache) >= _API_CACHE_MAX_ENTRIES:
        try:
            oldest_key = next(iter(cache))
            del cache[oldest_key]
        except (StopIteration, KeyError):
            pass
    cache[key] = value


async def _get_children_via_api(
    page: Page,
    current_url: str,
    scope: CrawlScope,
    logger,
    limiter: RateLimiter | None = None,
    api_cache: dict[tuple[str, int], list[dict]] | None = None,
    cache_lock: asyncio.Lock | None = None,
) -> tuple[list[str] | None, bool]:
    """Tenta buscar paginas filhas via Confluence REST API.

    Retorna (urls, was_blocked).
    Se api_cache for fornecido, reusa respostas por (page_id, start).
    """
    page_id = await _get_page_id(page)
    if not page_id:
        logger.debug("Sem ajs-page-id em %s; pulando REST API", current_url)
        return None, False

    parsed = urlparse(current_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    urls: list[str] = []
    start = 0

    for _ in range(_API_MAX_PAGES):
        results, blocked = await _fetch_or_cache_api_page(
            page, page_id, base, start, logger, limiter, api_cache, cache_lock,
        )
        if blocked:
            return None, True
        if results is None:
            return None, False
        urls.extend(_extract_urls_from_results(results, base, scope))
        if len(results) < _API_PAGE_SIZE:
            break
        start += _API_PAGE_SIZE

    return urls, False


async def _try_expand_current_node(page: Page) -> None:
    """Click no toggle do no atual na sidebar (fallback para REST API)."""
    try:
        await page.evaluate(
            """() => {
                const pageId = document.querySelector(
                    'meta[name="ajs-page-id"]'
                )?.content;
                if (!pageId) return;
                const el = document.querySelector(`[data-page-id="${pageId}"]`);
                if (!el) return;
                const li = el.closest('li');
                if (!li) return;
                if (li.querySelector('ul li')) return;  // ja tem filhos
                const toggle = li.querySelector(
                    '.plugin_pagetree_childtoggle, .icon-page-tree-expand, '
                    + '.expand-control-icon, button[aria-expanded="false"]'
                );
                if (toggle) toggle.click();
            }"""
        )
    except PlaywrightError:
        pass


async def _extract_child_links(page: Page, current_url: str) -> list[str]:
    return await page.evaluate(
        """
        ([currentUrl, treeRoots]) => {
          const normalize = (href) => {
            try {
              const u = new URL(href, window.location.origin);
              u.hash = '';
              return u.toString();
            } catch { return null; }
          };

          const altUrl = (() => {
            try {
              const u = new URL(currentUrl);
              if (u.pathname.startsWith('/display/public/')) {
                const a = new URL(u);
                a.pathname = u.pathname.replace('/display/public/', '/display/');
                return a.toString();
              }
              if (u.pathname.startsWith('/display/') && !u.pathname.startsWith('/display/public/')) {
                const parts = u.pathname.split('/').filter(Boolean);
                if (parts.length >= 2 && parts[0] === 'display') {
                  const a = new URL(u);
                  a.pathname = '/display/public/' + parts.slice(1).join('/');
                  return a.toString();
                }
              }
            } catch {}
            return null;
          })();

          const candidates = new Set([normalize(currentUrl)].filter(Boolean));
          if (altUrl) {
            const n = normalize(altUrl);
            if (n) candidates.add(n);
          }

          let li = null;

          const pageId = document.querySelector('meta[name="ajs-page-id"]')?.getAttribute('content');
          if (pageId) {
            for (const sel of treeRoots) {
              const container = document.querySelector(sel);
              if (!container) continue;
              const toggle = container.querySelector(`[data-page-id="${pageId}"]`);
              if (toggle) {
                li = toggle.closest('li');
                break;
              }
            }
          }

          if (!li) {
            for (const sel of treeRoots) {
              const container = document.querySelector(sel);
              if (!container) continue;
              for (const a of container.querySelectorAll('a[href]')) {
                const n = normalize(a.getAttribute('href'));
                if (n && candidates.has(n)) {
                  li = a.closest('li');
                  break;
                }
              }
              if (li) break;
            }
          }

          if (li) {
            const childHrefs = new Set();
            li.querySelectorAll('li a[href], ul a[href]').forEach(a => {
              const href = a.getAttribute('href');
              if (!href) return;
              const n = normalize(href);
              if (n && !candidates.has(n)) childHrefs.add(href);
            });
            if (childHrefs.size > 0) return Array.from(childHrefs);
          }

          const childSection = document.querySelector(
            '#children-section a[href], .childpages-macro a[href], .children-show-hide a[href]'
          );
          if (childSection) {
            const hrefs = new Set();
            document.querySelectorAll(
              '#children-section a[href], .childpages-macro a[href]'
            ).forEach(a => {
              const href = a.getAttribute('href');
              if (href) hrefs.add(href);
            });
            return Array.from(hrefs);
          }

          return [];
        }
        """,
        [current_url, list(_TREE_ROOTS)],
    )


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=2, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
async def _goto_dom_ready(page: Page, url: str, timeout_ms: int) -> None:
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)


# Aliases para retrocompat com codigo interno do crawler
_launch_browser_session = _launch_browser_session_helper
_is_browser_alive = _is_browser_alive_helper


# Lock global do crawler para recovery (crawler atual eh single-worker, mas
# previne race se for paralelizado no futuro).
_crawl_browser_recovery_lock = asyncio.Lock()


async def _ensure_browser_alive(
    session: BrowserSession, playwright, cfg: CrawlConfig, logger,
) -> None:
    if _is_browser_alive(session):
        return
    async with _crawl_browser_recovery_lock:
        # Re-check apos pegar o lock
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


def _is_valid_url_str(url) -> bool:
    """Filtra URLs malformadas (None, vazia, nao-string)."""
    return isinstance(url, str) and url.strip().startswith(("http://", "https://"))


def _restore_state_from_checkpoint(
    state: CrawlState, checkpoint: Checkpoint, start_canonical: str, logger,
) -> bool:
    """Restaura state da fila/seen salvos no manifest. Retorna True se resumiu.

    Filtra URLs malformadas que podem ter sido inseridas via edicao manual
    do manifest (None, strings vazias, nao-http URLs).
    """
    saved_queue = [u for u in checkpoint.manifest.crawl_queue if _is_valid_url_str(u)]
    saved_seen = [u for u in checkpoint.manifest.crawl_seen if _is_valid_url_str(u)]
    saved_mapped = [u for u in checkpoint.manifest.mapped_urls if _is_valid_url_str(u)]

    if not (saved_queue or saved_seen or saved_mapped):
        return False

    # Restaura mapped_urls como historico, DEDUPLICANDO (manifest pode estar
    # editado manualmente com duplicatas).
    seen_so_far: set[str] = set()
    state.ordered_urls = []
    for url in saved_mapped:
        if url not in seen_so_far:
            state.ordered_urls.append(url)
            seen_so_far.add(url)

    state.seen = set(saved_seen) | seen_so_far
    # Re-enqueua o que estava na fila
    for url in saved_queue:
        if url not in state.queued and url not in state.seen:
            state.queue.append(url)
            state.queued.add(url)

    # URLs em failures (timeout/erro de run anterior) merecem nova tentativa.
    # Remove de seen para reprocessamento e adiciona na fila.
    pending_retry = checkpoint.urls_pending_retry(MAX_RETRY_ATTEMPTS)
    retry_count = 0
    for url in pending_retry:
        state.seen.discard(url)
        if url not in state.queued:
            state.queue.append(url)
            state.queued.add(url)
            retry_count += 1
    if retry_count:
        logger.info(
            "Re-enfileirando %d URL(s) com failure do run anterior "
            "(tentativas < %d).",
            retry_count, MAX_RETRY_ATTEMPTS,
        )

    # Garante que o start_canonical esta processado ou na fila
    if start_canonical not in state.seen and start_canonical not in state.queued:
        state.queue.appendleft(start_canonical)
        state.queued.add(start_canonical)

    logger.info(
        "Resume parcial do crawl: %d mapeadas (historico), "
        "%d na fila, %d ja vistas.",
        len(state.ordered_urls), len(state.queue), len(state.seen),
    )
    return True


def _reset_for_force_recrawl(checkpoint: Checkpoint, logger) -> None:
    """Limpa estado parcial do manifest quando force_recrawl=True."""
    if not checkpoint.manifest.mapped_urls:
        return
    logger.info(
        "Re-crawl forcado: ignorando %d URLs cacheadas do run anterior.",
        len(checkpoint.manifest.mapped_urls),
    )
    checkpoint.manifest.crawl_queue = []
    checkpoint.manifest.crawl_seen = []
    checkpoint.manifest.crawl_complete = False
    try:
        checkpoint.save()
    except OSError as exc:
        logger.warning("Falha ao limpar manifest no force_recrawl: %s", exc)


def _finalize_crawl(
    state: CrawlState, checkpoint: Checkpoint, max_pages: int | None, logger,
) -> None:
    """Decide se marca crawl_complete ou salva progresso parcial."""
    timeout_count = sum(
        1 for r in state.slow_records if r.phase.endswith("-timeout")
    )
    timeout_ratio = timeout_count / max(1, len(state.ordered_urls))
    queue_empty = not state.queue
    if (
        state.ordered_urls
        and queue_empty
        and timeout_ratio < _CRAWL_TIMEOUT_RATIO_THRESHOLD
    ):
        checkpoint.record_crawl_complete(state.ordered_urls, max_pages=max_pages)
        return

    if not queue_empty:
        logger.warning(
            "Crawl interrompido: ainda restam %d URLs na fila. "
            "Proxima execucao retomara de onde parou.",
            len(state.queue),
        )
    if timeout_count:
        logger.warning(
            "Crawl com timeouts: %d/%d (%.0f%%) URLs deram timeout. "
            "Estas serao re-tentadas no proximo run.",
            timeout_count, len(state.ordered_urls), timeout_ratio * 100,
        )
    checkpoint.save_crawl_progress(
        ordered_urls=state.ordered_urls,
        queue=list(state.queue),
        seen=list(state.seen),
        max_pages=max_pages,
    )


async def crawl_confluence_tree(
    start_url: str,
    checkpoint: Checkpoint,
    logger,
    headless: bool = True,
    timeout_ms: int = 120_000,
    max_pages: int | None = None,
    slow_threshold_seconds: float = 60.0,
    force_recrawl: bool = False,
    rate_limit: RateLimitConfig | None = None,
    max_workers: int = 1,
    limiter: RateLimiter | None = None,
    state_path: Path | None = None,
    proxy: str | None = None,
) -> tuple[list[str], list[SlowPageRecord]]:
    """BFS pela arvore de paginas do Confluence.

    Se force_recrawl=False e o checkpoint indicar crawl ja completo, retorna
    a lista cacheada (modo resume). Com force_recrawl=True, sempre re-mapeia
    para detectar novas paginas. Se o crawl anterior foi interrompido (queue
    persistida), retoma da fila salva.
    """
    start_canonical = canonicalize_url(start_url)
    scope: CrawlScope = parse_scope(start_canonical)
    # DOM tem ate metade do orcamento; cap de 120s (algumas paginas TDN com
    # muitos macros levam ate 90-100s soh pra renderizar).
    dom_budget_ms = min(120_000, timeout_ms // 2)

    cfg = CrawlConfig(
        timeout_ms=timeout_ms, dom_budget_ms=dom_budget_ms,
        slow_threshold_seconds=slow_threshold_seconds,
        max_pages=max_pages, headless=headless,
        rate_limit=rate_limit or RateLimitConfig(),
        max_workers=max(1, max_workers),
        state_path=state_path,
        proxy=proxy,
    )

    # Resume completo: crawl ja terminou anteriormente
    if (
        not force_recrawl
        and checkpoint.manifest.crawl_complete
        and checkpoint.manifest.mapped_urls
    ):
        logger.info(
            "Resume: reutilizando crawl anterior com %d URLs mapeadas.",
            len(checkpoint.manifest.mapped_urls),
        )
        return list(checkpoint.manifest.mapped_urls), []

    if force_recrawl:
        _reset_for_force_recrawl(checkpoint, logger)

    logger.info("Iniciando crawl em %s", start_canonical)
    logger.info(
        "Escopo: dominio=%s | path_prefix=%s | space=%s",
        scope.domain, scope.path_prefix, scope.space_key,
    )
    logger.info(
        "Orcamento por pagina: %.0fs total | %.0fs DOM | slow >= %.0fs",
        timeout_ms / 1000, dom_budget_ms / 1000, slow_threshold_seconds,
    )
    logger.info(
        "Rate limit: %.1fs entre requests | cooldown inicial %.0fs | workers=%d",
        cfg.rate_limit.base_delay_seconds,
        cfg.rate_limit.backoff_initial_seconds,
        cfg.max_workers,
    )

    state = CrawlState(queue=deque([start_canonical]), queued={start_canonical})

    if not force_recrawl:
        _restore_state_from_checkpoint(state, checkpoint, start_canonical, logger)

    console = get_console()
    active_limiter = limiter if limiter is not None else RateLimiter(cfg.rate_limit)

    async with async_playwright() as playwright:
        session = await _launch_browser_session(
            playwright, headless, timeout_ms, logger,
            state_path=state_path, proxy=proxy,
        )
        try:
            await _run_crawl_loop(
                session, state, scope, playwright, cfg, logger, console,
                checkpoint, active_limiter,
            )
            await _safe_close_page(session.page)
        finally:
            await _teardown_session(session, logger)

    _finalize_crawl(state, checkpoint, max_pages, logger)
    logger.info(
        "Crawl finalizado: %s pagina(s) mapeada(s), %s lenta(s), fila restante=%d.",
        len(state.ordered_urls), len(state.slow_records), len(state.queue),
    )
    return state.ordered_urls, state.slow_records


async def _run_crawl_loop(
    session: BrowserSession,
    state: CrawlState,
    scope: CrawlScope,
    playwright,
    cfg: CrawlConfig,
    logger,
    console,
    checkpoint: Checkpoint,
    limiter: RateLimiter,
) -> None:
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=None),
        TextColumn("{task.completed} mapeada(s) | fila: {task.fields[queue]}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    with progress:
        task = progress.add_task("Mapeando arvore...", total=None, queue=0)
        pages_processed = 0

        while state.queue:
            current = state.queue.popleft()
            state.queued.discard(current)

            if current in state.seen:
                continue

            state.seen.add(current)
            state.ordered_urls.append(current)
            short = current if len(current) <= 60 else current[:57] + "..."
            progress.update(
                task, advance=1, queue=len(state.queue),
                description=f"Mapeando: {short}",
            )
            logger.info(
                "Mapeando (%s/%s, fila=%s): %s",
                len(state.ordered_urls), cfg.max_pages or "infinito",
                len(state.queue), current,
            )

            if cfg.max_pages and len(state.ordered_urls) >= cfg.max_pages:
                logger.warning("Limite max_pages=%s atingido.", cfg.max_pages)
                break

            await _ensure_browser_alive(session, playwright, cfg, logger)
            await _maybe_rotate_page(session, pages_processed, cfg, logger)
            await limiter.wait()  # throttle anti-bloqueio

            page_start = time.monotonic()
            session.page, timed_out = await _crawl_one(
                session.page, session.context, current, state, scope, cfg,
                page_start, logger, limiter,
            )
            progress.update(task, queue=len(state.queue))
            pages_processed += 1

            # Re-enfileira URL com timeout para nova tentativa (intra-run).
            if timed_out:
                await _maybe_requeue_timed_out(current, state, logger, checkpoint)

            # Snapshot periodico para resume parcial
            if pages_processed % _CRAWL_CHECKPOINT_EVERY == 0:
                _save_partial_progress(checkpoint, state, cfg, logger)


async def _maybe_requeue_timed_out(
    current: str, state: CrawlState, logger, checkpoint: Checkpoint,
) -> None:
    """Re-enfileira URL com timeout (ate _INTRA_RUN_RETRY_LIMIT vezes).

    NAO remove de ordered_urls para nao criar inconsistencia com checkpoints
    ja salvos. Apenas remove de `seen` para permitir reprocessamento.
    Quando desiste apos retries intra-run, registra em failures para que o
    proximo run a re-tente. Usa state_lock para sincronizar com workers.
    """
    async with state.state_lock:
        attempts = state.attempts.get(current, 0) + 1
        state.attempts[current] = attempts
        should_give_up = attempts >= _INTRA_RUN_RETRY_LIMIT

    if should_give_up:
        logger.error(
            "URL desistida apos %d timeouts intra-run: %s "
            "(registrada em failures para re-tentar no proximo run)",
            attempts, current,
        )
        try:
            checkpoint.record_failure(
                current, f"crawl timeout apos {attempts} tentativas intra-run",
            )
        except OSError as exc:
            logger.warning("Falha ao registrar failure de %s: %s", current, exc)
        return

    # Remove de seen para permitir reprocessamento; NAO mexer em ordered_urls
    state.seen.discard(current)
    # Coloca no FIM da fila pra dar tempo de outras URLs cooperarem
    state.queue.append(current)
    state.queued.add(current)
    logger.info(
        "URL re-enfileirada apos timeout (tentativa %d/%d): %s",
        attempts, _INTRA_RUN_RETRY_LIMIT, current,
    )


def _save_partial_progress(
    checkpoint: Checkpoint, state: CrawlState, cfg: CrawlConfig, logger,
) -> None:
    try:
        checkpoint.save_crawl_progress(
            ordered_urls=state.ordered_urls,
            queue=list(state.queue),
            seen=list(state.seen),
            max_pages=cfg.max_pages,
        )
    except OSError as exc:
        logger.warning("Falha ao salvar progresso parcial: %s", exc)


async def _maybe_rotate_page(
    session: BrowserSession, pages_processed: int, cfg: CrawlConfig, logger,
) -> None:
    if pages_processed == 0 or pages_processed % _PAGE_ROTATION_INTERVAL != 0:
        return
    logger.info("Rotacionando pagina apos %s URLs", pages_processed)
    await _safe_close_page(session.page)
    session.page = await _new_page(session.context, cfg.timeout_ms)


async def _collect_child_urls(
    page: Page,
    current: str,
    scope: CrawlScope,
    cfg: CrawlConfig,
    page_start: float,
    logger,
    limiter: RateLimiter,
    api_cache: dict[tuple[str, int], list[dict]] | None = None,
    cache_lock: asyncio.Lock | None = None,
) -> tuple[list[str], bool]:
    """Coleta filhos via REST API (primario) ou DOM (fallback).

    Retorna (urls, blocked). blocked=True sinaliza que o servidor recusou.
    """
    raw_hrefs, blocked = await _get_children_via_api(
        page, current, scope, logger, limiter,
        api_cache=api_cache, cache_lock=cache_lock,
    )
    if blocked:
        return [], True
    if raw_hrefs is not None:
        await limiter.report_success()
        logger.info("REST API: %d filhos em %s", len(raw_hrefs), current)
        return raw_hrefs, False

    # Fallback: aguarda sidebar carregar e extrai via DOM.
    logger.debug("REST API indisponivel, usando fallback DOM em %s", current)
    await _wait_for_sidebar(page, page_start, cfg)
    try:
        hrefs = await _extract_child_links(page, current)
        await limiter.report_success()
        return hrefs or [], False
    except PlaywrightError as exc:
        logger.warning("Falha ao extrair links em %s: %s", current, exc)
        if is_blocking_error(exc):
            await limiter.report_block(logger, f"DOM extract: {exc}")
        return [], False


def _record_slow_crawl(
    current: str, elapsed_total: float, cfg: CrawlConfig, state: CrawlState, logger,
) -> None:
    if elapsed_total < cfg.slow_threshold_seconds:
        return
    state.slow_records.append(
        SlowPageRecord(url=current, elapsed_seconds=elapsed_total, phase="crawl")
    )
    logger.warning("Pagina lenta no crawl (%.1fs): %s", elapsed_total, current)


async def _handle_crawl_timeout(
    page: Page, context, current: str, page_start: float,
    state: CrawlState, cfg: CrawlConfig, logger, limiter: RateLimiter,
) -> Page:
    elapsed = time.monotonic() - page_start
    logger.error(
        "TIMEOUT no crawl apos %.1fs (limite %.0fs): %s",
        elapsed, cfg.timeout_ms / 1000, current,
    )
    state.slow_records.append(
        SlowPageRecord(url=current, elapsed_seconds=elapsed, phase="crawl-timeout")
    )
    await limiter.report_block(logger, "timeout no crawl")
    await _safe_close_page(page)
    return await _new_page(context, cfg.timeout_ms)


async def _crawl_one(
    page: Page,
    context,
    current: str,
    state: CrawlState,
    scope: CrawlScope,
    cfg: CrawlConfig,
    page_start: float,
    logger,
    limiter: RateLimiter,
) -> tuple[Page, bool]:
    """Processa uma URL no crawl. Retorna (page, timed_out).

    timed_out=True: URL nao processou e deve ser re-tentada.
    """
    try:
        await _goto_dom_ready(page, current, cfg.dom_budget_ms)

        # Detecta Cloudflare challenge logo apos goto
        try:
            page_content = await page.content()
            if is_cloudflare_challenge(page_content.lower()):
                logger.warning("Cloudflare challenge detectado em %s", current)
                await limiter.report_block(logger, "Cloudflare challenge no crawl")
                await _safe_close_page(page)
                return await _new_page(context, cfg.timeout_ms), True
        except PlaywrightError:
            pass

        raw_hrefs, blocked = await _collect_child_urls(
            page, current, scope, cfg, page_start, logger, limiter,
            api_cache=state.api_cache, cache_lock=state.state_lock,
        )
        if blocked:
            logger.warning("Bloqueio detectado em %s (REST API)", current)
            await _safe_close_page(page)
            return await _new_page(context, cfg.timeout_ms), True

        if not raw_hrefs:
            logger.info("Nenhum link filho encontrado em %s", current)

        _enqueue_new_links(raw_hrefs, current, state, scope)
        _record_slow_crawl(
            current, time.monotonic() - page_start, cfg, state, logger,
        )
        return page, False

    except PlaywrightTimeoutError:
        page = await _handle_crawl_timeout(
            page, context, current, page_start, state, cfg, logger, limiter,
        )
        return page, True

    except PlaywrightError as exc:
        logger.error("Falha de Playwright no crawl em %s: %s", current, exc)
        timed_out = is_blocking_error(exc)
        if timed_out:
            await limiter.report_block(logger, f"Playwright: {exc}")
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms), timed_out

    except Exception:  # noqa: BLE001
        logger.exception("Erro inesperado no crawl em %s", current)
        await _safe_close_page(page)
        return await _new_page(context, cfg.timeout_ms), False


async def _wait_for_sidebar(page: Page, page_start: float, cfg: CrawlConfig) -> None:
    """Aguarda sidebar carregar de fato: tree com links + AJAX done + node expandido."""
    elapsed_ms = (time.monotonic() - page_start) * 1000
    remaining_ms = max(5_000, int(cfg.timeout_ms - elapsed_ms))

    # 1) Espera que a arvore tenha PELO MENOS 1 link (nao apenas o container vazio).
    #    O default selector `#main-content` retornava imediato; aqui pedimos
    #    explicitamente um link dentro do pagetree.
    try:
        await page.wait_for_function(
            """() => {
                const trees = document.querySelectorAll(
                    '.plugin_pagetree, .ia-splitter-left, #sidebar, #page-tree'
                );
                for (const t of trees) {
                    if (t.querySelector('a[href]')) return true;
                }
                return document.querySelector('#main-content') !== null;
            }""",
            timeout=min(remaining_ms, 20_000),
        )
    except PlaywrightError:
        pass

    # 2) Espera o spinner AJAX do pagetree desaparecer.
    elapsed_ms = (time.monotonic() - page_start) * 1000
    remaining_ms = max(5_000, int(cfg.timeout_ms - elapsed_ms))
    try:
        await page.wait_for_selector(
            ".plugin_pagetree_loading", state="detached",
            timeout=min(remaining_ms, 10_000),
        )
    except PlaywrightError:
        pass

    # 3) Tenta forcar expansao do no atual.
    await _try_expand_current_node(page)

    # 4) Espera o no atual ter filhos OU confirmar que nao tem (loading se foi).
    try:
        await page.wait_for_function(
            """() => {
                const pageId = document.querySelector(
                    'meta[name="ajs-page-id"]'
                )?.content;
                if (!pageId) return true;
                const el = document.querySelector(`[data-page-id="${pageId}"]`);
                if (!el) return true;
                const li = el.closest('li');
                if (!li) return true;
                if (li.querySelector('.plugin_pagetree_loading')) return false;
                return true;
            }""",
            timeout=10_000,
        )
    except PlaywrightError:
        pass

    # 5) Margem para AJAX dos filhos.
    await page.wait_for_timeout(1_500)


def _enqueue_new_links(
    raw_hrefs: list[str],
    current: str,
    state: CrawlState,
    scope: CrawlScope,
) -> None:
    for href in raw_hrefs:
        if not href or href == "#" or href.lower().startswith("javascript:"):
            continue
        absolute = canonicalize_url(urljoin(current, href))
        if absolute in state.seen or absolute in state.queued:
            continue
        if not is_url_in_scope(absolute, scope):
            continue
        state.queued.add(absolute)
        state.queue.append(absolute)


# Suprime warnings de imports nao usados nos hot paths abaixo (asyncio pode
# ser usado por extensoes futuras de paralelizacao do crawl).
_ = asyncio
_ = MAX_RETRY_ATTEMPTS
