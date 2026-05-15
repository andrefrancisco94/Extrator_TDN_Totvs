from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter


def merge_pdfs(pdf_files: list[Path], output_file: Path, logger) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not pdf_files:
        raise ValueError("Nenhum PDF disponivel para mesclar.")

    writer = PdfWriter()
    try:
        for file in pdf_files:
            writer.append(str(file))
        with output_file.open("wb") as fp:
            writer.write(fp)
    finally:
        writer.close()

    logger.info("PDF consolidado gerado: %s", output_file)
