from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from .utils import get_console


def merge_pdfs(
    pdf_entries: list[tuple[Path, str]],
    output_file: Path,
    logger,
    consolidated_title: str = "TDN TOTVS - Consolidado",
) -> None:
    """Mescla PDFs em consolidado com bookmarks, metadata e compressao.

    pdf_entries: lista de (path, titulo). Titulo vira marcador navegavel.
    Escreve em arquivo temporario e faz rename atomico no fim.
    Aplica compressao de streams para reduzir tamanho do consolidado.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not pdf_entries:
        raise ValueError("Nenhum PDF disponivel para mesclar.")

    console = get_console()
    skipped: list[tuple[Path, str]] = []
    writer = PdfWriter()
    tmp_file = output_file.with_suffix(output_file.suffix + ".tmp")

    try:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold magenta]Mesclando PDFs"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("|"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        with progress:
            task = progress.add_task("merge", total=len(pdf_entries))
            for entry_path, entry_title in pdf_entries:
                _append_with_bookmark(writer, entry_path, entry_title, skipped, logger)
                progress.advance(task)

        _set_metadata(writer, consolidated_title)
        _compress_streams(writer, logger)

        with tmp_file.open("wb") as fp:
            writer.write(fp)
    except Exception:
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass
        raise
    finally:
        writer.close()

    try:
        os.replace(str(tmp_file), str(output_file))
    except OSError as exc:
        logger.error("Falha ao renomear PDF consolidado: %s", exc)
        raise

    if skipped:
        logger.warning("PDFs ignorados na consolidacao: %s", len(skipped))

    logger.info(
        "PDF consolidado gerado: %s (%d entradas, %d ignoradas)",
        output_file, len(pdf_entries) - len(skipped), len(skipped),
    )


def _append_with_bookmark(
    writer: PdfWriter,
    entry_path: Path,
    entry_title: str,
    skipped: list[tuple[Path, str]],
    logger,
) -> None:
    """Appenda um PDF ao writer e adiciona bookmark apontando para a primeira pagina."""
    try:
        if not entry_path.exists() or entry_path.stat().st_size == 0:
            logger.warning("Pulando PDF inexistente/vazio: %s", entry_path.name)
            skipped.append((entry_path, "vazio ou inexistente"))
            return
    except OSError as exc:
        logger.warning("Pulando PDF inacessivel %s: %s", entry_path.name, exc)
        skipped.append((entry_path, str(exc)))
        return

    page_index_before = len(writer.pages)
    try:
        # Le primeiro para validar (evita corromper writer com PDF quebrado)
        with entry_path.open("rb") as fp:
            reader = PdfReader(fp)
            for p in reader.pages:
                writer.add_page(p)
    except (PdfReadError, OSError, ValueError, KeyError) as exc:
        logger.warning("Pulando PDF corrompido %s: %s", entry_path.name, exc)
        skipped.append((entry_path, str(exc)))
        return

    # Bookmark com o titulo da pagina, apontando para primeira pagina do PDF
    safe_title = _sanitize_bookmark_title(
        entry_title or entry_path.stem, entry_path.stem,
    )
    try:
        writer.add_outline_item(safe_title, page_index_before)
    except (ValueError, KeyError, OSError) as exc:
        logger.warning("Bookmark falhou para %s: %s", safe_title, exc)
        # Fallback: bookmark minimalista com filename stem
        try:
            writer.add_outline_item(entry_path.stem[:50], page_index_before)
        except (ValueError, KeyError, OSError):
            logger.debug("Bookmark fallback tambem falhou para %s", entry_path.name)


_PDF_OUTLINE_MAX_TITLE_LEN = 120  # Limite pratico do PDF outline


def _sanitize_bookmark_title(raw_title: str, fallback: str) -> str:
    """Sanitiza titulo para bookmark do PDF (remove chars problematicos)."""
    import re as _re
    safe = (raw_title or fallback).strip()
    if not safe:
        safe = fallback
    # Remove chars que algumas implementacoes de PDF outline nao suportam
    safe = safe.replace("™", "TM").replace("®", "(R)").replace("©", "(C)")
    # Zero-width chars
    safe = _re.sub(r"[​-‍﻿]", "", safe)
    # Trunca
    return safe[:_PDF_OUTLINE_MAX_TITLE_LEN]


def _set_metadata(writer: PdfWriter, title: str) -> None:
    """Define metadata do PDF consolidado (titulo, autor, datas em UTC)."""
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    # PDF date format com timezone: D:YYYYMMDDHHmmSS+00'00'
    pdf_date = "D:" + now.strftime("%Y%m%d%H%M%S") + "+00'00'"
    try:
        writer.add_metadata({
            "/Title": title,
            "/Author": "Extrator TDN TOTVS",
            "/Producer": "Extrator TDN TOTVS (pypdf)",
            "/Creator": "Extrator TDN TOTVS",
            "/CreationDate": pdf_date,
            "/ModDate": pdf_date,
        })
    except (ValueError, KeyError):
        pass


_COMPRESS_PAGE_LIMIT = 5000  # acima disso, pula compressao (OOM risk)


def _compress_streams(writer: PdfWriter, logger) -> None:
    """Comprime streams de conteudo do PDF consolidado (reduz tamanho).

    pypdf aplica zlib em streams nao comprimidos. Falha silenciosamente
    em PDFs problematicos (best-effort). Em PDFs muito grandes (>5000 paginas),
    pula a compressao para evitar OOM (cada page.compress carrega stream
    inteiro na memoria).
    """
    try:
        page_count = len(writer.pages)
        if page_count > _COMPRESS_PAGE_LIMIT:
            logger.info(
                "PDF muito grande (%d paginas > %d), pulando compressao para "
                "evitar OOM. Use pdftk/qpdf externamente se quiser comprimir.",
                page_count, _COMPRESS_PAGE_LIMIT,
            )
            return
        for page in writer.pages:
            try:
                page.compress_content_streams()
            except (ValueError, KeyError, AttributeError, OSError, MemoryError):
                continue
    except MemoryError:
        logger.warning("Sem memoria para compressao de streams, continuando")
    except Exception as exc:  # noqa: BLE001
        logger.debug("Compressao de streams falhou (continuando sem): %s", exc)
