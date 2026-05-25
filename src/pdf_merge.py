from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
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


def merge_pdfs(pdf_files: list[Path], output_file: Path, logger) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not pdf_files:
        raise ValueError("Nenhum PDF disponivel para mesclar.")

    console = get_console()
    skipped: list[tuple[Path, str]] = []
    writer = PdfWriter()

    try:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold magenta]Mesclando PDFs"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )
        with progress:
            task = progress.add_task("merge", total=len(pdf_files))
            for file in pdf_files:
                try:
                    writer.append(str(file))
                except (PdfReadError, OSError, ValueError) as exc:
                    logger.warning("Pulando PDF corrompido %s: %s", file.name, exc)
                    skipped.append((file, str(exc)))
                progress.advance(task)

        with output_file.open("wb") as fp:
            writer.write(fp)
    finally:
        writer.close()

    if skipped:
        logger.warning("PDFs ignorados na consolidacao: %s", len(skipped))

    logger.info("PDF consolidado gerado: %s", output_file)
