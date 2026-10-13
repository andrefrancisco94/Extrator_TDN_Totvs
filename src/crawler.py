from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urljoin, urlparse

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

from .blocking import classify_response, is_transient_network_error, short_error
from .browser import (
    BrowserSession,
    classify_page_block,
    is_browser_alive as _is_browser_alive_helper,
    launch_browser_session as _launch_browser_session_helper,
    new_page as _new_page,
    note_exit_failure,
    note_page_timeout,
    relaunch_session,
    rotate_exit_after_block,
    safe_close_page as _safe_close_page,
    teardown_session as _teardown_session,
)
from .proxy import ProxyPool
from .utils import (
    Checkpoint,
    CrawlScope,
    MAX_RETRY_ATTEMPTS,
    RateLimitConfig,
    RateLimiter,
    SlowPageRecord,
    canonicalize_url,
    get_console,
    is_url_in_scope,
    parse_scope,
)

__all__ = ["crawl_confluence_tree", "CrawlResult"]

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

from .utils import PAGE_ROTATION_INTERVAL as _PAGE_ROTATION_INTERVAL  # noqa: PLC0415

# Salva snapshot do crawl a cada N requests de mapeamento. Permite resume
# parcial se o processo for interrompido no meio.
_CRAWL_CHECKPOINT_EVERY = 10

# Maximo de re-tentativas dentro do mesmo run para URLs que deram TIMEOUT de
# navegacao (modo DOM). Diferente de MAX_RETRY_ATTEMPTS (que conta entre runs).
_INTRA_RUN_RETRY_LIMIT = 2

# Um bloqueio (429/1015) e propriedade do IP, nao da pagina: o MESMO no e
# repetido depois do cooldown. So apos tantos bloqueios seguidos no mesmo no o
# crawl para (salvando o ponto exato para o proximo run retomar).
_NODE_BLOCK_RETRIES = 8
# Erros de rede/servidor num mesmo no antes de deixa-lo pendente.
_NODE_ERROR_RETRIES = 4
# Nos seguidos sem conseguir listar filhos = rede fora do ar: para o crawl.
_MAX_CONSECUTIVE_UNRESOLVED = 5
# REST API negada (401/403/404) em tantos nos seguidos: desliga o modo REST e
# segue pelo DOM da arvore lateral.
_API_UNAVAILABLE_LIMIT = 3

# Cap do cache REST API intra-run (evita memory leak em sites enormes)
_API_CACHE_MAX_ENTRIES = 5000

_OK = "ok"
_BLOCKED = "blocked"
_FAILED = "failed"
_TIMEOUT = "timeout"
_UNAVAILABLE = "unavailable"

_RESOLVED = "resolved"
_PENDING = "pending"
_STOP = "stop"


class CrawlResult(NamedTuple):
    """Resultado do mapeamento.

    complete=False significa que ha paginas cujos filhos nao puderam ser
    listados (bloqueio persistente, rede fora): o mapa pode estar faltando
    subarvores e o proximo run retoma exatamente desses pontos.
    """
    urls: list[str]
    slow_records: list[SlowPageRecord]
    complete: bool
    note: str


@dataclass(frozen=True)
class ChildRef:
    """Filho descoberto: URL + o que a REST API ja informou sobre ele."""
    url: str
    page_id: str | None = None
    # True = sem filhos (nao precisa de request); None = desconhecido.
    is_leaf: bool | None = None


@dataclass
class ChildrenResult:
    """Resultado de listar os filhos de UM no. So _OK resolve o no: nunca se
    conclui "0 filhos" a partir de erro, bloqueio ou status inesperado."""
    status: str
    children: list[ChildRef] = field(default_factory=list)
    retry_after: float | None = None
    detail: str = ""
    transient: bool = False
    proxy_error: bool = False
    source: str = "REST API"


@dataclass
class CrawlState:
    """Estado mutavel do crawl (BFS + slow records)."""
    ordered_urls: list[str] = field(default_factory=list)
    # Espelha o conteudo de ordered_urls como set, mas NUNCA eh removido
    # (diferente de `seen`, que e descartado em timeout-retry para permitir
    # reprocessamento). Garante que uma URL entra em ordered_urls no maximo
    # uma vez, mesmo que passe por varios timeouts/retries intra-run.
    mapped_set: set[str] = field(default_factory=set)
    slow_records: list[SlowPageRecord] = field(default_factory=list)
    # Nos ja RESOLVIDOS (filhos listados) ou em processamento.
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
    # url -> pageId. Com o id em maos os filhos sao listados so pela REST API,
    # sem carregar a pagina no browser.
    page_ids: dict[str, str] = field(default_factory=dict)
    # Paginas que a REST API ja disse nao ter filhos: nenhum request necessario.
    known_leaves: set[str] = field(default_factory=set)
    # Nos mapeados cujos filhos NAO puderam ser listados neste run. Enquanto
    # houver algum, o crawl nao e marcado como completo.
    unresolved: list[str] = field(default_factory=list)
    api_mode: bool = True
    # False depois que o servidor recusa expand=children.page (HTTP 400).
    api_expand: bool = True
    # URL inicial (canonica): erro HTTP nela nunca pode virar "sem filhos".
    start_url: str = ""
    # Re-mapeamento de um job que ja tem mapa completo: nada e gravado no
    # manifest ate o novo mapa terminar.
    staged: bool = False
    api_unavailable_streak: int = 0
    # No sendo resolvido agora: se o run for interrompido no meio, ele volta
    # para a frente da fila antes de o progresso ser gravado.
    in_flight: str | None = None
    api_requests: int = 0
    navigations: int = 0
    stop_reason: str = ""


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
    pool: ProxyPool | None = None


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


def _page_id_from_url(url: str) -> str | None:
    """Extrai o pageId de URLs /pages/viewpage.action?pageId=N."""
    try:
        values = parse_qs(urlparse(url).query).get("pageId") or []
    except ValueError:
        return None
    return values[0] if values and values[0].isdigit() else None


async def _get_page_id(page: Page) -> str | None:
    """Le meta ajs-page-id da pagina (Confluence Server/DC)."""
    try:
        return await page.evaluate(
            "() => document.querySelector('meta[name=\"ajs-page-id\"]')?.getAttribute('content') || null"
        )
    except PlaywrightError:
        return None


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


def _child_refs_from_results(
    results: list[dict], base: str, scope: CrawlScope,
) -> list[ChildRef]:
    """Converte `results` da REST API em ChildRef (url, id, e se e folha).

    Com expand=children.page cada filho ja traz a quantidade de filhos dele:
    size == 0 identifica uma folha, que nao precisa de request proprio. Em
    arvores de documentacao ~85% dos nos sao folhas.
    """
    refs: list[ChildRef] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        link = (item.get("_links") or {}).get("webui", "")
        if not link:
            continue
        raw_id = item.get("id")
        page_id = str(raw_id) if raw_id not in (None, "") else None
        is_leaf: bool | None = None
        grandchildren = (item.get("children") or {}).get("page")
        if isinstance(grandchildren, dict):
            size = grandchildren.get("size")
            listed = grandchildren.get("results")
            if isinstance(size, int):
                is_leaf = size == 0
            elif isinstance(listed, list):
                is_leaf = len(listed) == 0
        refs.append(ChildRef(
            url=_match_start_url_format(base + link, scope),
            page_id=page_id, is_leaf=is_leaf,
        ))
    return refs


_FETCH_JS = """
async ([url, timeoutMs]) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      credentials: 'same-origin',
      headers: {'Accept': 'application/json'},
      signal: controller.signal,
    });
    const body = await response.text();
    return {
      status: response.status,
      retryAfter: response.headers.get('retry-after'),
      mitigated: response.headers.get('cf-mitigated'),
      body,
    };
  } catch (error) {
    return {error: String(error && error.message ? error.message : error)};
  } finally {
    clearTimeout(timer);
  }
}
"""


def _same_origin(page: Page | None, url: str) -> bool:
    """True se a page esta carregada no mesmo SITE da URL (pode usar fetch).

    Compara host:porta sem diferenciar maiusculas e SEM o esquema: se o
    servidor redireciona http -> https, a page continua valendo (as chamadas
    REST sao montadas na origem em que o browser realmente esta).
    """
    if page is None:
        return False
    try:
        page_url = page.url
        if not isinstance(page_url, str):
            return False
        current, target = urlparse(page_url), urlparse(url)
    except (PlaywrightError, ValueError):
        return False
    return (
        current.scheme in ("http", "https")
        and bool(target.netloc)
        and current.netloc.lower() == target.netloc.lower()
    )


def _site_base(page: Page | None, fallback_url: str) -> str:
    """Origem (esquema://host) em que o browser esta, para montar a chamada REST.

    Um fetch() so e same-origin se usar exatamente a origem da pagina aberta.
    """
    parsed = urlparse(fallback_url)
    if _same_origin(page, fallback_url):
        try:
            current = urlparse(page.url)
            return f"{current.scheme}://{current.netloc}"
        except (PlaywrightError, ValueError):
            pass
    return f"{parsed.scheme}://{parsed.netloc}"


async def _http_get(page: Page, url: str) -> tuple[int | None, dict, str, str]:
    """GET de um endpoint do site. Retorna (status, headers, corpo, erro).

    Usa fetch() de dentro da pagina ja aberta no site: o request sai pela
    pilha de rede do proprio Chromium (mesmos cookies, proxy, certificados e
    cabecalhos de uma navegacao real). O APIRequestContext do Playwright usa a
    pilha TLS do Node, que falha em redes com inspecao TLS ("self-signed
    certificate in certificate chain"); fica so como reserva.
    """
    fetch_error = ""
    if _same_origin(page, url):
        try:
            data = await page.evaluate(_FETCH_JS, [url, _API_TIMEOUT_MS])
        except PlaywrightError as exc:
            data = {"error": short_error(exc)}  # pagina navegando/fechada
        if isinstance(data, dict) and "status" in data:
            headers = {}
            if data.get("retryAfter"):
                headers["retry-after"] = data["retryAfter"]
            if data.get("mitigated"):
                headers["cf-mitigated"] = data["mitigated"]
            return int(data["status"]), headers, str(data.get("body") or ""), ""
        # fetch nao obteve resposta (rede, pagina descartada...): tenta a
        # reserva antes de desistir. Uma RESPOSTA (inclusive 429) nunca cai aqui.
        fetch_error = str((data or {}).get("error") or "falhou") if isinstance(data, dict) else "falhou"
    try:
        response = await page.request.get(url, timeout=_API_TIMEOUT_MS)
        body = await response.text()
        return response.status, dict(response.headers), body, ""
    except PlaywrightError as exc:
        detail = short_error(exc)
        if fetch_error:
            detail = f"fetch: {fetch_error}; APIRequestContext: {detail}"
        return None, {}, "", detail


# Status com que o Confluence recusa um parametro que nao entende.
_BAD_REQUEST = "bad-request"


@dataclass
class _ApiPage:
    """Uma pagina de resultados da REST API (child/page)."""
    status: str
    results: list[dict] = field(default_factory=list)
    retry_after: float | None = None
    detail: str = ""
    transient: bool = False
    # Paginacao informada pela RESPOSTA: o servidor pode aplicar um limite
    # menor que o pedido (ex: quando ha `expand`).
    limit: int | None = None
    has_next: bool = False

    @property
    def more(self) -> bool:
        if self.has_next:
            return True
        size = len(self.results)
        if self.limit:
            return size >= self.limit
        return size >= _API_PAGE_SIZE


async def _api_fetch_children_page(page: Page, api_url: str, logger) -> _ApiPage:
    """Um GET na REST API (lista de filhos de uma pagina)."""
    status, headers, body, error = await _http_get(page, api_url)
    if status is None:
        logger.debug("REST API falhou em %s: %s", api_url, error)
        return _ApiPage(_FAILED, detail=error, transient=True)

    if status != 200:
        block = classify_response(status, headers, body=body[:4000])
        if block is not None:
            return _ApiPage(
                _BLOCKED, retry_after=block.retry_after, detail=f"REST API: {block.describe()}",
            )
        logger.debug("REST API status %s em %s", status, api_url)
        if status == 400:
            return _ApiPage(_BAD_REQUEST, detail="REST API HTTP 400")
        if status in (401, 403, 404, 405, 410, 501):
            return _ApiPage(_UNAVAILABLE, detail=f"REST API HTTP {status}")
        return _ApiPage(_FAILED, detail=f"REST API HTTP {status}", transient=True)

    try:
        data = json.loads(body)
    except ValueError as exc:
        logger.debug("REST API JSON invalido: %s", exc)
        block = classify_response(status, headers, body=body[:4000])
        if block is not None:
            return _ApiPage(
                _BLOCKED, retry_after=block.retry_after, detail=f"REST API: {block.describe()}",
            )
        return _ApiPage(_UNAVAILABLE, detail="REST API devolveu conteudo que nao e JSON")
    if not isinstance(data, dict):
        return _ApiPage(_UNAVAILABLE, detail="REST API devolveu JSON inesperado")
    results = data.get("results", []) or []
    limit = data.get("limit")
    return _ApiPage(
        _OK,
        results=results if isinstance(results, list) else [],
        limit=limit if isinstance(limit, int) and limit > 0 else None,
        has_next=bool((data.get("_links") or {}).get("next")),
    )


async def _api_list_children(
    page: Page,
    page_id: str,
    current_url: str,
    scope: CrawlScope,
    logger,
    state: CrawlState,
    limiter: RateLimiter | None = None,
) -> ChildrenResult:
    """Lista os filhos de uma pagina pela REST API do Confluence (sem navegar).

    Um request devolve ate 200 filhos, ja com id, URL e contagem de filhos de
    cada um. Paginas extras passam pelo limiter. A paginacao segue o que a
    RESPOSTA informa (limit/_links.next), nao o tamanho pedido: se o servidor
    limitar a pagina a menos de 200, os filhos restantes nao sao perdidos.
    """
    parsed = urlparse(current_url)
    base = f"{parsed.scheme}://{parsed.netloc}"       # forma das URLs do job
    request_base = _site_base(page, current_url)      # origem real do browser
    children: list[ChildRef] = []
    start = 0

    for page_number in range(_API_MAX_PAGES):
        cache_key = (page_id, start)
        async with state.state_lock:
            api = state.api_cache.get(cache_key)
        if api is None:
            if page_number > 0 and limiter is not None:
                await limiter.wait()
            plain_url = (
                f"{request_base}/rest/api/content/{page_id}/child/page"
                f"?limit={_API_PAGE_SIZE}&start={start}"
            )
            api_url = plain_url + ("&expand=children.page" if state.api_expand else "")
            state.api_requests += 1
            api = await _api_fetch_children_page(page, api_url, logger)
            if api.status == _BAD_REQUEST and state.api_expand:
                # Servidor recusou o expand: segue sem ele (um request por
                # pagina, ainda sem navegar) em vez de falhar o mapeamento.
                state.api_expand = False
                logger.warning(
                    "REST API recusou expand=children.page (HTTP 400); seguindo sem "
                    "ele: folhas passam a custar um request cada."
                )
                if limiter is not None:
                    await limiter.wait()
                state.api_requests += 1
                api = await _api_fetch_children_page(page, plain_url, logger)
            if api.status != _OK:
                status = _UNAVAILABLE if api.status == _BAD_REQUEST else api.status
                return ChildrenResult(
                    status, retry_after=api.retry_after, detail=api.detail,
                    transient=api.transient,
                )
            async with state.state_lock:
                _bounded_cache_set(state.api_cache, cache_key, api)
        children.extend(_child_refs_from_results(api.results, base, scope))
        if not api.results or not api.more:
            break
        start += len(api.results)
    else:
        logger.warning(
            "Pagina %s tem mais de %d filhos: a lista foi truncada nesse ponto.",
            current_url, len(children),
        )

    state.api_unavailable_streak = 0
    return ChildrenResult(_OK, children=children)


def _note_api_unavailable(state: CrawlState, logger, detail: str) -> None:
    """Conta respostas "REST API negada"; apos algumas seguidas desliga o modo
    REST para nao gastar um request inutil por pagina no resto do crawl.

    So um sucesso da propria REST API zera a contagem (um fallback pelo DOM
    que funcionou nao prova que a API voltou).
    """
    state.api_unavailable_streak += 1
    if state.api_mode and state.api_unavailable_streak >= _API_UNAVAILABLE_LIMIT:
        state.api_mode = False
        logger.warning(
            "REST API do Confluence indisponivel (%s). Mapeando pelo DOM da "
            "arvore lateral (mais lento: uma navegacao por pagina).",
            detail,
        )


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
        if session.playwright is None:
            session.playwright = playwright
        async with session.rotation_lock:
            await relaunch_session(session, logger)


async def _recreate_page(session: BrowserSession, cfg: CrawlConfig) -> None:
    """Descarta a page atual (estado incerto apos erro/bloqueio) e abre outra."""
    await _safe_close_page(session.page)
    session.page = await _new_page(session.context, cfg.timeout_ms)


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

    # mapped_set espelha ordered_urls e NUNCA e descartado (diferente de
    # `seen` abaixo) — impede re-adicionar a mesma URL apos timeout-retry.
    state.mapped_set = set(seen_so_far)
    # "Visto" = filhos ja listados. So vale o que foi GRAVADO como visto:
    #  - um no que esta na FILA ainda nao foi resolvido, mesmo que conste em
    #    mapped_urls (aguardava nova tentativa quando o run parou);
    #  - constar em mapped_urls nao prova que os filhos foram listados (mapa
    #    parcial sem fila = estado inconsistente: recomeca da pagina inicial,
    #    e mapped_set impede duplicatas).
    # Tratar esses nos como vistos os descartaria em silencio com a subarvore.
    state.seen = set(saved_seen) - set(saved_queue)
    # Re-enqueua o que estava na fila
    for url in saved_queue:
        if url not in state.queued and url not in state.seen:
            state.queue.append(url)
            state.queued.add(url)

    # Desistencias do CRAWL de runs anteriores (filhos nunca listados) voltam
    # para a fila. Falhas de EXPORTACAO nao: a pagina ja teve os filhos
    # listados, e quem a re-tenta e o exportador (re-enfileirar custaria um
    # request por pagina a toa, justamente quando o IP esta sendo limitado).
    retry_count = 0
    for url in checkpoint.crawl_giveups():
        if not _is_valid_url_str(url):
            continue
        state.seen.discard(url)
        if url not in state.queued:
            state.queue.append(url)
            state.queued.add(url)
            retry_count += 1
    if retry_count:
        logger.info(
            "Re-enfileirando %d pagina(s) cujos filhos nao foram listados em run anterior.",
            retry_count,
        )

    # Ids de pagina aprendidos antes: evitam navegar so para descobrir o id.
    for url, page_id in (checkpoint.manifest.page_ids or {}).items():
        state.page_ids.setdefault(url, page_id)
    # Folhas ja identificadas pela REST API: continuam sem custar request.
    state.known_leaves.update(
        u for u in (checkpoint.manifest.crawl_leaves or []) if _is_valid_url_str(u)
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


def _reset_for_force_recrawl(checkpoint: Checkpoint, logger, start_url: str = "") -> None:
    """Descarta o mapa PARCIAL do manifest quando force_recrawl=True.

    Grava um estado coerente de "crawl recomecando" (sem mapa, fila = pagina
    inicial). Deixar o mapa parcial com a fila vazia faria um resume posterior
    tomar todas as paginas mapeadas por resolvidas e fechar o mapa incompleto.
    """
    if not checkpoint.manifest.mapped_urls:
        return
    logger.info(
        "Re-crawl forcado: ignorando %d URLs cacheadas do run anterior.",
        len(checkpoint.manifest.mapped_urls),
    )
    try:
        checkpoint.save_crawl_progress(
            ordered_urls=[], queue=[start_url] if start_url else [], seen=[],
            max_pages=checkpoint.manifest.crawl_max_pages, leaves=[],
        )
    except OSError as exc:
        logger.warning("Falha ao limpar manifest no force_recrawl: %s", exc)


def _pending_nodes(state: CrawlState) -> list[str]:
    """Nos que ainda precisam ter os filhos listados (pendentes + fila)."""
    return list(dict.fromkeys(list(state.unresolved) + list(state.queue)))


def _finalize_crawl(
    state: CrawlState,
    checkpoint: Checkpoint,
    max_pages: int | None,
    logger,
    staged_previous: list[str] | None = None,
) -> tuple[list[str], bool, str]:
    """Decide se marca crawl_complete ou salva progresso parcial.

    Invariante: o crawl so e completo quando a fila esta vazia E todo no
    mapeado teve os filhos listados com sucesso. Bloqueio, erro ou desistencia
    em qualquer no deixa o crawl incompleto, com o no guardado na fila para o
    proximo run retomar exatamente dali.

    staged_previous: mapa completo anterior, quando este crawl e um
    re-mapeamento (--update). Se o re-mapeamento nao terminar, o mapa anterior
    e mantido intacto em vez de ser trocado por uma lista parcial.

    Retorna (urls, completo, observacao).
    """
    pending = _pending_nodes(state)
    limited = state.stop_reason == "max_pages"
    if state.ordered_urls and not pending:
        if staged_previous is not None and len(state.ordered_urls) == 1 and len(staged_previous) > 1:
            # Re-mapeamento "completo" so com a pagina inicial, sobre um mapa
            # que tinha mais paginas: quase sempre e a pagina inicial sem
            # responder direito, nao a documentacao que sumiu. Nao troca.
            note = "o re-mapeamento encontrou so a pagina inicial"
            logger.warning(
                "Re-mapeamento descartado: %s (o mapa anterior tem %d paginas e "
                "foi mantido). Se a arvore realmente mudou, rode com --fresh.",
                note, len(staged_previous),
            )
            return list(staged_previous), False, note
        checkpoint.record_crawl_complete(
            state.ordered_urls, max_pages=max_pages, page_ids=state.page_ids,
        )
        return state.ordered_urls, True, ""

    note = state.stop_reason or (
        f"{len(pending)} pagina(s) sem a lista de filhos (rede/bloqueio)"
    )
    if staged_previous is not None and not limited:
        logger.warning(
            "Re-mapeamento NAO concluido (%s). O mapa anterior, com %d paginas, "
            "foi mantido sem alteracao. Rode a verificacao de atualizacoes de "
            "novo mais tarde.",
            note, len(staged_previous),
        )
        return list(staged_previous), False, note

    if limited:
        logger.warning(
            "Crawl limitado por max_pages: ainda restam %d URLs na fila.",
            len(pending),
        )
    else:
        logger.warning(
            "Mapeamento INCOMPLETO (%s): %d pagina(s) ainda precisam ter os "
            "filhos listados. A proxima execucao retoma exatamente desses pontos.",
            note, len(pending),
        )
    pending_set = set(pending)
    checkpoint.save_crawl_progress(
        ordered_urls=state.ordered_urls,
        queue=pending,
        seen=[u for u in state.seen if u not in pending_set],
        max_pages=max_pages,
        page_ids=state.page_ids,
        leaves=[u for u in pending if u in state.known_leaves],
    )
    return state.ordered_urls, limited, "" if limited else note


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
    pool: ProxyPool | None = None,
) -> CrawlResult:
    """BFS pela arvore de paginas do Confluence.

    Navega UMA vez (pagina inicial) e lista os filhos de cada no pela REST API,
    usando o id que a propria API devolve. Folhas nao custam request. O DOM da
    arvore lateral so e usado como fallback quando a REST API e negada.

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
        pool=pool,
    )

    previous_complete = bool(
        checkpoint.manifest.crawl_complete and checkpoint.manifest.mapped_urls
    )

    # Resume completo: crawl ja terminou anteriormente
    if not force_recrawl and previous_complete:
        giveups = checkpoint.crawl_giveups()
        if not giveups:
            logger.info(
                "Resume: reutilizando crawl anterior com %d URLs mapeadas.",
                len(checkpoint.manifest.mapped_urls),
            )
            return CrawlResult(list(checkpoint.manifest.mapped_urls), [], True, "")
        # Mapa "completo" que na verdade tem paginas sem os filhos listados
        # (versoes antigas desistiam apos 2 bloqueios e fechavam o mapa assim):
        # devolve-lo como completo esconderia subarvores inteiras. Re-mapeia.
        logger.warning(
            "Este mapa foi fechado por uma versao antiga que desistiu de %d "
            "pagina(s) sem listar os filhos delas: pode estar faltando "
            "subarvores. Re-mapeando a arvore para completar (o mapa atual so "
            "e substituido se o novo terminar; PDFs existentes sao reusados).",
            len(giveups),
        )
        force_recrawl = True

    # Re-mapeamento de um job que ja tem mapa completo: o novo mapa fica em
    # memoria e so substitui o antigo se terminar inteiro.
    staged_previous: list[str] | None = None
    if force_recrawl:
        if previous_complete:
            staged_previous = list(checkpoint.manifest.mapped_urls)
            logger.info(
                "Re-mapeando a arvore (mapa atual: %d paginas, preservado ate o "
                "novo mapeamento terminar).",
                len(staged_previous),
            )
        else:
            _reset_for_force_recrawl(checkpoint, logger, start_canonical)

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
    state.start_url = start_canonical
    state.staged = staged_previous is not None

    if not force_recrawl:
        _restore_state_from_checkpoint(state, checkpoint, start_canonical, logger)

    console = get_console()
    active_limiter = limiter if limiter is not None else RateLimiter(cfg.rate_limit, logger)

    async with async_playwright() as playwright:
        session: BrowserSession | None = None
        try:
            # block_extras: a pagina inicial fica aberta durante todo o
            # mapeamento; sem o bloqueio, a telemetria/polling dela seguiria
            # gastando requests.
            session = await _launch_browser_session(
                playwright, headless, timeout_ms, logger,
                state_path=state_path, proxy=proxy, pool=pool, block_extras=True,
            )
            await _run_crawl_loop(
                session, state, scope, playwright, cfg, logger, console,
                checkpoint, active_limiter, staged=staged_previous is not None,
            )
            await _safe_close_page(session.page)
        except BaseException:
            # Ctrl+C, ban acima do limite, falha ao abrir o browser, erro
            # inesperado: grava o ponto exato antes de propagar (um
            # re-mapeamento em andamento e so descartado: o mapa completo
            # anterior continua valendo).
            if staged_previous is None:
                _requeue_in_flight(state)
                _save_partial_progress(checkpoint, state, cfg, logger)
            raise
        finally:
            if session is not None:
                await _teardown_session(session, logger)

    urls, complete, note = _finalize_crawl(
        state, checkpoint, max_pages, logger, staged_previous=staged_previous,
    )
    logger.info(
        "Crawl finalizado: %d pagina(s) mapeada(s) com %d navegacao(oes) e %d "
        "request(s) de REST API (%d folhas sem request), %d lenta(s), fila restante=%d.",
        len(state.ordered_urls), state.navigations, state.api_requests,
        len(state.known_leaves & state.mapped_set), len(state.slow_records),
        len(_pending_nodes(state)),
    )
    return CrawlResult(urls, state.slow_records, complete, note)


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
    staged: bool = False,
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
        requests_at_last_save = 0
        consecutive_unresolved = 0

        while state.queue:
            current = state.queue.popleft()
            state.queued.discard(current)

            if current in state.seen:
                continue

            state.seen.add(current)
            is_new = current not in state.mapped_set
            if is_new:
                state.mapped_set.add(current)
                state.ordered_urls.append(current)
            short = current if len(current) <= 60 else current[:57] + "..."
            progress.update(
                task, advance=1 if is_new else 0, queue=len(state.queue),
                description=f"Mapeando: {short}",
            )

            if cfg.max_pages and len(state.ordered_urls) >= cfg.max_pages:
                logger.warning("Limite max_pages=%s atingido.", cfg.max_pages)
                state.stop_reason = "max_pages"
                # A pagina entra no mapa (sera exportada), mas seus filhos ainda
                # nao foram listados: volta para a fila. Marca-la como resolvida
                # faria um run posterior, sem limite, nunca listar a subarvore.
                state.seen.discard(current)
                state.queue.appendleft(current)
                state.queued.add(current)
                break

            state.in_flight = current
            outcome = await _resolve_node(
                session, current, state, scope, playwright, cfg, logger, limiter,
                checkpoint,
            )
            state.in_flight = None
            progress.update(task, queue=len(state.queue))

            if outcome == _STOP:
                # Devolve o no para a FRENTE da fila: o proximo run recomeca dele.
                state.seen.discard(current)
                state.queue.appendleft(current)
                state.queued.add(current)
                break
            if outcome == _PENDING:
                consecutive_unresolved += 1
                if consecutive_unresolved >= _MAX_CONSECUTIVE_UNRESOLVED:
                    state.stop_reason = (
                        f"{consecutive_unresolved} paginas seguidas sem resposta "
                        "(rede ou servidor fora do ar)"
                    )
                    logger.error("Crawl interrompido: %s.", state.stop_reason)
                    break
            else:
                consecutive_unresolved = 0

            # Snapshot periodico para resume parcial (por requests feitos:
            # folhas conhecidas passam direto e nao justificam gravar o manifest).
            done = state.api_requests + state.navigations
            if not staged and done - requests_at_last_save >= _CRAWL_CHECKPOINT_EVERY:
                requests_at_last_save = done
                _save_partial_progress(checkpoint, state, cfg, logger)


def _requeue_in_flight(state: CrawlState) -> None:
    """Devolve para a frente da fila o no que estava sendo resolvido quando o
    crawl foi interrompido (ele ainda nao teve os filhos listados)."""
    current, state.in_flight = state.in_flight, None
    if not current or current in state.queued or current in state.unresolved:
        return
    state.seen.discard(current)
    state.queue.appendleft(current)
    state.queued.add(current)


def _save_partial_progress(
    checkpoint: Checkpoint, state: CrawlState, cfg: CrawlConfig, logger,
) -> None:
    pending = _pending_nodes(state)
    pending_set = set(pending)
    try:
        checkpoint.save_crawl_progress(
            ordered_urls=state.ordered_urls,
            queue=pending,
            seen=[u for u in state.seen if u not in pending_set],
            max_pages=cfg.max_pages,
            page_ids=state.page_ids,
            leaves=[u for u in pending if u in state.known_leaves],
        )
    except OSError as exc:
        logger.warning("Falha ao salvar progresso parcial: %s", exc)


async def _maybe_rotate_page(
    session: BrowserSession, state: CrawlState, cfg: CrawlConfig, logger,
) -> None:
    """Troca a page a cada N navegacoes (libera memoria em crawls pelo DOM)."""
    if state.navigations == 0 or state.navigations % _PAGE_ROTATION_INTERVAL != 0:
        return
    logger.info("Rotacionando pagina apos %s navegacoes", state.navigations)
    await _recreate_page(session, cfg)


def _mark_unresolved(current: str, state: CrawlState) -> None:
    """Registra que os filhos de `current` nao foram listados neste run."""
    state.seen.discard(current)
    if current not in state.unresolved:
        state.unresolved.append(current)


async def _resolve_node(
    session: BrowserSession,
    current: str,
    state: CrawlState,
    scope: CrawlScope,
    playwright,
    cfg: CrawlConfig,
    logger,
    limiter: RateLimiter,
    checkpoint: Checkpoint,
) -> str:
    """Lista os filhos de `current` e os enfileira.

    Retorna _RESOLVED (filhos listados), _PENDING (nao foi possivel agora; o
    no fica pendente e o crawl segue) ou _STOP (bloqueio persistente: parar).

    Bloqueio por rate limit repete o MESMO no depois do cooldown, sem gastar
    tentativa de outra URL e sem dar a pagina por resolvida.
    """
    if current in state.known_leaves:
        logger.debug("Folha (sem request): %s", current)
        return _RESOLVED

    blocks = 0
    errors = 0
    proxy_timeouts = 0
    force_navigation = False
    skip_api = False

    while True:
        await _ensure_browser_alive(session, playwright, cfg, logger)
        page_id = state.page_ids.get(current) or _page_id_from_url(current)
        if page_id:
            state.page_ids.setdefault(current, page_id)
        # So usa a REST API direto se a page ja esta aberta no site (cookies de
        # sessao + fetch pela pilha do browser). Caso contrario navega primeiro.
        primed = _same_origin(session.page, current)
        use_api = bool(state.api_mode and page_id and primed and not force_navigation)

        await limiter.wait()
        started_at = time.monotonic()
        generation = session.generation
        logger.info(
            "Mapeando (%s/%s, fila=%s%s): %s",
            len(state.ordered_urls), cfg.max_pages or "infinito", len(state.queue),
            "" if use_api else ", navegando", current,
        )
        if use_api:
            result = await _api_list_children(
                session.page, page_id, current, scope, logger, state, limiter,
            )
        else:
            await _maybe_rotate_page(session, state, cfg, logger)
            result = await _navigate_and_list(
                session, current, state, scope, cfg, logger, limiter, skip_api=skip_api,
            )
            limiter.mark_done()

        if result.status == _OK:
            await limiter.report_success()
            if session.pool is not None:
                session.pool.mark_ok()
            if result.children:
                logger.info(
                    "%s: %d filhos em %s", result.source, len(result.children), current,
                )
            else:
                logger.info("Nenhum link filho encontrado em %s", current)
            _enqueue_children(result.children, current, state, scope)
            return _RESOLVED

        if result.status == _BLOCKED:
            blocks += 1
            cooldown = await limiter.report_block(
                logger, result.detail, retry_after=result.retry_after,
                started_at=started_at,
            )
            await rotate_exit_after_block(
                session, limiter, cooldown, logger, generation=generation,
            )
            if blocks >= _NODE_BLOCK_RETRIES:
                state.stop_reason = (
                    f"bloqueio persistente do servidor ({blocks} tentativas em {current})"
                )
                logger.error("Crawl interrompido: %s.", state.stop_reason)
                return _STOP
            continue

        if result.status == _UNAVAILABLE:
            # REST API negada para este no: lista os filhos pelo DOM da pagina
            # (sem perguntar a REST de novo para o mesmo id).
            _note_api_unavailable(state, logger, result.detail)
            force_navigation = True
            skip_api = True
            continue

        if result.status == _TIMEOUT:
            via_proxy = await note_page_timeout(
                session, limiter, logger, "timeout no crawl",
                started_at=started_at, generation=generation,
            )
            proxy_timeouts += via_proxy
            if via_proxy and proxy_timeouts < _NODE_BLOCK_RETRIES:
                # O suspeito e o proxy (na 3a falha seguida ele sai do rodizio):
                # repete o no em vez de gastar as tentativas da pagina.
                continue
            await _maybe_requeue_timed_out(current, state, logger, checkpoint)
            if current in state.queued:
                return _RESOLVED  # volta a ser processado mais adiante neste run
            _mark_unresolved(current, state)
            return _PENDING

        # _FAILED: erro de rede/servidor. Nunca vira "pagina sem filhos".
        errors += 1
        logger.warning(
            "Falha ao listar filhos de %s (tentativa %d/%d): %s",
            current, errors, _NODE_ERROR_RETRIES, result.detail,
        )
        if result.transient:
            await note_exit_failure(session, logger, limiter=limiter, generation=generation)
        if errors >= _NODE_ERROR_RETRIES:
            logger.error(
                "Filhos de %s nao listados apos %d tentativas; a pagina fica "
                "pendente para o proximo run (%s).",
                current, errors, result.detail,
            )
            _mark_unresolved(current, state)
            return _PENDING
        await asyncio.sleep(min(30.0, 3.0 * errors))


def _record_slow_crawl(
    current: str, elapsed_total: float, cfg: CrawlConfig, state: CrawlState, logger,
) -> None:
    if elapsed_total < cfg.slow_threshold_seconds:
        return
    state.slow_records.append(
        SlowPageRecord(url=current, elapsed_seconds=elapsed_total, phase="crawl")
    )
    logger.warning("Pagina lenta no crawl (%.1fs): %s", elapsed_total, current)


async def _navigate_and_list(
    session: BrowserSession,
    current: str,
    state: CrawlState,
    scope: CrawlScope,
    cfg: CrawlConfig,
    logger,
    limiter: RateLimiter,
    skip_api: bool = False,
) -> ChildrenResult:
    """Abre a pagina no browser e lista os filhos (REST API ou DOM da arvore).

    Usado para a primeira pagina de cada sessao (estabelece cookies e descobre
    o pageId) e como fallback quando a REST API nao esta disponivel.
    skip_api=True: a REST ja foi negada para este no; vai direto ao DOM.
    """
    page = session.page
    page_start = time.monotonic()
    state.navigations += 1
    try:
        response = await page.goto(
            current, wait_until="domcontentloaded", timeout=cfg.dom_budget_ms,
        )
        block = await classify_page_block(page, response)
        if block is not None:
            await _recreate_page(session, cfg)
            return ChildrenResult(
                _BLOCKED, retry_after=block.retry_after, detail=block.describe(),
            )
        status = response.status if response is not None else None
        if status is not None and status >= 400:
            if current == state.start_url or status not in (404, 410):
                # Erro HTTP nao e "pagina sem filhos" — muito menos na pagina
                # inicial: concluir isso fecharia o mapa com uma unica URL (e,
                # num --update, trocaria o mapa completo por ele).
                return ChildrenResult(_FAILED, detail=f"HTTP {status} ao abrir a pagina")
            # 404/410 numa pagina interna: foi removida depois de listada pelo
            # pai. Nao ha filhos a listar; o export registra a falha dela.
            logger.warning("HTTP %s ao abrir %s no crawl (pagina removida).", status, current)
            return ChildrenResult(_OK, source=f"HTTP {status}")

        page_id = await _get_page_id(page)
        if page_id:
            state.page_ids[current] = page_id
        else:
            logger.debug("Sem ajs-page-id em %s; pulando REST API", current)

        if state.api_mode and page_id and not skip_api:
            limiter.mark_done()
            await limiter.wait()
            result = await _api_list_children(
                page, page_id, current, scope, logger, state, limiter,
            )
            if result.status != _UNAVAILABLE:
                _record_slow_crawl(
                    current, time.monotonic() - page_start, cfg, state, logger,
                )
                return result
            _note_api_unavailable(state, logger, result.detail)
            logger.debug("REST API indisponivel, usando fallback DOM em %s", current)

        # Fallback: aguarda sidebar carregar e extrai via DOM.
        await _wait_for_sidebar(page, page_start, cfg)
        hrefs = await _extract_child_links(page, current)
        _record_slow_crawl(current, time.monotonic() - page_start, cfg, state, logger)
        return ChildrenResult(
            _OK, children=[ChildRef(url=href) for href in (hrefs or [])],
            source="DOM da arvore",
        )

    except PlaywrightTimeoutError:
        elapsed = time.monotonic() - page_start
        logger.error(
            "TIMEOUT no crawl apos %.1fs (limite %.0fs): %s",
            elapsed, cfg.timeout_ms / 1000, current,
        )
        state.slow_records.append(
            SlowPageRecord(url=current, elapsed_seconds=elapsed, phase="crawl-timeout")
        )
        await _recreate_page(session, cfg)
        return ChildrenResult(_TIMEOUT, detail=f"timeout apos {elapsed:.0f}s")

    except PlaywrightError as exc:
        logger.error("Falha de Playwright no crawl em %s: %s", current, short_error(exc))
        try:
            await _recreate_page(session, cfg)
        except PlaywrightError:
            pass  # browser morto: _ensure_browser_alive relanca na proxima volta
        return ChildrenResult(
            _FAILED, detail=short_error(exc),
            transient=is_transient_network_error(exc),
        )


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
        if state.staged:
            # Re-mapeamento de um mapa completo: se nao terminar, o mapa antigo
            # continua valendo e nao pode herdar um registro de desistencia.
            return
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


def _enqueue_children(
    children: list[ChildRef],
    current: str,
    state: CrawlState,
    scope: CrawlScope,
) -> None:
    """Enfileira os filhos e guarda o que a REST API informou (id, folha)."""
    for child in children:
        href = child.url
        if not href or href == "#" or href.lower().startswith("javascript:"):
            continue
        # Mesma forma de URL do start (/display/public/X ou /display/X) para
        # links vindos da REST API E do DOM: a mesma pagina nunca pode virar
        # duas chaves diferentes no manifest.
        absolute = canonicalize_url(
            _match_start_url_format(urljoin(current, href), scope)
        )
        if not is_url_in_scope(absolute, scope):
            continue
        if child.page_id:
            state.page_ids.setdefault(absolute, child.page_id)
        if child.is_leaf:
            state.known_leaves.add(absolute)
        if absolute in state.seen or absolute in state.queued:
            continue
        state.queued.add(absolute)
        state.queue.append(absolute)


def _enqueue_new_links(
    raw_hrefs: list[str],
    current: str,
    state: CrawlState,
    scope: CrawlScope,
) -> None:
    _enqueue_children([ChildRef(url=href) for href in raw_hrefs if href], current, state, scope)
