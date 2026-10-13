from __future__ import annotations

import asyncio
import os
import time
from contextlib import AsyncExitStack
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

from .blocking import (
    RATE_LIMIT,
    SERVER_ERROR,
    is_transient_network_error,
    short_error,
)
from .browser import (
    BrowserSession,
    classify_page_block,
    get_page_stats,
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
from .proxy import AllProxiesDeadError, ProxyPool
from .utils import (
    BlockedTooLongError,
    Checkpoint,
    MAX_RETRY_ATTEMPTS,
    RateLimitConfig,
    RateLimiter,
    SlowPageRecord,
    atomic_replace_with_retry,
    build_pdf_file_name,
    count_pdf_pages,
    ensure_output_dirs,
    get_console,
    get_free_disk_bytes,
    is_blocking_error,
    is_valid_pdf,
    sha256_file,
)

# Aliases para retrocompat com codigo interno do exporter
_launch_browser_session = _launch_browser_session_helper
_is_browser_alive = _is_browser_alive_helper

# Limite minimo de espaco em disco durante export (50MB). Abaixo disso,
# aciona abort_requested via circuit breaker.
_MIN_DISK_FREE_DURING_EXPORT = 50 * 1024 * 1024
_DISK_CHECK_EVERY_N = 25

_CONTENT_SELECTORS = (
    "#main-content",
    "#content",
    ".wiki-content",
    "article",
    "main",
)

from .utils import PAGE_ROTATION_INTERVAL as _PAGE_ROTATION_INTERVAL  # noqa: PLC0415

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

# Tentativas por URL dentro do mesmo run. Um bloqueio (429/1015) e do IP, nao
# da pagina: a MESMA URL e repetida depois do cooldown em vez de ser dada como
# falha e trocada pela proxima (que herdaria o cooldown no lugar dela).
_MAX_ATTEMPTS_PER_URL = 4
# Repescagens ao final do export para paginas que ainda falharam.
_FINAL_RETRY_ROUNDS = 2
_RETRY_ROUND_ATTEMPTS = 2

_OK = "ok"
_BLOCKED = "blocked"      # rate limit / desafio / 5xx: esperar cooldown e repetir
_TRANSIENT = "transient"  # erro de rede do cliente: pausa curta e repetir
_FAILED = "failed"        # erro inesperado: repetir uma vez
_TIMEOUT = "timeout"      # pagina estourou o orcamento: fica para a repescagem
_PERMANENT = "permanent"  # login / HTTP 4xx: repetir agora nao resolve


class TransientHTTPError(RuntimeError):
    """HTTP 5xx/429 que merece nova tentativa apos cooldown."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitedError(TransientHTTPError):
    """HTTP 429 / Cloudflare 1015 — na pagina ou em recursos dela (imagens)."""


class TransientNetworkError(RuntimeError):
    """Recursos da pagina falharam por erro de rede do lado do cliente."""


class PageHTTPError(RuntimeError):
    """A pagina respondeu HTTP 4xx (removida, restrita): repetir nao resolve."""


class LoginPageError(RuntimeError):
    """Pagina retornou tela de login (Confluence privado / sessao expirada)."""


class CloudflareChallengeError(RuntimeError):
    """Pagina retornou desafio/bloqueio do Cloudflare (anti-bot)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


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
    pool: ProxyPool | None = None


@dataclass
class ExportOutcome:
    """Resultado de UMA tentativa de exportar uma URL."""
    status: str
    error: str = ""
    retry_after: float | None = None
    phase: str | None = None
    elapsed: float = 0.0
    # Login/desafio anti-bot: conta no circuit breaker de sessao.
    session_failure: bool = False
    # Erro de conexao na NAVEGACAO (pode ser o proxy fora do ar).
    exit_failure: bool = False


@dataclass
class _PageSlot:
    """Page em uso por um worker + a geracao da sessao em que foi criada.

    Quando o browser e relancado (crash ou troca de proxy) a geracao muda e a
    page antiga deixa de valer: o slot pede uma nova no context atual.
    """
    page: Page | None
    generation: int


class _DeferExport(Exception):
    """Devolve a URL para a fila quando outra saida pode processa-la agora."""


@dataclass
class ExportAccumulators:
    pages_dir: Path
    checkpoint: Checkpoint
    used_names: set[str] = field(default_factory=set)
    # indice na lista de URLs -> (path, title). Indexado para que o merge siga
    # a ordem da arvore mesmo com workers paralelos, retries e repescagens.
    exported: dict[int, tuple[Path, str]] = field(default_factory=dict)
    # url -> ultimo erro. Removida se a pagina for exportada numa nova tentativa.
    failures: dict[str, str] = field(default_factory=dict)
    # URLs cuja falha nao melhora repetindo agora (login, HTTP 4xx).
    permanent: set[str] = field(default_factory=set)
    # url -> nome de arquivo ja alocado (mesmo nome em todas as tentativas).
    names_by_url: dict[str, str] = field(default_factory=dict)
    slow_records: list[SlowPageRecord] = field(default_factory=list)
    # Lock para mutacoes em paralelo (used_names, exported, failures, slow_records,
    # consecutive_session_failures, abort_requested).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Lock dedicado para recovery de browser (impede que multiplos workers
    # relancem browser simultaneamente em caso de crash).
    browser_recovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Circuit breaker: conta failures consecutivos (login/CF) para abortar
    # quando a sessao/proteção fica em estado ruim e nao adianta seguir.
    consecutive_session_failures: int = 0
    abort_requested: bool = False
    # Requests reais ao servidor nas paginas exportadas (diagnostico de ritmo).
    requests_total: int = 0
    # Uma sessao por grupo de proxies, cada uma com limiter independente.
    # Sem proxies multiplos, os workers compartilham a sessao e o limiter.
    network_workers: list[tuple[BrowserSession, RateLimiter]] = field(default_factory=list)
    deferred_attempts: dict[str, int] = field(default_factory=dict)
    # Navegacoes podem usar mais proxies que nucleos. A impressao em PDF
    # usa CPU: limita so essa etapa, mantendo os downloads em paralelo.
    pdf_slots: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(max(1, os.cpu_count() or 4)),
    )

    def ordered_exports(self) -> list[tuple[Path, str]]:
        return [self.exported[index] for index in sorted(self.exported)]

    def failure_list(self) -> list[tuple[str, str]]:
        return list(self.failures.items())


# Apos N failures consecutivos do tipo login ou CF challenge, aborta o pipeline.
_SESSION_FAILURE_CIRCUIT_BREAKER = 5


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


async def _goto_dom_ready(page: Page, url: str, timeout_ms: int) -> None:
    """Navega ate o DOM pronto e levanta o erro certo para cada tipo de resposta.

    Sem retry automatico aqui: repetir um 429 dois segundos depois so acerta
    o servidor de novo DENTRO do ban. Quem decide repetir e o loop por URL,
    sempre passando pelo cooldown do RateLimiter.
    """
    get_page_stats(page).begin(url)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    block = await classify_page_block(page, response)
    if block is not None:
        if block.kind == RATE_LIMIT:
            raise RateLimitedError(block.describe(), retry_after=block.retry_after)
        if block.kind == SERVER_ERROR:
            raise TransientHTTPError(f"HTTP {block.status}", retry_after=block.retry_after)
        raise CloudflareChallengeError(block.describe(), retry_after=block.retry_after)
    if response is not None and response.status >= 400:
        raise PageHTTPError(f"HTTP {response.status}")


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

    try:
        await page.emulate_media(media="print")
    except PlaywrightError:
        pass

    elapsed_ms = (time.monotonic() - page_start) * 1000
    remaining_ms = int(timeout_ms - elapsed_ms)
    if remaining_ms > 2_000:
        try:
            await page.evaluate(
                """async ({selector, budget}) => {
                    const root = document.querySelector(selector) || document.body;
                    const images = Array.from(root.querySelectorAll('img'));
                    const ready = images.map(img => {
                        img.loading = 'eager';
                        if (img.complete) return Promise.resolve();
                        return new Promise(resolve => {
                            img.addEventListener('load', resolve, {once: true});
                            img.addEventListener('error', resolve, {once: true});
                        });
                    });
                    root.getBoundingClientRect();
                    if (document.fonts) ready.push(...Array.from(document.fonts)
                        .filter(font => font.status === 'loading')
                        .map(font => font.loaded.catch(() => {})));
                    let timer;
                    await Promise.race([
                        Promise.all(ready),
                        new Promise(resolve => {timer = setTimeout(resolve, budget);})
                    ]);
                    clearTimeout(timer);
                }""",
                {"selector": content_sel, "budget": min(remaining_ms, 20_000)},
            )
        except PlaywrightError:
            pass


async def _verify_page_assets(page: Page, logger, url: str, lenient: bool = False) -> None:
    """Confere se os recursos da pagina carregaram ANTES de imprimir o PDF.

    O HTML pode chegar com 200 e as imagens da mesma pagina ja serem
    respondidas com 429: o rate limit estoura no meio do carregamento. Sem
    esta checagem o PDF e gravado sem os prints de tela e registrado como
    sucesso, e nenhum resume o refaz.

      * HTTP 429 em qualquer recurso  -> RateLimitedError (cooldown + repete).
        Nunca e tolerado.
      * HTTP 5xx em imagem/CSS/fonte, ou queda de conexao no meio do download
        -> TransientNetworkError: pausa curta e repete, SEM acionar o cooldown
        de rate limit (nao e bloqueio do IP). Com lenient=True vira so aviso:
        pode ser uma imagem quebrada na propria documentacao, e perder a
        pagina inteira por causa dela seria pior.
    """
    stats = get_page_stats(page)
    rate_limited = [item for item in stats.blocked if item[1] == 429]
    if rate_limited:
        raise RateLimitedError(
            f"{len(rate_limited)} recurso(s) da pagina bloqueados com HTTP 429",
            retry_after=stats.retry_after(),
        )

    problems: list[str] = []
    server_errors = [item for item in stats.blocked if item[1] != 429]
    if server_errors:
        statuses = "/".join(sorted({str(status) for _u, status, _r in server_errors}))
        problems.append(f"{len(server_errors)} recurso(s) da pagina com HTTP {statuses}")
    # So quedas reais de rede contam; net::ERR_FAILED generico (CORS, bloqueio
    # do proprio browser) acontece em paginas perfeitamente carregadas.
    dropped = [
        item for item in stats.failed
        if is_transient_network_error(Exception(item[1]))
    ]
    if dropped:
        problems.append(
            f"{len(dropped)} imagem(ns)/estilo(s) falharam por erro de rede ({dropped[0][1]})"
        )
    if problems:
        message = "; ".join(problems)
        if not lenient:
            raise TransientNetworkError(message)
        logger.warning("%s em %s (PDF gerado mesmo assim).", message, url)

    try:
        broken = await page.evaluate(
            "() => Array.from(document.images).filter("
            "i => i.complete && i.naturalWidth === 0 && i.getAttribute('src')).length"
        )
    except PlaywrightError:
        broken = 0
    if broken:
        logger.warning(
            "%d imagem(ns) quebrada(s) na origem em %s (o PDF sai sem elas).", broken, url,
        )


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
            # Valida que tem pelo menos 1 pagina (evita PDF corrompido).
            # count_pdf_pages retorna -1 em erro, 0+ se valido.
            page_count = count_pdf_pages(pdf_path)
            if page_count < 0:
                last_error = f"PDF nao parseavel (size={size} bytes)"
                continue
            if page_count == 0:
                last_error = f"PDF sem paginas legiveis (size={size} bytes)"
                continue
            return size
        except OSError as exc:
            last_error = f"erro de IO ({exc})"
    raise RuntimeError(f"PDF gerado nao e valido: {last_error}")



async def _allocate_filename(
    index: int, title: str, acc: ExportAccumulators, url: str | None = None,
) -> str:
    """Aloca nome de arquivo unico de forma thread-safe (com lock).

    A mesma URL recebe sempre o mesmo nome dentro do run: uma nova tentativa
    sobrescreve o arquivo da anterior em vez de criar "...-2.pdf".
    """
    async with acc.lock:
        if url is not None and url in acc.names_by_url:
            return acc.names_by_url[url]
        # Pagina que ja tem PDF registrado (--regenerate): regrava o MESMO
        # arquivo. Um nome novo (o indice muda quando o mapa muda) deixaria o
        # PDF antigo orfao na pasta.
        existing = acc.checkpoint.get_exported_entry(url) if url is not None else None
        file_name = str((existing or {}).get("filename") or "")
        if not file_name or file_name in acc.used_names:
            file_name = build_pdf_file_name(index, title, acc.used_names)
        acc.used_names.add(file_name)
        if url is not None:
            acc.names_by_url[url] = file_name
    return file_name


async def _record_export_result(
    url: str, pdf_path: Path, title: str, file_name: str,
    size_bytes: int, elapsed_total: float, acc: ExportAccumulators, cfg: ExportConfig,
    logger, index: int = 0,
) -> None:
    """Registra um PDF exportado com sucesso (thread-safe + integridade).

    Calcula SHA-256 do PDF (data integrity) ANTES de pegar o lock — io de hash
    pode demorar em arquivos grandes, nao quer bloquear outros workers.
    """
    # Hash fora do lock E fora do event loop (sha256_file eh blocking IO).
    # Executar em thread executor evita travar workers paralelos durante
    # leitura de PDFs gigantes (10MB+ em HDD pode bloquear 100ms+).
    try:
        loop = asyncio.get_running_loop()
        pdf_hash = await loop.run_in_executor(None, sha256_file, pdf_path)
    except OSError as exc:
        logger.warning("Falha ao calcular SHA-256 de %s: %s", pdf_path.name, exc)
        pdf_hash = None

    async with acc.lock:
        acc.exported[index] = (pdf_path, title)
        acc.failures.pop(url, None)
        acc.permanent.discard(url)
        acc.checkpoint.record_export(
            url=url, filename=file_name, title=title,
            size_bytes=size_bytes, elapsed_seconds=elapsed_total,
            pdf_hash=pdf_hash,
        )
        if elapsed_total >= cfg.slow_threshold_seconds:
            acc.slow_records.append(
                SlowPageRecord(url=url, elapsed_seconds=elapsed_total, phase="pdf")
            )
            slow = True
        else:
            slow = False
    if slow:
        logger.warning("Pagina lenta na exportacao (%.1fs): %s", elapsed_total, url)


async def _record_export_failure(
    url: str, error: str, acc: ExportAccumulators, phase: str | None = None,
    elapsed: float = 0.0, permanent: bool = False,
) -> None:
    """Registra a falha FINAL de uma URL neste passe (thread-safe).

    Chamado so depois de esgotadas as tentativas da URL — um bloqueio que foi
    resolvido repetindo a pagina nao aparece em failures.
    """
    async with acc.lock:
        first_time = url not in acc.failures
        acc.failures[url] = error
        if permanent:
            acc.permanent.add(url)
        if phase:
            acc.slow_records.append(
                SlowPageRecord(url=url, elapsed_seconds=elapsed, phase=phase)
            )
    # Uma tentativa por run no manifest (repescagens nao inflam o contador).
    if first_time:
        acc.checkpoint.record_failure(url, error)


async def _check_disk_or_abort(acc: ExportAccumulators, logger) -> None:
    """Verifica espaco em disco; aborta export se abaixo do minimo (thread-safe)."""
    free = get_free_disk_bytes(acc.pages_dir)
    if 0 < free < _MIN_DISK_FREE_DURING_EXPORT:
        async with acc.lock:
            should_log = not acc.abort_requested
            acc.abort_requested = True
        if should_log:
            logger.error(
                "Disco quase cheio (%d MB livres < %d MB). Abortando export "
                "para nao corromper PDFs. Libere espaco e rode novamente.",
                free // (1024 * 1024),
                _MIN_DISK_FREE_DURING_EXPORT // (1024 * 1024),
            )


async def _bump_session_failures(acc: ExportAccumulators, logger) -> None:
    """Incrementa contador de falhas de sessao (login/CF) e aciona circuit breaker.

    Thread-safe via acc.lock — evita race em modo paralelo onde workers
    incrementam o mesmo contador simultaneamente.
    """
    async with acc.lock:
        acc.consecutive_session_failures += 1
        should_abort = (
            acc.consecutive_session_failures >= _SESSION_FAILURE_CIRCUIT_BREAKER
        )
        if should_abort and not acc.abort_requested:
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
        # Rename atomico apenas se valido — retry em Windows antivirus
        atomic_replace_with_retry(str(tmp_path), str(pdf_path))
        return size_bytes
    except BaseException:
        # Inclui KeyboardInterrupt — limpa tmp em qualquer interrupcao
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise



async def _fresh_page(page: Page | None, session: BrowserSession, cfg: ExportConfig) -> Page | None:
    """Troca a page (estado incerto apos erro). None se o browser morreu ou foi
    trocado: o slot cria outra na proxima tentativa."""
    await _safe_close_page(page)
    async with session.rotation_lock:
        try:
            return await _new_page(session.context, cfg.timeout_ms)
        except (PlaywrightError, AttributeError):
            return None


async def _export_one(
    page: Page,
    session: BrowserSession,
    url: str,
    index: int,
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    lenient_assets: bool = False,
) -> tuple[Page | None, ExportOutcome]:
    """UMA tentativa de exportar a URL. Retorna (page, resultado).

    Nao registra falha nem aciona cooldown: classifica o que aconteceu e
    devolve para _export_with_retries decidir (esperar e repetir, ou desistir).
    """
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

        file_name = await _allocate_filename(index, title, acc, url=url)
        pdf_path = acc.pages_dir / file_name
        async with acc.pdf_slots:
            size_bytes = await _generate_pdf(page, pdf_path)
        elapsed_total = elapsed()

        await _record_export_result(
            url, pdf_path, title, file_name, size_bytes, elapsed_total, acc, cfg, logger,
            index=index,
        )
        requests = get_page_stats(page).requests
        async with acc.lock:
            acc.consecutive_session_failures = 0  # sucesso reseta circuit breaker
            acc.requests_total += requests
        logger.info(
            "PDF gerado (%s/%s) em %.1fs (%.1f KB, %d requests): %s",
            index, total, elapsed_total, size_bytes / 1024, requests, file_name,
        )
        return page, ExportOutcome(_OK, elapsed=elapsed_total)

    except LoginPageError as exc:
        logger.error("Login detectado em %s: %s", url, exc)
        return page, ExportOutcome(
            _PERMANENT, "Pagina de login (Confluence privado)", elapsed=elapsed(),
            session_failure=True,
        )

    except RateLimitedError as exc:
        logger.warning("Rate limit em %s apos %.1fs: %s", url, elapsed(), exc)
        outcome = ExportOutcome(
            _BLOCKED, str(exc), retry_after=exc.retry_after, elapsed=elapsed(),
        )

    except CloudflareChallengeError as exc:
        logger.error("Bloqueio do Cloudflare em %s: %s", url, exc)
        outcome = ExportOutcome(
            _BLOCKED, f"Cloudflare: {exc}", retry_after=exc.retry_after,
            phase="pdf-cf-challenge", elapsed=elapsed(), session_failure=True,
        )

    except TransientHTTPError as exc:
        logger.warning("HTTP transitorio em %s apos %.1fs: %s", url, elapsed(), exc)
        outcome = ExportOutcome(
            _BLOCKED, str(exc), retry_after=exc.retry_after, elapsed=elapsed(),
        )

    except TransientNetworkError as exc:
        logger.warning("Erro de rede em %s apos %.1fs: %s", url, elapsed(), exc)
        outcome = ExportOutcome(_TRANSIENT, str(exc), elapsed=elapsed())

    except PageHTTPError as exc:
        logger.error("%s ao abrir %s", exc, url)
        return page, ExportOutcome(_PERMANENT, str(exc), elapsed=elapsed())

    except PlaywrightTimeoutError:
        msg = f"TIMEOUT apos {elapsed():.1f}s (limite {cfg.timeout_ms/1000:.0f}s)"
        logger.error("%s: %s", msg, url)
        outcome = ExportOutcome(_TIMEOUT, msg, phase="pdf-timeout", elapsed=elapsed())

    except PlaywrightError as exc:
        logger.error(
            "Erro Playwright em %s apos %.1fs: %s", url, elapsed(), short_error(exc),
        )
        transient = is_transient_network_error(exc) or is_blocking_error(exc)
        outcome = ExportOutcome(
            _TRANSIENT if transient else _FAILED, short_error(exc), elapsed=elapsed(),
            exit_failure=transient,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Erro ao gerar PDF de %s apos %.1fs", url, elapsed())
        outcome = ExportOutcome(_FAILED, str(exc) or type(exc).__name__, elapsed=elapsed())

    return await _fresh_page(page, session, cfg), outcome


async def _slot_page(
    slot: _PageSlot, session: BrowserSession, cfg: ExportConfig, adopt: bool = False,
) -> Page:
    """Devolve uma page utilizavel para o slot (recria se a sessao mudou).

    Segura session.rotation_lock: a troca de browser (rodizio de proxy ou
    recovery de crash) fecha o context antigo e so depois publica o novo;
    criar page nesse intervalo falharia com "context has been closed".

    adopt=True (modo sequencial): apos um relancamento usa a page que ja veio
    com a nova sessao em vez de abrir outra.
    """
    async with session.rotation_lock:
        stale = slot.generation != session.generation
        closed = slot.page is None
        if not closed and not stale:
            try:
                closed = slot.page.is_closed()
            except PlaywrightError:
                closed = True
        if stale or closed:
            if not stale:
                await _safe_close_page(slot.page)
            reusable = session.page if (adopt and stale) else None
            try:
                usable = reusable is not None and not reusable.is_closed()
            except PlaywrightError:
                usable = False
            slot.page = reusable if usable else await _new_page(session.context, cfg.timeout_ms)
            slot.generation = session.generation
        return slot.page


# Tentativas extras quando a page morre por causa de um relancamento do
# browser feito por OUTRO worker (nao e falha da URL).
_MAX_RELAUNCH_REPLAYS = 6


def _defer_to_free_worker(
    url: str, index: int, total: int, acc: ExportAccumulators,
    session: BrowserSession, limiter: RateLimiter, attempt: int,
    last_attempt: bool, logger,
) -> None:
    if not last_attempt and limiter.cooldown_remaining() > 0 and any(
        other is not session and other_limiter.cooldown_remaining() == 0
        and (other.pool is None or other.pool.has_live_exits)
        for other, other_limiter in acc.network_workers
    ):
        # Ban ou quarentena de proxy lento: outra saida pode terminar a URL.
        acc.deferred_attempts[url] = attempt
        logger.info("Pagina %s/%s devolvida a fila para outra saida de rede.", index, total)
        raise _DeferExport()


async def _export_with_retries(
    slot: _PageSlot,
    session: BrowserSession,
    url: str,
    index: int,
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    limiter: RateLimiter,
    max_attempts: int = _MAX_ATTEMPTS_PER_URL,
    adopt_session_page: bool = False,
) -> bool:
    """Exporta a URL repetindo-a apos bloqueios/erros transitorios. True se gerou o PDF.

    Bloqueio (429/1015/5xx/desafio): registra no limiter (que usa o Retry-After
    do servidor), troca de proxy se houver outro livre e repete a MESMA URL —
    limiter.wait() so libera depois do cooldown. Erro de rede: pausa curta e
    repete. Login/HTTP 4xx/timeout: nao repete agora.
    """
    outcome = ExportOutcome(_FAILED, "nao processada")
    attempt = acc.deferred_attempts.pop(url, 0)
    replays = 0
    while attempt < max_attempts:
        if acc.abort_requested:
            if attempt == 0:
                return False  # nem chegou a ser tentada: fica como pendente
            break
        attempt += 1
        await _ensure_browser_alive(session, session.playwright, cfg, logger, acc)
        await limiter.wait()
        try:
            page = await _slot_page(slot, session, cfg, adopt=adopt_session_page)
        except PlaywrightError as exc:
            # Browser caiu entre a checagem e a criacao da page: tenta de novo.
            outcome = ExportOutcome(_FAILED, short_error(exc))
            slot.page = None
            replays += 1
            if replays <= _MAX_RELAUNCH_REPLAYS:
                attempt -= 1
            continue
        started_at = time.monotonic()
        generation = session.generation
        last_attempt = attempt >= max_attempts
        slot.page, outcome = await _export_one(
            page, session, url, index, total, acc, cfg, logger,
            # A partir da 3a tentativa, problema em sub-recurso que NAO e rate
            # limit vira aviso (imagem quebrada na origem nao pode custar a pagina).
            lenient_assets=attempt >= 3 or last_attempt,
        )
        limiter.mark_done()

        if outcome.status == _OK:
            await limiter.report_success()
            if session.pool is not None:
                session.pool.mark_ok()
            return True

        if (
            outcome.status in (_FAILED, _TRANSIENT)
            and session.generation != generation
            and replays < _MAX_RELAUNCH_REPLAYS
        ):
            # O browser foi relancado por outro worker (troca de proxy) no meio
            # desta pagina: nao e falha da URL, repete sem gastar tentativa.
            replays += 1
            attempt -= 1
            continue

        if outcome.session_failure:
            await _bump_session_failures(acc, logger)

        if outcome.status == _BLOCKED:
            cooldown = await limiter.report_block(
                logger, outcome.error, retry_after=outcome.retry_after,
                started_at=started_at,
            )
            await rotate_exit_after_block(
                session, limiter, cooldown, logger, generation=generation,
            )
            _defer_to_free_worker(
                url, index, total, acc, session, limiter, attempt, last_attempt, logger,
            )
            if not last_attempt:
                logger.info(
                    "Pagina %s/%s sera repetida apos o bloqueio (tentativa %d/%d).",
                    index, total, attempt + 1, max_attempts,
                )
            continue

        if outcome.status in (_TRANSIENT, _FAILED):
            if outcome.exit_failure:
                await note_exit_failure(
                    session, logger, limiter=limiter, generation=generation,
                )
                _defer_to_free_worker(
                    url, index, total, acc, session, limiter, attempt, last_attempt, logger,
                )
            if last_attempt or (outcome.status == _FAILED and attempt >= 2):
                break
            pause = min(20.0, 4.0 * attempt)
            logger.info(
                "Pagina %s/%s sera repetida em %.0fs (tentativa %d/%d).",
                index, total, pause, attempt + 1, max_attempts,
            )
            await asyncio.sleep(pause)
            continue

        if outcome.status == _TIMEOUT:
            # Direto: pagina pesada ou servidor lento, desacelera e deixa para a
            # repescagem. Por proxy: a falha conta contra a saida e a pagina e
            # repetida (na 3a seguida o proxy sai do rodizio).
            via_proxy = await note_page_timeout(
                session, limiter, logger, "timeout no PDF",
                started_at=started_at, generation=generation,
            )
            if via_proxy and not last_attempt:
                _defer_to_free_worker(
                    url, index, total, acc, session, limiter, attempt, last_attempt, logger,
                )
                logger.info(
                    "Pagina %s/%s sera repetida (tentativa %d/%d).",
                    index, total, attempt + 1, max_attempts,
                )
                continue
        break

    await _record_export_failure(
        url, outcome.error, acc, phase=outcome.phase, elapsed=outcome.elapsed,
        permanent=outcome.status == _PERMANENT,
    )
    return False


def _expand_urls_with_retries(
    urls: list[str], checkpoint: Checkpoint, logger,
) -> list[str]:
    """Adiciona URLs com failures ao fim da lista de export para nova tentativa.

    URLs ja em `urls` (mapped_urls) sao priorizadas. URLs com failures que
    ainda tem tentativas disponiveis (< MAX_RETRY_ATTEMPTS) sao incluidas
    no fim para serem re-tentadas — independentemente de force_reexport,
    porque essas URLs nunca tiveram PDF gerado (so falhas).

    Dedup contra exported tambem: se uma URL falhou no passado mas depois
    foi exportada com sucesso, nao re-tenta (record_export ja limpa failures
    mas pode haver dessincronia em manifests editados).
    """
    pending = checkpoint.urls_pending_retry(MAX_RETRY_ATTEMPTS)
    if not pending:
        return urls
    seen_urls = set(urls)
    exported_urls = set(checkpoint.manifest.exported.keys())
    extra = [u for u in pending if u not in seen_urls and u not in exported_urls]
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
    pool: ProxyPool | None = None,
) -> tuple[list[tuple[Path, str]], list[tuple[str, str]], list[SlowPageRecord]]:
    """Exporta URLs para PDF. Retorna (exported_entries, failures, slow_records).

    exported_entries: list of (path, title) tuples NA ORDEM de `urls` (para o
    merger), mesmo com workers paralelos e paginas geradas em repescagem.
    Usa checkpoint para skip de URLs ja exportadas em runs anteriores.
    Paginas bloqueadas sao repetidas no proprio run; as que ainda falharem
    passam por repescagens antes de retornar.
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
        pool=pool,
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

    skip_count = 0
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
    active_limiter = limiter if limiter is not None else RateLimiter(cfg.rate_limit, logger)

    try:
        async with async_playwright() as playwright, AsyncExitStack() as sessions:
            pending_count = total - skip_count
            groups = (
                pool.partition(min(cfg.max_workers, pending_count))
                if pool is not None and len(pool) > 1 and cfg.max_workers > 1
                and pending_count > 1 else [pool]
            )
            async def open_worker(
                index: int, group: ProxyPool | None,
            ) -> tuple[BrowserSession, RateLimiter]:
                # Cookies de um IP nao devem ser carregados/salvos por outro.
                worker_state = None if len(groups) > 1 else state_path
                worker_limiter = active_limiter if index == 0 else RateLimiter(cfg.rate_limit, logger)
                if group is not None:
                    await worker_limiter.set_cooldown(max(
                        worker_limiter.cooldown_remaining() if index == 0 else 0.0,
                        group.remaining_block(),
                    ))
                session = await _launch_browser_session(
                    playwright, headless, timeout_ms, logger,
                    state_path=worker_state, proxy=proxy, pool=group, block_extras=True,
                )
                sessions.push_async_callback(_teardown_session, session, logger)
                return session, worker_limiter

            # Lancamentos independentes em paralelo; em falha/cancelamento,
            # aguarda as tarefas antes de fechar as sessoes ja registradas.
            launches = [asyncio.create_task(open_worker(index, group)) for index, group in enumerate(groups)]
            try:
                acc.network_workers = list(await asyncio.gather(*launches))
            except BaseException:
                for launch in launches:
                    launch.cancel()
                await asyncio.gather(*launches, return_exceptions=True)
                raise
            session = acc.network_workers[0][0]
            if len(groups) > 1:
                logger.info(
                    "Paralelismo por rede: %d workers com navegadores e esperas independentes; "
                    "%.1fs de intervalo por saida. Proxies extras ficam de reserva.",
                    len(groups), cfg.rate_limit.base_delay_seconds,
                )
            try:
                await _run_export(
                    session, urls, total, acc, cfg, logger, console, active_limiter,
                )
                await _safe_close_page(session.page)
            finally:
                for _session, worker_limiter in acc.network_workers[1:]:
                    active_limiter.total_blocks += worker_limiter.total_blocks
                    active_limiter.total_wait_seconds += worker_limiter.total_wait_seconds
    finally:
        # Grava o que ja foi exportado mesmo em Ctrl+C/erro: sem isto ate 19
        # PDFs prontos ficavam fora do manifest e eram baixados de novo.
        try:
            checkpoint.flush()
        except OSError as exc:
            logger.warning("Falha ao gravar manifest ao final do export: %s", exc)

    if acc.exported:
        logger.info(
            "Export: %d pagina(s) geradas neste run com %d requests ao servidor.",
            sum(1 for u in urls if u in acc.names_by_url and u not in acc.failures),
            acc.requests_total,
        )
    return acc.ordered_exports(), acc.failure_list(), acc.slow_records


def _new_progress(console) -> Progress:
    return Progress(
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


async def _run_export(
    session: BrowserSession,
    urls: list[str],
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    console,
    limiter: RateLimiter,
) -> None:
    """Passe principal + repescagens das paginas que falharam."""
    items = list(enumerate(urls, start=1))
    with _new_progress(console) as progress:
        task = progress.add_task("Gerando PDFs", total=total)
        await _run_pass(
            session, items, total, acc, cfg, logger, progress, task, limiter,
            max_attempts=_MAX_ATTEMPTS_PER_URL,
        )

        for round_number in range(1, _FINAL_RETRY_ROUNDS + 1):
            pending = [
                (index, url) for index, url in items
                if url in acc.failures and url not in acc.permanent
            ]
            if not pending or acc.abort_requested:
                break
            logger.info(
                "Repescagem %d/%d: %d pagina(s) que falharam serao tentadas de novo.",
                round_number, _FINAL_RETRY_ROUNDS, len(pending),
            )
            retry_task = progress.add_task(
                f"Repescagem {round_number}", total=len(pending),
            )
            await _run_pass(
                session, pending, total, acc, cfg, logger, progress, retry_task, limiter,
                max_attempts=_RETRY_ROUND_ATTEMPTS,
            )


async def _run_pass(
    session: BrowserSession,
    items: list[tuple[int, str]],
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    progress,
    task,
    limiter: RateLimiter,
    max_attempts: int,
) -> None:
    if len(acc.network_workers) > 1 or (cfg.max_workers > 1 and len(items) > 1):
        await _run_parallel(
            session, items, total, acc, cfg, logger, progress, task, limiter,
            max_attempts,
        )
    else:
        await _run_export_loop(
            session, items, total, acc, cfg, logger, progress, task, limiter,
            max_attempts,
        )


async def _run_export_loop(
    session: BrowserSession,
    items: list[tuple[int, str]],
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    progress,
    task,
    limiter: RateLimiter,
    max_attempts: int,
) -> None:
    slot = _PageSlot(page=session.page, generation=session.generation)
    processed = 0
    for index, url in items:
        if acc.abort_requested:
            logger.warning("Export interrompido por circuit breaker.")
            break
        # Check de disco periodico: aborta antes de gerar PDF que vai falhar
        if index % _DISK_CHECK_EVERY_N == 0:
            await _check_disk_or_abort(acc, logger)
        short_url = url if len(url) <= 60 else url[:57] + "..."

        if not cfg.force_reexport and acc.checkpoint.is_exported(url, acc.pages_dir):
            await _resume_existing(url, short_url, index, total, acc, progress, task)
            progress.advance(task)
            continue

        progress.update(task, description=f"PDF {index}/{total}: {short_url}")
        if processed and processed % _PAGE_ROTATION_INTERVAL == 0:
            logger.info("Rotacionando pagina apos %s URLs", processed)
            await _safe_close_page(slot.page)
            slot.page = None
        await _export_with_retries(
            slot, session, url, index, total, acc, cfg, logger, limiter,
            max_attempts=max_attempts, adopt_session_page=True,
        )
        session.page = slot.page
        processed += 1
        progress.advance(task)
    session.page = slot.page


async def _run_parallel(
    session: BrowserSession,
    items: list[tuple[int, str]],
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    progress,
    task,
    limiter: RateLimiter,
    max_attempts: int,
) -> None:
    """Fila comum, paginas em paralelo e isolamento quando ha varios proxies."""
    queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
    for item in items:
        queue.put_nowait(item)
    resources = acc.network_workers or [(session, limiter)]
    if len(resources) > 1:
        resources = sorted(
            [item for item in resources if item[0].pool is None or item[0].pool.has_live_exits],
            key=lambda item: item[1].cooldown_remaining(),
        )
        if not resources:
            raise AllProxiesDeadError("Todos os workers ficaram sem proxies utilizaveis.")
        resources = resources[:len(items)]
        workers_count = len(resources)
    else:
        workers_count = min(cfg.max_workers, len(items))
        resources = resources * workers_count
    progress.update(task, description=f"Gerando PDFs (workers={workers_count})")
    workers = [
        asyncio.create_task(
            _worker_loop(
                wid, worker_session, queue, total, acc, cfg, logger, worker_limiter, progress, task,
                max_attempts,
            )
        )
        for wid, (worker_session, worker_limiter) in enumerate(resources)
    ]
    # join inclui URLs devolvidas a fila. Ao terminar, cancela os workers
    # ociosos ou aguardando cooldown, sem esperar o ban deles expirar.
    joined = asyncio.create_task(queue.join())
    waiting = set(workers) | {joined}
    try:
        while joined in waiting:
            done, waiting = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()  # propaga erro fatal; finally encerra os demais
            if waiting == {joined} and not joined.done():
                raise RuntimeError("Nenhum worker disponivel para concluir a fila de PDFs.")
    finally:
        for worker in workers:
            worker.cancel()
        joined.cancel()
        await asyncio.gather(*workers, joined, return_exceptions=True)


async def _worker_loop(
    worker_id: int,
    session: BrowserSession,
    queue: asyncio.Queue,
    total: int,
    acc: ExportAccumulators,
    cfg: ExportConfig,
    logger,
    limiter: RateLimiter,
    progress,
    task,
    max_attempts: int,
) -> None:
    """Loop de um worker paralelo: pega URL da fila, exporta, repete."""
    slot = _PageSlot(page=None, generation=session.generation)
    processed = 0
    try:
        while True:
            if len(acc.network_workers) > 1 and limiter.cooldown_remaining() > 0:
                # Espera antes de reservar outra URL; os workers livres
                # continuam consumindo a fila durante o ban deste IP.
                try:
                    await limiter.wait()
                except BlockedTooLongError:
                    if not any(
                        other is not session and other_limiter.cooldown_remaining()
                        <= other_limiter.config.max_block_wait_seconds
                        and (other.pool is None or other.pool.has_live_exits)
                        for other, other_limiter in acc.network_workers
                    ):
                        raise
                    logger.warning("Worker %d com ban longo; os demais continuam a fila.", worker_id)
                    return
            item = await queue.get()  # propaga CancelledError naturalmente
            try:
                if item is None:
                    break
                if acc.abort_requested:
                    continue  # esvazia a fila sem processar
                index, url = item
                short_url = url if len(url) <= 50 else url[:47] + "..."
                if not cfg.force_reexport and acc.checkpoint.is_exported(url, acc.pages_dir):
                    await _resume_existing(url, short_url, index, total, acc, progress, task)
                else:
                    progress.update(
                        task, description=f"W{worker_id} {index}/{total}: {short_url}",
                    )
                    try:
                        await _export_with_retries(
                            slot, session, url, index, total, acc, cfg, logger, limiter,
                            max_attempts=max_attempts,
                        )
                    except _DeferExport:
                        queue.put_nowait(item)
                        continue
                    except AllProxiesDeadError:
                        if not any(
                            other is not session and other.pool is not None
                            and other.pool.has_live_exits
                            for other, _other_limiter in acc.network_workers
                        ):
                            raise
                        logger.warning("Worker %d sem proxies utilizaveis; redistribuindo a pagina.", worker_id)
                        queue.put_nowait(item)
                        return
                    processed += 1
                    # Rotacao periodica para liberar memoria
                    if processed % _PAGE_ROTATION_INTERVAL == 0:
                        logger.info(
                            "Worker %d: rotacionando page apos %d URLs",
                            worker_id, processed,
                        )
                        await _safe_close_page(slot.page)
                        slot.page = None
                progress.advance(task)
            finally:
                # Sempre marca task_done — sem isto, queue.join() trava forever
                queue.task_done()
    finally:
        if slot.generation == session.generation:
            await _safe_close_page(slot.page)


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
        acc.exported[index] = (acc.pages_dir / filename, title)
        if filename:
            acc.used_names.add(filename)
    progress.update(
        task, description=f"PDF {index}/{total}: [cyan]resume[/cyan] {short_url}",
    )


async def _ensure_browser_alive(
    session: BrowserSession, playwright, cfg, logger,
    acc: ExportAccumulators | None = None,
) -> None:
    """Verifica e relanca browser se morreu. Usa lock se acc fornecido (paralelo).

    Em modo single-worker, acc=None e funciona normal. Em paralelo, dois workers
    podem detectar crash simultaneamente; o lock garante que so um relanca.
    """
    if _is_browser_alive(session):
        return

    # Lock so quando ha acc (modo paralelo). Single-worker: sem contencao.
    if acc is not None:
        async with acc.browser_recovery_lock:
            # Re-check apos pegar o lock (outro worker pode ter relancado)
            if _is_browser_alive(session):
                return
            await _do_browser_recovery(session, playwright, cfg, logger)
    else:
        await _do_browser_recovery(session, playwright, cfg, logger)


async def _do_browser_recovery(
    session: BrowserSession, playwright, cfg, logger,
) -> None:
    """Relanca o browser. Serializado com a troca de proxy (rotation_lock):
    as duas operacoes substituem browser/context da mesma sessao."""
    async with session.rotation_lock:
        if _is_browser_alive(session):
            return  # outro worker ja relancou (ou trocou de proxy) enquanto esperavamos
        logger.warning("Browser desconectou. Relancando...")
        if session.playwright is None:
            session.playwright = playwright
        await relaunch_session(session, logger)


async def _maybe_rotate_page(
    session: BrowserSession, index: int, cfg, logger,
) -> None:
    if index <= 1 or (index - 1) % _PAGE_ROTATION_INTERVAL != 0:
        return
    logger.info("Rotacionando pagina apos %s URLs", index - 1)
    await _safe_close_page(session.page)
    session.page = await _new_page(session.context, cfg.timeout_ms)
