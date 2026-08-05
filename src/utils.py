from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse, urlunparse

import colorama
from rich.console import Console
from rich.logging import RichHandler

colorama.just_fix_windows_console()

KEEP_QUERY_PARAMS = {"pageId", "spaceKey", "title"}

MANIFEST_FILENAME = "manifest.json"
STORAGE_STATE_FILENAME = "browser_state.json"

# Maximo de tentativas por URL antes de desistir definitivamente. URLs em
# failures com attempts < este valor sao re-enqueuadas no proximo run.
MAX_RETRY_ATTEMPTS = 5

# Intervalo de rotacao de pagina Playwright (compartilhado entre crawler/exporter
# para evitar memory leak em runs longos)
PAGE_ROTATION_INTERVAL = 50

# Threshold para confirmar batch grande antes de exportar
LARGE_BATCH_THRESHOLD = 500

_INVALID_FILE_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_MAX_FILENAME_LEN = 80


def utc_now_iso() -> str:
    """Timestamp UTC em ISO 8601 com sufixo +00:00 explícito.

    Garante consistencia ao comparar timestamps entre maquinas em TZ diferentes.
    isoformat() padrao do Python pode omitir o sufixo em alguns casos; aqui
    forcamos explicitamente.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Python ja inclui +00:00 quando datetime tem tzinfo, mas garantimos:
    if not now.endswith(("+00:00", "Z")):
        now = now + "+00:00"
    return now

# Nomes de dispositivo reservados no Windows (em qualquer caixa, com ou sem extensao)
_WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})

# Tamanho minimo aceitavel para um PDF gerado. Aumentado de 1KB para 3KB
# para reduzir falsos positivos (paginas de erro/login renderizam ~1.5KB).
_MIN_VALID_PDF_SIZE = 3 * 1024

_console: Console | None = None


def get_console() -> Console:
    global _console
    if _console is None:
        _console = Console(highlight=False)
    return _console


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


@dataclass
class RateLimitConfig:
    """Configuracao de rate limit + backoff anti-bloqueio.

    base_delay_seconds: pausa minima entre requests (anti-rate-limit basico).
    backoff_initial_seconds: cooldown apos detectar bloqueio (5xx, 429).
    backoff_max_seconds: teto do cooldown apos bloqueios sucessivos.
    backoff_multiplier: cada bloqueio sucessivo multiplica o cooldown por isto.
    jitter_ratio: variacao aleatoria do delay base (0.0 a 1.0 = +/-10% a +/-100%).
    """
    base_delay_seconds: float = 2.0
    backoff_initial_seconds: float = 30.0
    backoff_max_seconds: float = 900.0  # 15min teto
    backoff_multiplier: float = 2.0
    jitter_ratio: float = 0.2


class RateLimiter:
    """Throttle adaptativo: delay base entre requests + cooldown crescente em bloqueios.

    Uso:
        await limiter.wait()           # antes de cada request
        limiter.report_success()       # apos request bem-sucedido
        await limiter.report_block(logger)  # apos 5xx/429/timeout consecutivo

    Thread-safe entre coroutines via asyncio.Lock. Nao protege contra threads,
    so contra concorrencia cooperativa de asyncio.
    """

    def __init__(self, config: RateLimitConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._last_request_at: float = 0.0
        self._consecutive_blocks: int = 0
        self._cooldown_until: float = 0.0

    async def wait(self) -> None:
        """Bloqueia ate ser seguro fazer o proximo request.

        IMPORTANTE: o lock so eh segurado para reservar o slot temporal
        (atualizar _last_request_at e calcular sleep). O asyncio.sleep
        em si acontece FORA do lock para permitir que outros workers
        avancem em paralelo respeitando suas proprias reservas.
        """
        async with self._lock:
            now = time.monotonic()
            cooldown_sleep = max(0.0, self._cooldown_until - now)
            # Calcula tempo desde o ultimo request reservado
            elapsed_since_reserved = now - self._last_request_at
            jitter = 1.0 + random.uniform(
                -self.config.jitter_ratio, self.config.jitter_ratio,
            )
            target_delay = max(0.0, self.config.base_delay_seconds * jitter)
            base_sleep = max(0.0, target_delay - elapsed_since_reserved)
            # Reserva o slot: marca o tempo em que este request VAI ocorrer
            total_sleep = cooldown_sleep + base_sleep
            self._last_request_at = now + total_sleep

        if total_sleep > 0:
            await asyncio.sleep(total_sleep)

    async def report_success(self) -> None:
        """Reseta o contador de bloqueios consecutivos (thread-safe)."""
        async with self._lock:
            self._consecutive_blocks = 0

    async def report_block(self, logger=None, reason: str = "") -> float:
        """Registra bloqueio e agenda cooldown exponencial. Retorna duracao do cooldown.

        Jitter aplica APENAS aumento (0 a +25%) — nunca reduz cooldown abaixo
        do calculado, evitando bater no servidor antes da hora.
        """
        async with self._lock:
            self._consecutive_blocks += 1
            cooldown = min(
                self.config.backoff_max_seconds,
                self.config.backoff_initial_seconds
                * (self.config.backoff_multiplier ** (self._consecutive_blocks - 1)),
            )
            # Jitter SOMENTE aumenta (1% a 25%) para anti-sincronizacao garantida.
            # `random.uniform(0, 0.25)` pode retornar 0 exato, causando workers
            # bater no servidor no mesmo instante.
            cooldown *= 1.0 + random.uniform(0.01, 0.25)
            self._cooldown_until = time.monotonic() + cooldown
            if logger is not None:
                logger.warning(
                    "Bloqueio detectado%s. Cooldown #%d: %.0fs",
                    f" ({reason})" if reason else "",
                    self._consecutive_blocks, cooldown,
                )
            return cooldown

    @property
    def consecutive_blocks(self) -> int:
        """Leitura snapshot (int atomico em CPython com GIL)."""
        return self._consecutive_blocks


_REALISTIC_USER_AGENTS = (
    # Chrome 120 Win10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 121 Win11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Edge 121
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    # Firefox 122
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
)

_REALISTIC_VIEWPORTS = (
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1600, "height": 900},
)


def pick_user_agent() -> str:
    """Retorna um User-Agent realista aleatorio (anti-fingerprinting)."""
    return random.choice(_REALISTIC_USER_AGENTS)


def pick_viewport() -> dict:
    """Retorna viewport realistico aleatorio."""
    return random.choice(_REALISTIC_VIEWPORTS)


def build_browser_context_args(
    state_path: Path | None = None,
    proxy_url: str | None = None,
) -> dict:
    """Argumentos comuns para new_context (UA, viewport, locale, storage_state, proxy)."""
    args: dict = {
        "user_agent": pick_user_agent(),
        "viewport": pick_viewport(),
        "locale": "pt-BR",
        "timezone_id": "America/Sao_Paulo",
        "extra_http_headers": {
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
    }
    if state_path and state_path.exists():
        try:
            with state_path.open("r", encoding="utf-8") as fp:
                json.load(fp)
            args["storage_state"] = str(state_path)
        except (OSError, ValueError):
            pass  # ignora storage_state corrompido
    proxy_dict = parse_proxy_arg(proxy_url)
    if proxy_dict:
        args["proxy"] = proxy_dict
    return args


def parse_proxy_arg(proxy: str | None) -> dict | None:
    """Converte string de proxy em dict do Playwright. Suporta env var fallback."""
    if not proxy:
        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if parsed.scheme not in {"http", "https", "socks5"}:
        return None
    result = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


_CF_CHALLENGE_INDICATORS = (
    "cf-browser-verification",
    "challenge-platform",
    "checking your browser",
    "verifying you are human",
    "ddos protection by cloudflare",
    "ray id:",
    "cf-error",
    "cf-chl-bypass",
)


def is_cloudflare_challenge(content_lower: str) -> bool:
    """Detecta paginas de challenge do Cloudflare via marcadores no HTML."""
    return any(k in content_lower for k in _CF_CHALLENGE_INDICATORS)


_BLOCKING_HTTP_CODES = ("522", "523", "524", "525", "502", "503", "504", "429")
_BLOCKING_TEXT_MARKERS = (
    "net::err_aborted",
    "net::err_connection_reset",
    "net::err_connection_closed",
    "net::err_timed_out",
    "err_http_response_code_failure",
)


def is_blocking_error(exc: BaseException) -> bool:
    """Detecta excecoes que sugerem bloqueio do servidor (Cloudflare 5xx, 429).

    Heuristica conservadora: tem que ter palavras-chave especificas na mensagem.

    So inspeciona a "headline" da excecao (antes de "\\nCall log:") — o
    Playwright anexa ali um eco dos headers do request original (inclusive
    Cookie), e numeros de sessao/timestamp (ex: cookies _ga_*) podem
    coincidentemente conter "503" etc, causando falso positivo. Codigos
    HTTP numericos usam lookaround pra nao casar digitos embutidos em IDs
    maiores (ex: "503" dentro de "1785936503").
    """
    headline = str(exc).split("\nCall log:", 1)[0].lower()
    if any(marker in headline for marker in _BLOCKING_TEXT_MARKERS):
        return True
    return any(
        re.search(rf"(?<!\d){code}(?!\d)", headline) for code in _BLOCKING_HTTP_CODES
    )


@dataclass(frozen=True)
class CrawlScope:
    domain: str
    path_prefix: str
    space_key: str | None


@dataclass(frozen=True)
class SlowPageRecord:
    url: str
    elapsed_seconds: float
    phase: str  # "crawl" | "crawl-timeout" | "pdf" | "pdf-timeout"


@dataclass
class Manifest:
    """Estado persistido entre execucoes para resume/checkpoint."""
    version: int = 2
    start_url: str = ""
    started_at: str = ""
    last_updated: str = ""
    crawl_complete: bool = False
    # Limite de paginas usado no crawl que produziu mapped_urls.
    # Permite detectar quando o usuario muda max_pages e force re-crawl.
    crawl_max_pages: int | None = None
    mapped_urls: list[str] = field(default_factory=list)
    # Fila de URLs ainda nao mapeadas (para resume parcial de crawl interrompido).
    # Quando crawl_complete=True isto eh sempre vazio.
    crawl_queue: list[str] = field(default_factory=list)
    # URLs ja vistas no crawl (independente de terem virado mapped_urls).
    # Evita reprocessar paginas que falharam por timeout no resume.
    crawl_seen: list[str] = field(default_factory=list)
    # url -> {filename, title, size_bytes, elapsed_seconds}
    exported: dict[str, dict] = field(default_factory=dict)
    # url -> {error, attempts, last_attempt}. URLs com failures voltam pra fila
    # no resume para nova tentativa (ate _MAX_RETRY_ATTEMPTS).
    failures: dict[str, dict] = field(default_factory=dict)
    # url -> {filename, title, size_bytes, elapsed_seconds}. Espelha `exported`
    # mas para a exportacao em Markdown (pipeline independente do PDF).
    exported_md: dict[str, dict] = field(default_factory=dict)


class Checkpoint:
    """Gerencia leitura/escrita do manifest.json (atomic + tolerante a corrupcao).

    Quando o arquivo existe mas esta corrompido ou e de versao incompativel,
    armazena o motivo em `load_warning` para o caller poder alertar o usuario.

    Thread-safe entre coroutines via threading.Lock no save() (acessado tambem
    de codigo sincrono, por isso threading e nao asyncio.Lock).
    """

    def __init__(self, output_dir: Path, start_url: str, logger=None):
        self.path = output_dir / MANIFEST_FILENAME
        self.load_warning: str | None = None
        self.previous_start_url: str | None = None
        self.manifest = self._load_or_create(start_url)
        self._save_lock = threading.Lock()
        # Batch de saves: evita O(N²) write em runs grandes.
        # `_pending_save_count` e `_save_every_n` protegidos pelo mesmo
        # _save_lock para evitar race em modo paralelo.
        self._pending_save_count = 0
        self._save_every_n = 20
        if self.load_warning and logger is not None:
            logger.warning("Manifest descartado: %s (recomecando do zero)", self.load_warning)

    def _maybe_batched_save(self) -> None:
        """Salva apenas a cada N chamadas (thread-safe).

        Decide DENTRO do lock se precisa salvar — evita race onde 2 workers
        leem count=19, ambos incrementam pra 20 e ambos chamam save().
        Se save() falhar, contador NAO eh zerado (proxima chamada tenta de novo).
        """
        with self._save_lock:
            self._pending_save_count += 1
            should_save = self._pending_save_count >= self._save_every_n
            if should_save:
                self._pending_save_count = 0
        if should_save:
            try:
                self.save()
            except OSError:
                # Save falhou — restaura contador pra tentar de novo proxima
                with self._save_lock:
                    self._pending_save_count = self._save_every_n
                raise

    def flush(self) -> None:
        """Forca save pendente (chamar em fim de fase).

        Se save() falhar, mantem contador para retry (nao zera estado).
        """
        self.save()  # save() ja faz lock interno; se falhar, propaga
        with self._save_lock:
            self._pending_save_count = 0

    def _load_or_create(self, start_url: str) -> Manifest:
        if not self.path.exists():
            return self._fresh(start_url)
        try:
            with self.path.open("r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError) as exc:
            # ValueError cobre JSONDecodeError (subclasse)
            self.load_warning = f"manifest.json invalido/corrompido ({exc})"
            return self._fresh(start_url)

        version = data.get("version")
        if version not in (1, 2):
            self.load_warning = (
                f"manifest.json com versao desconhecida ({version!r})"
            )
            return self._fresh(start_url)
        prev_url = str(data.get("start_url", ""))
        if prev_url and prev_url != start_url:
            # Mudou a URL inicial — preserva info para alertar o caller.
            self.previous_start_url = prev_url
            return self._fresh(start_url)

        crawl_complete = bool(data.get("crawl_complete", False))
        mapped_urls = list(data.get("mapped_urls", []))
        # Consistencia: crawl_complete=True com mapped_urls=[] e estado invalido
        if crawl_complete and not mapped_urls:
            self.load_warning = "manifest.json marcado como crawl_complete mas sem URLs"
            return self._fresh(start_url)

        # Migracao v1 -> v2: failures era dict[str, str], agora eh dict[str, dict].
        # Quando string, tenta extrair numero de tentativas da mensagem (ex:
        # "timeout apos 3 tentativas") para preservar historico. Sem isso,
        # URLs ja exauridas em v1 voltariam pra retry no v2.
        raw_failures = data.get("failures", {}) or {}
        failures: dict[str, dict] = {}
        for url, value in raw_failures.items():
            if isinstance(value, str):
                match = re.search(r"(\d+)\s*(?:tentativa|attempt)", value, re.I)
                attempts = int(match.group(1)) if match else 1
                failures[url] = {"error": value, "attempts": attempts, "last_attempt": ""}
            elif isinstance(value, dict):
                failures[url] = {
                    "error": str(value.get("error", "")),
                    "attempts": int(value.get("attempts", 1)),
                    "last_attempt": str(value.get("last_attempt", "")),
                }

        return Manifest(
            version=2,
            start_url=str(data.get("start_url", start_url)),
            started_at=str(data.get("started_at", "")),
            last_updated=str(data.get("last_updated", "")),
            crawl_complete=crawl_complete,
            crawl_max_pages=data.get("crawl_max_pages"),
            mapped_urls=mapped_urls,
            crawl_queue=list(data.get("crawl_queue", [])),
            crawl_seen=list(data.get("crawl_seen", [])),
            exported=dict(data.get("exported", {})),
            failures=failures,
            exported_md=dict(data.get("exported_md", {})),
        )

    @staticmethod
    def _fresh(start_url: str) -> Manifest:
        return Manifest(
            start_url=start_url,
            started_at=utc_now_iso(),
        )

    def save(self) -> None:
        """Salva manifest atomicamente (tmp + replace). Tolerante a falhas de IO.

        Usa lock para evitar corrupcao quando multiplos workers chamam
        save() em paralelo. Se a escrita falhar, preserva o timestamp
        anterior para evitar estado inconsistente onde last_updated
        avanca mas o conteudo ficou parcial.
        """
        with self._save_lock:
            prev_timestamp = self.manifest.last_updated
            self.manifest.last_updated = utc_now_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            try:
                with tmp.open("w", encoding="utf-8") as fp:
                    json.dump(asdict(self.manifest), fp, indent=2, ensure_ascii=False)
                    fp.flush()
                    try:
                        os.fsync(fp.fileno())
                    except OSError:
                        pass  # nao critico, alguns FS nao suportam
                # Retry em Windows: antivirus pode segurar manifest temporariamente
                atomic_replace_with_retry(str(tmp), str(self.path))
            except OSError:
                # Restaura timestamp anterior em caso de falha (consistencia)
                self.manifest.last_updated = prev_timestamp
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                raise

    def backup(self, max_backups: int = 5) -> Path | None:
        """Cria backup do manifest atual em manifest.json.bak.N (rotativo).

        Mantem os ultimos `max_backups`. Retorna o path do backup criado,
        ou None se nao havia manifest para fazer backup. Chamado antes de
        operacoes destrutivas (--fresh, reset).
        """
        if not self.path.exists():
            return None
        with self._save_lock:
            # Rotaciona backups: .bak.1 -> .bak.2, ..., descarta o mais antigo
            for i in range(max_backups - 1, 0, -1):
                src = self.path.with_suffix(f".json.bak.{i}")
                dst = self.path.with_suffix(f".json.bak.{i + 1}")
                if src.exists():
                    try:
                        if dst.exists():
                            dst.unlink()
                        os.replace(str(src), str(dst))
                    except OSError:
                        pass
            target = self.path.with_suffix(".json.bak.1")
            try:
                import shutil
                shutil.copy2(str(self.path), str(target))
                return target
            except OSError:
                return None

    def record_crawl_complete(self, urls: list[str], max_pages: int | None = None) -> None:
        """Marca crawl como completo. Trim crawl_seen (info redundante apos completo).

        Quando crawl termina, `crawl_seen` eh redundante (== mapped_urls).
        Limpa pra evitar manifest.json gigante (10k+ URLs * 2 = 20k entries).
        """
        self.manifest.crawl_complete = True
        self.manifest.mapped_urls = list(urls)
        self.manifest.crawl_queue = []  # fila esvaziada — crawl terminou
        self.manifest.crawl_seen = []   # redundante apos completo
        self.manifest.crawl_max_pages = max_pages
        self.save()

    def save_crawl_progress(
        self,
        ordered_urls: list[str],
        queue: list[str],
        seen: list[str],
        max_pages: int | None = None,
    ) -> None:
        """Salva snapshot incremental do crawl (para resume parcial).

        Chamado periodicamente durante o crawl, NAO marca crawl_complete.
        """
        self.manifest.mapped_urls = list(ordered_urls)
        self.manifest.crawl_queue = list(queue)
        self.manifest.crawl_seen = list(seen)
        self.manifest.crawl_max_pages = max_pages
        self.save()

    def is_exported(self, url: str, pages_dir: Path) -> bool:
        """True se URL tem PDF valido no disco. Valida header + EOF + tamanho minimo."""
        entry = self.manifest.exported.get(url)
        if not entry:
            return False
        filename = entry.get("filename", "")
        if not filename:
            return False
        path = pages_dir / filename
        return is_valid_pdf(path)

    def is_exported_md(self, url: str, md_dir: Path) -> bool:
        """True se URL ja tem arquivo Markdown no disco (checkpoint da pipeline MD)."""
        entry = self.manifest.exported_md.get(url)
        if not entry:
            return False
        filename = entry.get("filename", "")
        if not filename:
            return False
        path = md_dir / filename
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def record_export_md(
        self, url: str, filename: str, title: str,
        size_bytes: int, elapsed_seconds: float,
    ) -> None:
        """Registra export Markdown bem-sucedido (namespace separado de `exported`)."""
        self.manifest.exported_md[url] = {
            "filename": filename,
            "title": title,
            "size_bytes": size_bytes,
            "elapsed_seconds": elapsed_seconds,
        }
        self._maybe_batched_save()

    def reconcile_md_with_disk(self, md_dir: Path, logger=None) -> int:
        """Remove do manifest entradas MD cujo arquivo nao existe mais. Retorna removidos."""
        if not md_dir.exists() or not md_dir.is_dir():
            return 0
        removed = 0
        for url in list(self.manifest.exported_md.keys()):
            entry = self.manifest.exported_md[url]
            filename = entry.get("filename", "")
            path = md_dir / filename if filename else None
            if not filename or not path.is_file() or path.stat().st_size == 0:
                del self.manifest.exported_md[url]
                removed += 1
                if logger is not None:
                    logger.warning(
                        "Manifest MD dessincronizado: %s sem arquivo valido. "
                        "Sera regenerado.", filename or url,
                    )
        if removed > 0:
            self.save()
        return removed

    def check_invariants(self, logger=None) -> list[str]:
        """Verifica invariantes do manifest e retorna lista de inconsistencias.

        Detecta:
          - URL em exported E failures simultaneamente (qual venceu?)
          - mapped_urls com duplicatas
          - crawl_max_pages negativo
          - crawl_complete=True com mapped_urls vazio
          - URLs vazias/None em qualquer lista
        """
        issues: list[str] = []
        m = self.manifest

        # URLs em ambos exported e failures
        both = set(m.exported.keys()) & set(m.failures.keys())
        if both:
            issues.append(
                f"{len(both)} URL(s) em exported E failures simultaneamente "
                f"(exemplo: {next(iter(both))[:80]})"
            )

        # mapped_urls duplicadas
        if len(m.mapped_urls) != len(set(m.mapped_urls)):
            dup_count = len(m.mapped_urls) - len(set(m.mapped_urls))
            issues.append(f"mapped_urls tem {dup_count} duplicata(s)")

        # crawl_max_pages negativo
        if m.crawl_max_pages is not None and m.crawl_max_pages < 0:
            issues.append(f"crawl_max_pages negativo: {m.crawl_max_pages}")

        # crawl_complete inconsistente
        if m.crawl_complete and not m.mapped_urls:
            issues.append("crawl_complete=True mas mapped_urls esta vazio")

        # URLs vazias
        if any(not u or not isinstance(u, str) for u in m.mapped_urls):
            issues.append("mapped_urls contem entries vazias ou nao-string")

        if issues and logger is not None:
            for issue in issues:
                logger.warning("Manifest invariant: %s", issue)
        return issues

    def reconcile_with_disk(self, pages_dir: Path, logger=None) -> int:
        """Remove do manifest entradas cujo PDF nao existe mais ou eh invalido.

        Retorna numero de entradas removidas. Chamada no inicio do resume
        para sincronizar manifest com estado real do disco (PDFs deletados
        manualmente, antivirus quarantine, etc). Se pages_dir nao for
        diretorio, retorna 0 (nao corrompe o manifest).
        """
        if not pages_dir.exists():
            if logger is not None:
                logger.debug("pages_dir nao existe ainda, pulando reconcile")
            return 0
        if not pages_dir.is_dir():
            if logger is not None:
                logger.warning(
                    "reconcile_with_disk: %s nao eh diretorio, pulando",
                    pages_dir,
                )
            return 0
        removed = 0
        # list() necessario: deletamos do dict dentro do loop (mutacao concorrente)
        for url in list(self.manifest.exported.keys()):  # noqa: PLR0904
            entry = self.manifest.exported[url]
            filename = entry.get("filename", "")
            if not filename:
                del self.manifest.exported[url]
                removed += 1
                continue
            path = pages_dir / filename
            if not is_valid_pdf(path):
                del self.manifest.exported[url]
                removed += 1
                if logger is not None:
                    logger.warning(
                        "Manifest dessincronizado: %s sem PDF valido. "
                        "Sera regenerado.", filename,
                    )
        if removed > 0:
            self.save()
        return removed

    # Nomes protegidos: nunca devem ser deletados por find_orphan_pdfs.
    # Cobertura: manifest.json e backups, mesmo que algum tenha extensao .pdf.
    _PROTECTED_NAMES = frozenset({
        MANIFEST_FILENAME,
        "browser_state.json",
        "run.log",
        "slow_pages.log",
    })

    def find_orphan_pdfs(self, pages_dir: Path) -> list[Path]:
        """PDFs no disco sem entrada no manifest (orfaos).

        Protege contra falsa deteccao de nomes especiais (manifest.json.pdf,
        backup files) e ignora arquivos .tmp em geracao.
        """
        if not pages_dir.exists() or not pages_dir.is_dir():
            return []
        manifested = {entry.get("filename", "") for entry in self.manifest.exported.values()}
        manifested.discard("")
        orphans: list[Path] = []
        for path in pages_dir.glob("*.pdf"):
            # Skip arquivos protegidos por nome ou em geracao (.tmp)
            if path.name in self._PROTECTED_NAMES:
                continue
            if path.name.endswith(".tmp") or ".pdf.tmp" in path.name:
                continue
            if path.name not in manifested:
                orphans.append(path)
        return orphans

    def get_exported_entry(self, url: str) -> dict | None:
        return self.manifest.exported.get(url)

    def record_export(
        self,
        url: str,
        filename: str,
        title: str,
        size_bytes: int,
        elapsed_seconds: float,
        pdf_hash: str | None = None,
    ) -> None:
        """Registra export bem-sucedido.

        Se `pdf_hash` fornecido (SHA-256), permite validar integridade depois
        (detecta corrupcao pos-write por antivirus/disco).
        """
        entry = {
            "filename": filename,
            "title": title,
            "size_bytes": size_bytes,
            "elapsed_seconds": elapsed_seconds,
        }
        if pdf_hash:
            entry["sha256"] = pdf_hash
        self.manifest.exported[url] = entry
        # Remove de failures se estava la (re-tentativa bem-sucedida)
        self.manifest.failures.pop(url, None)
        # Batched save: evita O(N²) em runs com muitos PDFs
        self._maybe_batched_save()

    def record_failure(self, url: str, error: str) -> None:
        existing = self.manifest.failures.get(url) or {}
        attempts = int(existing.get("attempts", 0)) + 1
        self.manifest.failures[url] = {
            "error": error,
            "attempts": attempts,
            "last_attempt": utc_now_iso(),
        }
        self._maybe_batched_save()

    def failure_attempts(self, url: str) -> int:
        entry = self.manifest.failures.get(url)
        if not entry:
            return 0
        return int(entry.get("attempts", 0))

    def urls_pending_retry(self, max_attempts: int) -> list[str]:
        """URLs com failures mas que ainda merecem nova tentativa."""
        return [
            url for url, entry in self.manifest.failures.items()
            if int(entry.get("attempts", 0)) < max_attempts
        ]


@dataclass(frozen=True)
class PendingJob:
    """Job incompleto detectado em output/<subdir>/manifest.json."""
    output_dir: Path
    manifest_path: Path
    start_url: str
    mapped_count: int
    queue_count: int
    exported_count: int
    failure_count: int
    last_updated: str
    crawl_complete: bool

    @property
    def display_name(self) -> str:
        return self.output_dir.name

    @property
    def is_crawl_pending(self) -> bool:
        return not self.crawl_complete

    @property
    def is_export_pending(self) -> bool:
        return self.exported_count < self.mapped_count or self.failure_count > 0


def _collect_manifest_candidates(root_dir: Path, max_depth: int = 3) -> list[Path]:
    """Lista paths candidatos a manifest (root + subpastas ate `max_depth` niveis).

    Default profundidade 3 cobre `output/Area/Projeto/manifest.json`.
    """
    candidates: list[Path] = [root_dir / MANIFEST_FILENAME]
    try:
        if root_dir.exists():
            for depth in range(1, max_depth + 1):
                pattern = "/".join(["*"] * depth) + "/" + MANIFEST_FILENAME
                candidates.extend(root_dir.glob(pattern))
    except OSError:
        pass
    # Dedup preservando ordem
    seen: set[Path] = set()
    unique: list[Path] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _read_manifest_for_job(manifest_path: Path) -> PendingJob | None:
    """Le um manifest e retorna PendingJob se houver trabalho pendente."""
    if not manifest_path.exists():
        return None
    try:
        with manifest_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    crawl_complete = bool(data.get("crawl_complete", False))
    mapped = list(data.get("mapped_urls", []))
    exported = dict(data.get("exported", {}))
    failures = dict(data.get("failures", {}))
    is_pending = (
        not crawl_complete
        or len(exported) < len(mapped)
        or len(failures) > 0
    )
    if not is_pending:
        return None

    return PendingJob(
        output_dir=manifest_path.parent,
        manifest_path=manifest_path,
        start_url=str(data.get("start_url", "")),
        mapped_count=len(mapped),
        queue_count=len(list(data.get("crawl_queue", []))),
        exported_count=len(exported),
        failure_count=len(failures),
        last_updated=str(data.get("last_updated", "")),
        crawl_complete=crawl_complete,
    )


def find_pending_jobs(root_dir: Path) -> list[PendingJob]:
    """Encontra subpastas com manifest.json indicando trabalho incompleto.

    Procura ate 2 niveis de profundidade em root_dir e na pasta atual.
    Retorna ordenado pela data de last_updated (mais recente primeiro).
    """
    jobs: list[PendingJob] = []
    for manifest_path in _collect_manifest_candidates(root_dir):
        job = _read_manifest_for_job(manifest_path)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda j: j.last_updated, reverse=True)
    return jobs


class InvalidStartUrlError(ValueError):
    """URL inicial invalida (scheme nao-http, sem dominio, etc)."""


# Hosts privados/reservados bloqueados (defesa contra SSRF e auto-conexao).
_SSRF_BLOCKED_HOSTS = frozenset({
    "localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]",
    # AWS metadata endpoint
    "169.254.169.254",
    # Outros metadata endpoints comuns
    "metadata.google.internal", "metadata.azure.com",
})


def _normalize_ipv4_alt_repr(hostname: str) -> str:
    """Normaliza representacoes alternativas de IPv4 (hex/decimal/octal).

    Examples:
        '0x7f000001' -> '127.0.0.1'
        '2130706433' -> '127.0.0.1'
        '017700000001' -> '127.0.0.1'  (octal)

    Retorna hostname original se nao for representacao alternativa.
    """
    import ipaddress
    try:
        # Tenta como int — ORDEM importa:
        # 1. hex (0x...) primeiro (distintivo)
        # 2. octal (0... com soh 0-7) ANTES de decimal (octal tambem eh isdigit())
        # 3. decimal puro
        if hostname.startswith(("0x", "0X")):
            value = int(hostname, 16)
        elif (
            hostname.startswith("0") and len(hostname) > 1
            and all(c in "01234567" for c in hostname)
        ):
            value = int(hostname, 8)
        elif hostname.isdigit():
            value = int(hostname)
        else:
            return hostname
        # Converte int 32-bit para dotted-quad
        if 0 <= value <= 0xFFFFFFFF:
            return str(ipaddress.IPv4Address(value))
    except ValueError:
        pass
    return hostname


def _is_private_or_loopback_ip(hostname: str) -> bool:
    """True se hostname eh IP privado, loopback ou link-local.

    Tenta normalizar representacoes alternativas de IPv4 (hex, decimal, octal)
    para detectar bypasses tipo `0x7f000001`, `2130706433`.
    Remove zona ID de IPv6 (`fe80::1%eth0` -> `fe80::1`) antes de checar.
    """
    import ipaddress
    # Remove zona ID IPv6 (`%eth0` etc)
    if "%" in hostname:
        hostname = hostname.split("%", 1)[0]
    # Normaliza IPv4 alternativo
    hostname = _normalize_ipv4_alt_repr(hostname)
    try:
        ip = ipaddress.ip_address(hostname)
        return (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved
        )
    except (ValueError, ImportError):
        return False


def validate_start_url(url: str, allow_local: bool = False) -> str:
    """Valida e canonicaliza URL inicial. Bloqueia SSRF se allow_local=False.

    Rejeita:
      - Scheme nao-http(s) (file://, javascript:, data:, etc)
      - URL sem dominio
      - Hosts locais/privados (localhost, 127.x, 10.x, etc) se allow_local=False
      - IPs de metadata cloud (169.254.169.254)
    """
    if not url or not isinstance(url, str):
        raise InvalidStartUrlError("URL vazia ou nao e string.")

    stripped = url.strip()
    if not stripped:
        raise InvalidStartUrlError("URL vazia apos trim.")

    parsed = urlparse(stripped)
    if parsed.scheme not in {"http", "https"}:
        raise InvalidStartUrlError(
            f"URL deve comecar com http:// ou https:// (recebido: {parsed.scheme!r})."
        )
    if not parsed.netloc:
        raise InvalidStartUrlError(f"URL sem dominio: {stripped!r}")

    if not allow_local:
        hostname = (parsed.hostname or "").lower()
        if hostname in _SSRF_BLOCKED_HOSTS:
            raise InvalidStartUrlError(
                f"URL bloqueada (host privado/metadata): {hostname}. "
                "Use allow_local=True se intencional."
            )
        if _is_private_or_loopback_ip(hostname):
            raise InvalidStartUrlError(
                f"URL bloqueada (IP privado/loopback): {hostname}. "
                "Use allow_local=True se intencional."
            )

    return canonicalize_url(stripped)


# Caminho Windows MAX_PATH legacy (260 chars). Margem de seguranca: 240.
_MAX_PATH_SAFE = 240


def validate_safe_path(path: Path, must_be_relative_to: Path | None = None) -> Path:
    """Valida path contra traversal e MAX_PATH. Retorna path resolvido.

    Levanta ValueError se:
      - Resolved path excede MAX_PATH legacy do Windows (260 chars)
      - must_be_relative_to fornecido e resolved path nao esta dentro dele
    """
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Path invalido: {path} ({exc})") from exc

    resolved_str = str(resolved)
    if os.name == "nt" and len(resolved_str) > _MAX_PATH_SAFE:
        raise ValueError(
            f"Path muito longo para Windows ({len(resolved_str)} chars > "
            f"{_MAX_PATH_SAFE}): {resolved_str[:80]}..."
        )

    if must_be_relative_to is not None:
        try:
            base = must_be_relative_to.resolve(strict=False)
            resolved.relative_to(base)
        except (ValueError, OSError) as exc:
            raise ValueError(
                f"Path traversal detectado: {resolved} fora de {base}"
            ) from exc

    return resolved


def get_free_disk_bytes(path: Path) -> int:
    """Retorna bytes livres no filesystem que contem `path`. -1 se erro."""
    try:
        import shutil as _shutil
        target = path if path.exists() else path.parent
        usage = _shutil.disk_usage(str(target))
        return usage.free
    except OSError:
        return -1


def atomic_replace_with_retry(src: str, dst: str, max_retries: int = 5) -> None:
    """os.replace com retry para contornar antivirus lock em Windows.

    Antivirus pode segurar handle do arquivo recem-escrito por alguns segundos.
    Tenta ate `max_retries` com backoff curto (0.2s, 0.4s, ...).
    """
    import time as _time
    last_error: OSError | None = None
    for attempt in range(max_retries):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last_error = exc
            if attempt < max_retries - 1:
                _time.sleep(0.2 * (attempt + 1))
    if last_error is not None:
        raise last_error


def sanitize_proxy_for_log(proxy: str | None) -> str:
    """Retorna proxy string sem credenciais para logging seguro.

    Mascara username/password sempre. Hostname/porta sao preservados
    APENAS se hostname for publico (nao IP privado/loopback). Caso
    contrario, mascara hostname tambem (evita vazar infra interna).
    """
    if not proxy:
        return ""
    try:
        parsed = urlparse(proxy)
        hostname = parsed.hostname or ""
        port = parsed.port
        scheme = parsed.scheme or "http"
        has_creds = bool(parsed.username or parsed.password)

        # Detecta infra interna (IPs privados, hostnames .internal, .local etc)
        is_internal = _is_private_or_loopback_ip(hostname) or any(
            hostname.endswith(suffix) for suffix in (".internal", ".local", ".lan")
        )

        if is_internal:
            host_repr = "[REDACTED]"
        else:
            host_repr = hostname
            if port:
                host_repr = f"{host_repr}:{port}"

        if has_creds:
            return f"{scheme}://***:***@{host_repr}"
        return f"{scheme}://{host_repr}"
    except (ValueError, AttributeError):
        return "[invalid proxy URL]"


def ensure_output_dirs(output_dir: Path) -> tuple[Path, Path]:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "run.log"
    return pages_dir, log_file


def storage_state_path(output_dir: Path) -> Path:
    """Caminho do storage_state (cookies + localStorage) reutilizavel entre runs."""
    return output_dir / STORAGE_STATE_FILENAME


_LOCK_FILENAME = ".extrator.lock"


class JobLockError(RuntimeError):
    """Outro processo ja esta usando este output_dir."""


class JobLock:
    """Lock file para impedir 2 processos simultaneos no mesmo output_dir.

    Uso:
        with JobLock(output_dir):
            ...  # operacoes seguras

    Armazena PID. Se outro processo existe e tem o lock, mas PID nao esta
    ativo (crash), assume lock orfao e toma posse.
    """

    def __init__(self, output_dir: Path):
        self.lock_file = output_dir / _LOCK_FILENAME
        self._held = False

    def acquire(self) -> None:
        """Adquire o lock. Levanta JobLockError se ja em uso por processo vivo."""
        if self.lock_file.exists():
            try:
                pid_str = self.lock_file.read_text(encoding="utf-8").strip()
                pid = int(pid_str) if pid_str.isdigit() else 0
            except (OSError, ValueError):
                pid = 0
            if pid and _is_pid_alive(pid):
                raise JobLockError(
                    f"Outro processo (PID {pid}) ja esta usando {self.lock_file.parent}. "
                    "Aguarde ou finalize-o antes de rodar novamente."
                )
            # Lock orfao (processo morto) — toma posse
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            self.lock_file.write_text(str(os.getpid()), encoding="utf-8")
            self._held = True
        except OSError as exc:
            raise JobLockError(f"Falha ao adquirir lock: {exc}") from exc

    def release(self) -> None:
        """Libera o lock se este processo o detem."""
        if not self._held:
            return
        try:
            self.lock_file.unlink()
        except OSError:
            pass
        self._held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


def _is_pid_alive(pid: int) -> bool:
    """Verifica se um PID esta ativo (Windows + Unix)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows: usar tasklist (mais portavel que ctypes)
        try:
            import subprocess
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5,
            )
            return str(pid) in result.stdout
        except (OSError, subprocess.TimeoutExpired, ImportError):
            return False
    try:
        os.kill(pid, 0)  # signal 0 = check sem matar (ProcessLookupError eh subclasse de OSError)
        return True
    except OSError:
        return False


def setup_logger(
    log_file: Path, debug: bool = False, correlation_id: str | None = None,
) -> logging.Logger:
    """Configura logger com rotacao automatica (10MB x 5 backups).

    Args:
        log_file: arquivo de log (rotativo).
        debug: se True, ativa nivel DEBUG (mais verboso).
        correlation_id: ID unico do run (UUID curto) — incluido em cada linha
            para facilitar rastreamento de logs em multiplos runs.
    """
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("tdn_extractor")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    # Formato com correlation_id se fornecido
    if correlation_id:
        fmt_file = f"%(asctime)s | [{correlation_id}] | %(levelname)s | %(message)s"
        fmt_console = f"[{correlation_id}] %(message)s"
    else:
        fmt_file = "%(asctime)s | %(levelname)s | %(message)s"
        fmt_console = "%(message)s"

    file_handler = RotatingFileHandler(
        log_file, encoding="utf-8",
        maxBytes=10 * 1024 * 1024, backupCount=5,
    )
    file_handler.setFormatter(logging.Formatter(fmt_file))
    logger.addHandler(file_handler)

    rich_handler = RichHandler(
        console=get_console(),
        show_path=False,
        show_time=True,
        rich_tracebacks=True,
        markup=False,
    )
    rich_handler.setFormatter(logging.Formatter(fmt_console))
    logger.addHandler(rich_handler)

    return logger


def generate_correlation_id() -> str:
    """Gera ID unico curto para um run (8 chars hex)."""
    import uuid
    return uuid.uuid4().hex[:8]


def write_slow_pages_log(output_dir: Path, records: Iterable[SlowPageRecord]) -> Path:
    log_path = output_dir / "slow_pages.log"
    sorted_records = sorted(records, key=lambda r: r.elapsed_seconds, reverse=True)
    with log_path.open("w", encoding="utf-8") as fp:
        fp.write("# Paginas lentas detectadas durante a execucao\n")
        fp.write("# Formato: <tempo(s)> | <fase> | <url>\n")
        fp.write("# Ordenado do mais lento para o mais rapido.\n\n")
        for r in sorted_records:
            fp.write(f"{r.elapsed_seconds:>7.1f}s | {r.phase:<14} | {r.url}\n")
    return log_path


def format_bytes(n: int | float) -> str:
    """123456 -> '120.6 KB'."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def format_duration(seconds: float) -> str:
    """82.5 -> '1min 22s'. 7250.0 -> '2h 0min 50s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}min {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}min {secs}s"


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = re.sub(r"/+", "/", parsed.path or "/")

    query = parse_qs(parsed.query, keep_blank_values=True)
    filtered_query_items: list[tuple[str, str]] = []
    for key in sorted(query.keys()):
        if key in KEEP_QUERY_PARAMS:
            for value in sorted(query[key]):
                filtered_query_items.append((key, value))

    query_str = "&".join(f"{key}={value}" for key, value in filtered_query_items)
    normalized = parsed._replace(fragment="", query=query_str, path=path)
    return urlunparse(normalized)


_ZERO_WIDTH_CHARS = re.compile(r"[​-‍﻿]")
_RTL_MARKERS = re.compile(r"[‪-‮؜]")

_SLUGIFY_SUBSTITUTIONS = (
    ("c++", "c-plus-plus"),
    ("C++", "c-plus-plus"),
    ("c#", "c-sharp"),
    ("C#", "c-sharp"),
    ("&", "-and-"),
    ("™", "-tm"),
    ("®", "-r"),
    ("©", "-c"),
    ("@", "-at-"),
)

# Regex compiladas (modulo-level) — evita recompilar a cada chamada slugify().
_SPACE_RE = re.compile(r"\s+")
_NON_ASCII_SLUG_RE = re.compile(r"[^a-z0-9\- _]")
_MULTI_DASH_RE = re.compile(r"-+")


@__import__("functools").lru_cache(maxsize=4096)
def slugify(value: str, max_len: int = _MAX_FILENAME_LEN) -> str:
    """Gera slug seguro para arquivo Windows preservando informacao multi-idioma.

    Usa NFKD para normalizar acentos (Conceitos -> Conceitos preserva 'c'),
    converte simbolos comuns (C++ -> c-plus-plus, C# -> c-sharp), mantem
    transliteracao ASCII. Rejeita nomes reservados do Windows.

    Filtra:
      - Zero-width chars (U+200B..U+200D, U+FEFF) que sao invisiveis e corrompem filenames
      - RTL markers (U+202A..U+202E, U+061C) que reordenam graficamente
      - Simbolos comuns (™, ®, ©, @, &) com substituicao semantica

    Para idiomas que nao normalizam para ASCII (chines/arabe), usa hash MD5.
    """
    # 1. Remove chars invisiveis que quebram filenames
    value = _ZERO_WIDTH_CHARS.sub("", value)
    value = _RTL_MARKERS.sub("", value)

    # 2. Substituicoes semanticas ANTES de NFKD (preserva info)
    for src, dst in _SLUGIFY_SUBSTITUTIONS:
        value = value.replace(src, dst)

    # 3. Normalize: NFKD separa acentos do char base, ASCII descarta acentos
    # mas preserva a letra base (cafe -> cafe, nao 'c' soh)
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")

    ascii_value = _SPACE_RE.sub(" ", ascii_value).strip().lower()
    ascii_value = ascii_value.replace("/", "-")
    ascii_value = _INVALID_FILE_CHARS.sub("", ascii_value)
    ascii_value = _NON_ASCII_SLUG_RE.sub("", ascii_value)
    ascii_value = ascii_value.replace(" ", "-")
    ascii_value = _MULTI_DASH_RE.sub("-", ascii_value).strip("-")

    if not ascii_value:
        # Fallback para idiomas que nao normalizam para ASCII (chines/arabe/etc)
        digest = hashlib.md5(value.encode("utf-8")).hexdigest()[:8]
        return f"pagina-{digest}"

    if ascii_value in _WINDOWS_RESERVED_NAMES:
        ascii_value = ascii_value + "-page"
    if len(ascii_value) > max_len:
        digest = hashlib.md5(ascii_value.encode("utf-8")).hexdigest()[:8]
        ascii_value = ascii_value[: max_len - 9] + "-" + digest
    return ascii_value


def is_valid_pdf(path: Path) -> bool:
    """Valida estrutura basica de um PDF: header, EOF marker, tamanho minimo.

    Mais barato que abrir com PdfReader. Detecta arquivos vazios/parciais
    que Playwright pode deixar quando crasha durante page.pdf(). Tambem
    rejeita HTML disfarcado de PDF (servidor retorna HTML de erro mas
    Content-Type mente).
    """
    try:
        if not path.exists():
            return False
        size = path.stat().st_size
        if size < _MIN_VALID_PDF_SIZE:
            return False
        with path.open("rb") as fp:
            header = fp.read(16)
            if not header.startswith(b"%PDF-"):
                return False
            # Rejeita HTML de erro (alguns servidores entregam HTML com
            # Content-Type: application/pdf, ou Playwright captura err page)
            head_lower = header.lower()
            if b"<html" in head_lower or b"<!doctype" in head_lower:
                return False
            # Le os ultimos 1024 bytes para procurar pelo marcador %%EOF
            fp.seek(max(0, size - 1024))
            tail = fp.read()
        return b"%%EOF" in tail
    except OSError:
        return False


def sha256_file(path: Path, chunk_size: int = 65536) -> str:
    """Calcula SHA-256 de um arquivo (streaming, memoria O(1)).

    Retorna string hex 64 chars. Levanta OSError se nao conseguir ler.
    Usado para detectar corrupcao posterior de PDFs (disco ruim, antivirus).
    """
    hasher = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def count_pdf_pages(path: Path) -> int:
    """Conta paginas de um PDF. Retorna -1 se invalido/corrompido, N>=0 se valido.

    Distingue erro (-1, ex: PdfReader nao parsea) de PDF valido sem paginas
    (0, raro mas possivel). Callers devem checar `>= 0` para validade.
    """
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        return -1
    try:
        with path.open("rb") as fp:
            reader = PdfReader(fp)
            try:
                count = len(reader.pages)
            except (PdfReadError, ValueError, KeyError, AttributeError):
                return -1
            return count
    except (PdfReadError, OSError, ValueError, KeyError):
        return -1


def parse_scope(start_url: str) -> CrawlScope:
    parsed = urlparse(start_url)
    domain = parsed.netloc.lower()

    parts = [part for part in parsed.path.split("/") if part]
    space_key = None
    path_prefix = "/"

    if len(parts) >= 3 and parts[0] == "display":
        space_key = parts[2]
        path_prefix = f"/{parts[0]}/{parts[1]}/{parts[2]}/"
    elif len(parts) >= 2 and parts[0] == "spaces":
        space_key = parts[1]
        path_prefix = f"/{parts[0]}/{parts[1]}/"

    return CrawlScope(domain=domain, path_prefix=path_prefix, space_key=space_key)


_CONFLUENCE_EDIT_ACTIONS = {
    "editpage.action", "createpage.action", "editblogpost.action",
}


def is_url_in_scope(url: str, scope: CrawlScope) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc.lower() != scope.domain:
        return False

    path = parsed.path or "/"
    if path.startswith(scope.path_prefix):
        return True

    if not scope.space_key:
        return False
    return _matches_space_scope(path, parsed.query, scope.space_key)


def _matches_space_scope(path: str, query_string: str, space_key: str) -> bool:
    """Verifica se URL pertence ao espaco via patterns alternativos do Confluence."""
    if re.match(rf"^/display/(public/)?{re.escape(space_key)}/", path):
        return True

    query = parse_qs(query_string)
    if _is_valid_page_action(path, query):
        return True
    return query.get("spaceKey", [None])[0] == space_key


def _is_valid_page_action(path: str, query: dict) -> bool:
    """Aceita /pages/*.action com pageId, exceto acoes de edicao."""
    if not (path.startswith("/pages/") and path.endswith(".action")):
        return False
    action = path.rsplit("/", 1)[-1]
    if action in _CONFLUENCE_EDIT_ACTIONS:
        return False
    return "pageId" in query


def build_file_name(
    index: int, title: str, existing_names: Iterable[str], ext: str = ".pdf",
) -> str:
    """Gera nome de arquivo unico `NNNN-slug.ext`, com fallback por hash em colisao."""
    base = f"{index:04d}-{slugify(title)}"
    candidate = f"{base}{ext}"
    used = set(existing_names)
    suffix = 2

    while candidate in used:
        candidate = f"{base}-{suffix}{ext}"
        suffix += 1
        if suffix > 99:
            digest = hashlib.md5(f"{index}-{title}".encode("utf-8")).hexdigest()[:8]
            candidate = f"{base}-{digest}{ext}"
            break

    return candidate


def build_pdf_file_name(index: int, title: str, existing_names: Iterable[str]) -> str:
    return build_file_name(index, title, existing_names, ext=".pdf")
