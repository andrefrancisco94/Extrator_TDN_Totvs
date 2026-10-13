"""Helpers compartilhados de Playwright (browser session management).

Antes essas funcoes eram duplicadas em crawler.py e pdf_exporter.py. Centralizar
aqui elimina ~80 linhas duplicadas e garante que crawler/export usem o mesmo
comportamento (UA coerente, storage_state, recovery, proxy, bloqueio de
requests superfluos e contagem de rede por pagina).
"""
from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from .blocking import BlockInfo, classify_response, parse_retry_after
from .proxy import ProxyPool, mask_proxy, to_playwright_proxy
from .utils import build_browser_context_args, chrome_user_agent, format_duration

_MAX_BROWSER_LAUNCH_ATTEMPTS = 3

# Lock global para save_storage_state — impede 2 workers escreverem
# simultaneamente o mesmo browser_state.json (corromperia JSON).
_storage_state_lock = asyncio.Lock()

# Requests que uma pagina do Confluence dispara e que NAO afetam o conteudo
# impresso: telemetria, polling de "pagina alterada" e rastreadores de
# terceiros. Cada um deles conta no rate limit do Cloudflare (por IP) sem
# trazer nada para o PDF. Bloqueados via CDP (Network.setBlockedURLs), e nao
# via page.route(): rotear requests desliga o cache HTTP do Chromium e faria
# todo CSS/JS estatico ser baixado de novo a cada pagina.
# A arvore lateral (pagetree) NAO e bloqueada: a macro "Page Tree" usada dentro
# do conteudo de algumas paginas depende do mesmo endpoint.
BLOCKED_URL_PATTERNS: tuple[str, ...] = (
    "*/rest/analytics/*",
    "*/rest/quickreload/*",
    "*/rest/mywork/*",
    "*/rest/helptips/*",
    "*/plugins/servlet/notifications-miniview*",
    "*google-analytics.com/*",
    "*googletagmanager.com/*",
    "*doubleclick.net/*",
    "*hotjar.com/*",
    "*clarity.ms/*",
    "*facebook.net/*",
)

# Tipos de recurso cujo erro compromete o PDF (imagem faltando, pagina sem CSS).
_CONTENT_RESOURCE_TYPES = frozenset({"image", "stylesheet", "font", "media"})
# Falhas de request que sao esperadas e nao indicam problema de rede.
_IGNORED_FAILURES = ("err_aborted", "err_blocked_by_client", "err_cache_miss")


@dataclass
class PageNetStats:
    """Contabilidade de rede de UMA pagina, zerada a cada navegacao.

    O HTML pode voltar 200 enquanto as imagens da mesma pagina ja estao sendo
    respondidas com 429 (o rate limit estoura no meio do carregamento). Sem
    olhar os sub-recursos, o PDF sairia sem imagens e seria gravado como
    sucesso. Aqui ficam registrados os sub-recursos bloqueados/falhos.
    """
    host: str = ""
    requests: int = 0
    # (url, status, retry_after_segundos)
    blocked: list[tuple[str, int, float | None]] = field(default_factory=list)
    # (url, erro) de imagens/CSS que falharam por erro de rede
    failed: list[tuple[str, str]] = field(default_factory=list)

    def begin(self, url: str) -> None:
        """Inicia a contagem de uma nova navegacao para `url`."""
        self.host = urlparse(url).netloc.lower()
        self.requests = 0
        self.blocked = []
        self.failed = []

    def retry_after(self) -> float | None:
        values = [r for _url, _status, r in self.blocked if r]
        return max(values) if values else None

    def _same_host(self, url: str) -> bool:
        return bool(self.host) and urlparse(url).netloc.lower() == self.host

    def on_request(self, request) -> None:
        try:
            if self._same_host(request.url):
                self.requests += 1
        except Exception:  # noqa: BLE001 - listener nunca pode derrubar o run
            pass

    def on_response(self, response) -> None:
        try:
            request = response.request
            if request.is_navigation_request() or not self._same_host(response.url):
                return
            status = response.status
            if status == 429 or (
                status >= 500 and request.resource_type in _CONTENT_RESOURCE_TYPES
            ):
                retry_after = parse_retry_after((response.headers or {}).get("retry-after"))
                self.blocked.append((response.url, status, retry_after))
        except Exception:  # noqa: BLE001
            pass

    def on_request_failed(self, request) -> None:
        try:
            if request.resource_type not in _CONTENT_RESOURCE_TYPES:
                return
            if not self._same_host(request.url):
                return
            failure = str(request.failure or "")
            if any(marker in failure.lower() for marker in _IGNORED_FAILURES):
                return
            self.failed.append((request.url, failure))
        except Exception:  # noqa: BLE001
            pass


# context -> padroes de URL a bloquear nas pages criadas a partir dele.
_context_blocklists: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
# page -> PageNetStats
_page_stats: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


@dataclass
class BrowserSession:
    """Container mutavel para browser/context/page (permite recovery in-place).

    Guarda tambem o necessario para se relancar sozinha (playwright, opcoes,
    pool de proxies): recovery de crash e troca de proxy usam o mesmo caminho.
    """
    browser: object = None
    context: object = None
    page: Page | None = None
    state_path: Path | None = None
    playwright: object = None
    headless: bool = True
    timeout_ms: int = 120_000
    proxy: str | None = None
    pool: ProxyPool | None = None
    block_extras: bool = False
    # Incrementado a cada relancamento: workers em paralelo percebem que o
    # browser foi trocado e pedem uma page nova em vez de usar a antiga.
    generation: int = 0
    rotation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def get_page_stats(page) -> PageNetStats:
    """Contabilidade de rede da page (objeto avulso se a page for desconhecida)."""
    try:
        stats = _page_stats.get(page)
    except TypeError:
        stats = None
    return stats if stats is not None else PageNetStats()


async def _apply_blocklist(context, page, patterns) -> None:
    """Bloqueia padroes de URL via CDP (so Chromium). Falha nunca e fatal."""
    try:
        cdp = await context.new_cdp_session(page)
        await cdp.send("Network.enable")
        await cdp.send("Network.setBlockedURLs", {"urls": list(patterns)})
    except Exception:  # noqa: BLE001 - otimizacao opcional
        pass


async def new_page(context, timeout_ms: int) -> Page:
    """Cria uma nova page, configura timeout e liga a contabilidade de rede."""
    page = await context.new_page()
    page.set_default_timeout(timeout_ms)
    try:
        patterns = _context_blocklists.get(context)
    except TypeError:
        patterns = None
    if patterns:
        await _apply_blocklist(context, page, patterns)
    try:
        stats = PageNetStats()
        _page_stats[page] = stats
        page.on("request", stats.on_request)
        page.on("response", stats.on_response)
        page.on("requestfailed", stats.on_request_failed)
    except Exception:  # noqa: BLE001 - metricas nao podem impedir o uso da page
        pass
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


def _uses_shared_cookies(pool: ProxyPool | None) -> bool:
    """Cookies persistidos so fazem sentido com UMA saida de rede: cookies
    emitidos para um IP nao devem ser reapresentados a partir de outro."""
    return pool is None or len(pool) <= 1


async def launch_browser_session(
    playwright, headless: bool, timeout_ms: int, logger=None,
    state_path: Path | None = None, proxy: str | None = None,
    pool: ProxyPool | None = None, block_extras: bool = False,
) -> BrowserSession:
    """Lança browser com retry. UA coerente com o Chromium, storage_state, proxy.

    Se `pool` for informado, a saida de rede atual do pool tem precedencia
    sobre `proxy`. O proxy e aplicado no LAUNCH (vale para o browser inteiro,
    inclusive as chamadas REST feitas por page.request).

    Levanta RuntimeError se nao conseguir lancar apos _MAX_BROWSER_LAUNCH_ATTEMPTS.
    """
    if pool is not None:
        proxy = pool.current
    launch_kwargs: dict = {"headless": headless, "channel": "chromium"}
    proxy_settings = to_playwright_proxy(proxy)
    if proxy_settings:
        launch_kwargs["proxy"] = proxy_settings
    effective_state = state_path if _uses_shared_cookies(pool) else None

    last_error: Exception | None = None
    for attempt in range(1, _MAX_BROWSER_LAUNCH_ATTEMPTS + 1):
        try:
            browser = await playwright.chromium.launch(**launch_kwargs)
            context_args = build_browser_context_args(
                state_path=effective_state,
                user_agent=chrome_user_agent(getattr(browser, "version", None)),
            )
            context = await browser.new_context(**context_args)
            if block_extras:
                try:
                    _context_blocklists[context] = BLOCKED_URL_PATTERNS
                except TypeError:
                    pass
            page = await new_page(context, timeout_ms)
            return BrowserSession(
                browser=browser, context=context, page=page, state_path=state_path,
                playwright=playwright, headless=headless, timeout_ms=timeout_ms,
                proxy=proxy, pool=pool, block_extras=block_extras,
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
        f"ou use a opcao [9] do menu). Ultimo erro: {last_error}"
    )


async def relaunch_session(session: BrowserSession, logger) -> None:
    """Fecha o browser atual e abre outro in-place (crash recovery / troca de proxy).

    Usa a saida de rede atual do pool, se houver. Nao salva storage_state do
    browser antigo: ele pode estar morto ou associado a um IP bloqueado.

    O chamador deve segurar session.rotation_lock: entre fechar o context
    antigo e publicar o novo, ninguem pode criar page nesta sessao.
    """
    for closer in (session.context, session.browser):
        if closer is None:
            continue
        try:
            await closer.close()
        except (PlaywrightError, Exception):  # noqa: BLE001 - browser pode estar morto
            pass
    new_session = await launch_browser_session(
        session.playwright, session.headless, session.timeout_ms, logger,
        state_path=session.state_path, proxy=session.proxy,
        pool=session.pool, block_extras=session.block_extras,
    )
    session.browser = new_session.browser
    session.context = new_session.context
    session.page = new_session.page
    session.proxy = new_session.proxy
    session.generation += 1


async def rotate_exit_after_block(
    session: BrowserSession, limiter, cooldown: float, logger,
    generation: int | None = None,
) -> bool:
    """Apos um bloqueio por IP, troca para outra saida de rede se houver.

    Marca a saida atual como banida por `cooldown` segundos, escolhe a que
    libera primeiro e ajusta o cooldown do limiter para a espera real dela
    (zero se estiver livre). Retorna True se o browser foi relancado em outra
    saida. Sem pool (ou com saida unica) nao faz nada: vale o cooldown normal.

    generation: geracao da sessao vista por quem sofreu o bloqueio. Se outro
    worker ja trocou de saida por causa do MESMO bloqueio, nao troca de novo.
    """
    pool = session.pool
    if pool is None or len(pool) <= 1:
        return False
    async with session.rotation_lock:
        if generation is not None and generation != session.generation:
            return True
        blocked_label = pool.current_label
        previous_exit = pool.current
        pool.mark_blocked(cooldown)
        switched, wait = pool.pick_best()
        if switched:
            await limiter.switch_exit(previous_exit, pool.current)
        await limiter.set_cooldown(wait)
        if not switched:
            logger.warning(
                "Todas as %d saidas de rede estao bloqueadas. Aguardando %s pela "
                "que libera primeiro (%s).",
                len(pool), format_duration(wait), pool.current_label,
            )
            return False
        logger.warning(
            "Saida %s bloqueada por %s. Alternando para %s.",
            blocked_label, format_duration(cooldown), pool.current_label,
        )
        await relaunch_session(session, logger)
        return True


async def note_exit_failure(
    session: BrowserSession, logger, limiter=None, generation: int | None = None,
) -> bool:
    """Registra erro de conexao (ou timeout) na saida atual; tira do rodizio o
    proxy que falha seguidamente e troca para o proximo.

    Retorna True se a saida foi trocada. Pode levantar AllProxiesDeadError
    quando nao sobra nenhuma saida utilizavel.

    generation: geracao da sessao vista por quem sofreu a falha. Se outro
    worker ja trocou de saida, a falha era do proxy ANTIGO: nao conta contra o
    novo. limiter: recebe a espera da saida escolhida (zero se estiver livre).
    """
    pool = session.pool
    if pool is None:
        return False
    async with session.rotation_lock:
        if generation is not None and generation != session.generation:
            return True
        failed_label = pool.current_label
        previous_exit = pool.current
        if not pool.mark_failed():
            return False
        switched, wait = pool.pick_best()
        if limiter is not None:
            if switched:
                await limiter.switch_exit(previous_exit, pool.current)
            await limiter.set_cooldown(wait)
        if switched:
            logger.warning(
                "Proxy %s fora do rodizio apos falhas seguidas (conexao/timeout). "
                "Alternando para %s.", failed_label, pool.current_label,
            )
            await relaunch_session(session, logger)
        else:
            logger.warning(
                "Proxy %s falhou seguidamente e nao ha outra saida livre. "
                "Nova tentativa em %s.", failed_label, format_duration(wait),
            )
        return switched


async def note_page_timeout(
    session: BrowserSession, limiter, logger, reason: str,
    started_at: float | None = None, generation: int | None = None,
) -> bool:
    """Trata o timeout de uma pagina. Retorna True se ela saiu por um proxy.

    Por proxy, o suspeito e a saida (lenta ou morta), nao o servidor: conta
    como falha dela e o ritmo dos requests NAO muda. Em conexao direta, pode
    ser o servidor segurando o IP: vale como bloqueio (cooldown + desacelera).
    """
    pool = session.pool
    if pool is not None and pool.current is not None:
        await note_exit_failure(session, logger, limiter=limiter, generation=generation)
        return True
    await limiter.report_block(logger, reason, started_at=started_at)
    return False


async def classify_page_block(page, response) -> BlockInfo | None:
    """Classifica a pagina recem-navegada: bloqueio (rate limit/challenge/5xx) ou None.

    Decide pelo status e headers da resposta. O HTML so e lido quando ha
    suspeita (status fora de 2xx, ou pagina 2xx que nao e do Confluence): uma
    pagina de documentacao normal nunca e marcada como bloqueio por conter,
    no texto ou em scripts, palavras que lembram o Cloudflare.
    """
    status = response.status if response is not None else None
    headers = response.headers if response is not None else {}
    if status == 429:
        return classify_response(status, headers)
    try:
        title = await page.title()
    except PlaywrightError:
        title = ""
    try:
        is_confluence = bool(await page.evaluate(
            "() => !!document.querySelector('meta[name=\"ajs-page-id\"], #main-content')"
        ))
    except PlaywrightError:
        is_confluence = False
    if status is not None and 200 <= status < 300 and is_confluence:
        return None
    try:
        body = (await page.content())[:20_000]
    except PlaywrightError:
        body = ""
    return classify_response(
        status, headers, title=title, body=body, is_confluence_page=is_confluence,
    )


def describe_exit(session: BrowserSession) -> str:
    """Texto curto da saida de rede atual, sem credenciais (para logs)."""
    return mask_proxy(session.pool.current if session.pool is not None else session.proxy)


async def save_storage_state(session: BrowserSession, logger) -> None:
    """Salva cookies/storage no caminho configurado (se houver).

    Usa lock global para impedir 2 workers escreverem simultaneamente — race
    poderia gerar JSON parcial/corrompido. Se save falhar, loga warning mas
    nao interrompe (cookies podem ser regenerados na proxima sessao).
    """
    if session.state_path is None or session.context is None:
        return
    if not _uses_shared_cookies(session.pool):
        return
    async with _storage_state_lock:
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
