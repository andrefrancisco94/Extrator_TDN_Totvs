from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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

_INVALID_FILE_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_MAX_FILENAME_LEN = 80

# Nomes de dispositivo reservados no Windows (em qualquer caixa, com ou sem extensao)
_WINDOWS_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
})

# Tamanho minimo aceitavel para um PDF gerado (header + estrutura basica > 1KB)
_MIN_VALID_PDF_SIZE = 1024

_console: Console | None = None


def get_console() -> Console:
    global _console
    if _console is None:
        _console = Console(highlight=False)
    return _console


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


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
    version: int = 1
    start_url: str = ""
    started_at: str = ""
    last_updated: str = ""
    crawl_complete: bool = False
    # Limite de paginas usado no crawl que produziu mapped_urls.
    # Permite detectar quando o usuario muda max_pages e force re-crawl.
    crawl_max_pages: int | None = None
    mapped_urls: list[str] = field(default_factory=list)
    # url -> {filename, title, size_bytes, elapsed_seconds}
    exported: dict[str, dict] = field(default_factory=dict)
    # url -> error
    failures: dict[str, str] = field(default_factory=dict)


class Checkpoint:
    """Gerencia leitura/escrita do manifest.json (atomic + tolerante a corrupcao).

    Quando o arquivo existe mas esta corrompido ou e de versao incompativel,
    armazena o motivo em `load_warning` para o caller poder alertar o usuario.
    """

    def __init__(self, output_dir: Path, start_url: str, logger=None):
        self.path = output_dir / "manifest.json"
        self.load_warning: str | None = None
        self.manifest = self._load_or_create(start_url)
        if self.load_warning and logger is not None:
            logger.warning("Manifest descartado: %s (recomecando do zero)", self.load_warning)

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

        if data.get("version") != 1:
            self.load_warning = (
                f"manifest.json com versao desconhecida ({data.get('version')!r})"
            )
            return self._fresh(start_url)
        if data.get("start_url") != start_url:
            # Mudou a URL inicial — silencioso, e esperado quando muda projeto.
            return self._fresh(start_url)

        crawl_complete = bool(data.get("crawl_complete", False))
        mapped_urls = list(data.get("mapped_urls", []))
        # Consistencia: crawl_complete=True com mapped_urls=[] e estado invalido
        if crawl_complete and not mapped_urls:
            self.load_warning = "manifest.json marcado como crawl_complete mas sem URLs"
            return self._fresh(start_url)

        return Manifest(
            version=int(data.get("version", 1)),
            start_url=str(data.get("start_url", start_url)),
            started_at=str(data.get("started_at", "")),
            last_updated=str(data.get("last_updated", "")),
            crawl_complete=crawl_complete,
            crawl_max_pages=data.get("crawl_max_pages"),
            mapped_urls=mapped_urls,
            exported=dict(data.get("exported", {})),
            failures=dict(data.get("failures", {})),
        )

    @staticmethod
    def _fresh(start_url: str) -> Manifest:
        return Manifest(
            start_url=start_url,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def save(self) -> None:
        """Salva manifest atomicamente (tmp + replace). Tolerante a falhas de IO."""
        self.manifest.last_updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(asdict(self.manifest), fp, indent=2, ensure_ascii=False)
            os.replace(str(tmp), str(self.path))
        except OSError:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise

    def record_crawl_complete(self, urls: list[str], max_pages: int | None = None) -> None:
        self.manifest.crawl_complete = True
        self.manifest.mapped_urls = list(urls)
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

    def get_exported_entry(self, url: str) -> dict | None:
        return self.manifest.exported.get(url)

    def record_export(
        self,
        url: str,
        filename: str,
        title: str,
        size_bytes: int,
        elapsed_seconds: float,
    ) -> None:
        self.manifest.exported[url] = {
            "filename": filename,
            "title": title,
            "size_bytes": size_bytes,
            "elapsed_seconds": elapsed_seconds,
        }
        # Remove de failures se estava la (re-tentativa bem-sucedida)
        self.manifest.failures.pop(url, None)
        self.save()

    def record_failure(self, url: str, error: str) -> None:
        self.manifest.failures[url] = error
        self.save()


class InvalidStartUrlError(ValueError):
    """URL inicial invalida (scheme nao-http, sem dominio, etc)."""


def validate_start_url(url: str) -> str:
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

    return canonicalize_url(stripped)


def ensure_output_dirs(output_dir: Path) -> tuple[Path, Path]:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "run.log"
    return pages_dir, log_file


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("tdn_extractor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(file_handler)

    rich_handler = RichHandler(
        console=get_console(),
        show_path=False,
        show_time=True,
        rich_tracebacks=True,
        markup=False,
    )
    rich_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(rich_handler)

    return logger


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


def slugify(value: str, max_len: int = _MAX_FILENAME_LEN) -> str:
    """Gera slug seguro para arquivo Windows.

    Limita comprimento e rejeita nomes reservados do Windows (CON, PRN, AUX,
    NUL, COMn, LPTn) que falham silenciosamente ao serem criados.
    """
    value = re.sub(r"\s+", " ", value).strip().lower()
    value = value.replace("/", "-")
    value = _INVALID_FILE_CHARS.sub("", value)
    value = re.sub(r"[^a-z0-9\- _]", "", value)
    value = value.replace(" ", "-")
    value = re.sub(r"-+", "-", value).strip("-")
    if not value:
        return "pagina"
    # Nomes reservados Windows (qualquer extensao subsequente sera ignorada pelo SO)
    if value in _WINDOWS_RESERVED_NAMES:
        value = value + "-page"
    if len(value) > max_len:
        digest = hashlib.md5(value.encode("utf-8")).hexdigest()[:8]
        value = value[: max_len - 9] + "-" + digest
    return value


def is_valid_pdf(path: Path) -> bool:
    """Valida estrutura basica de um PDF: header, EOF marker, tamanho minimo.

    Mais barato que abrir com PdfReader. Detecta arquivos vazios/parciais
    que Playwright pode deixar quando crasha durante page.pdf().
    """
    try:
        if not path.exists():
            return False
        size = path.stat().st_size
        if size < _MIN_VALID_PDF_SIZE:
            return False
        with path.open("rb") as fp:
            header = fp.read(5)
            if header != b"%PDF-":
                return False
            # Le os ultimos 1024 bytes para procurar pelo marcador %%EOF
            fp.seek(max(0, size - 1024))
            tail = fp.read()
        return b"%%EOF" in tail
    except OSError:
        return False


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


def build_pdf_file_name(index: int, title: str, existing_names: Iterable[str]) -> str:
    base = f"{index:04d}-{slugify(title)}"
    candidate = f"{base}.pdf"
    used = set(existing_names)
    suffix = 2

    while candidate in used:
        candidate = f"{base}-{suffix}.pdf"
        suffix += 1
        if suffix > 99:
            digest = hashlib.md5(f"{index}-{title}".encode("utf-8")).hexdigest()[:8]
            candidate = f"{base}-{digest}.pdf"
            break

    return candidate
