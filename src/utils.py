from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse, urlunparse


KEEP_QUERY_PARAMS = {"pageId", "spaceKey", "title"}


@dataclass(frozen=True)
class CrawlScope:
    domain: str
    path_prefix: str
    space_key: str | None


def ensure_output_dirs(output_dir: Path) -> tuple[Path, Path]:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "run.log"
    return pages_dir, log_file


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("tdn_extractor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


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


def slugify(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip().lower()
    value = value.replace("/", "-")
    value = re.sub(r"[^a-z0-9\- _]", "", value)
    value = value.replace(" ", "-")
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "pagina"


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


def is_url_in_scope(url: str, scope: CrawlScope) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False

    if parsed.netloc.lower() != scope.domain:
        return False

    path = parsed.path or "/"
    if path.startswith(scope.path_prefix):
        return True

    if scope.space_key:
        # Confluence can mix /display/SPACE and /display/public/SPACE for public spaces.
        if re.match(rf"^/display/(public/)?{re.escape(scope.space_key)}/", path):
            return True

        # Confluence usa vários padrões de URL com pageId (viewpage, releaseview, etc.)
        # Aceita qualquer /pages/*.action com pageId, exceto ações de edição/admin.
        _EDIT_ACTIONS = {"editpage.action", "createpage.action", "editblogpost.action"}
        if path.startswith("/pages/") and path.endswith(".action"):
            action = path.rsplit("/", 1)[-1]
            if action not in _EDIT_ACTIONS:
                query = parse_qs(parsed.query)
                if "pageId" in query:
                    return True

        query = parse_qs(parsed.query)
        if query.get("spaceKey", [None])[0] == scope.space_key:
            return True

    return False


def build_pdf_file_name(index: int, title: str, existing_names: Iterable[str]) -> str:
    base = f"{index:04d}-{slugify(title)}"
    candidate = f"{base}.pdf"
    used = set(existing_names)
    suffix = 2

    while candidate in used:
        candidate = f"{base}-{suffix}.pdf"
        suffix += 1

    return candidate
