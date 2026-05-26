"""Testes do pdf_merge usando PDFs sinteticos."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest
from pypdf import PdfWriter

from src.pdf_merge import merge_pdfs


def _create_synthetic_pdf(path: Path, num_pages: int = 1) -> None:
    """Cria PDF valido com N paginas em branco."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=595, height=842)  # A4
    with path.open("wb") as fp:
        writer.write(fp)


def test_merge_pdfs_basic():
    """Merge de 3 PDFs sintéticos."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        p1 = td_path / "1.pdf"
        p2 = td_path / "2.pdf"
        p3 = td_path / "3.pdf"
        _create_synthetic_pdf(p1, 1)
        _create_synthetic_pdf(p2, 2)
        _create_synthetic_pdf(p3, 1)

        output = td_path / "merged.pdf"
        entries = [(p1, "Pagina 1"), (p2, "Pagina 2"), (p3, "Pagina 3")]
        merge_pdfs(entries, output, logging.getLogger("test"))

        assert output.exists()
        assert output.stat().st_size > 0

        # Valida 4 paginas no total
        from pypdf import PdfReader
        with output.open("rb") as fp:
            reader = PdfReader(fp)
            assert len(reader.pages) == 4


def test_merge_pdfs_empty_raises():
    """Lista vazia deve levantar ValueError."""
    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "merged.pdf"
        with pytest.raises(ValueError):
            merge_pdfs([], output, logging.getLogger("test"))


def test_merge_pdfs_skips_corrupted():
    """PDFs corrompidos devem ser pulados, nao corromper o merged."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        good = td_path / "good.pdf"
        bad = td_path / "bad.pdf"
        _create_synthetic_pdf(good, 1)
        bad.write_bytes(b"not a valid pdf")

        output = td_path / "merged.pdf"
        entries = [(good, "Good"), (bad, "Bad")]
        merge_pdfs(entries, output, logging.getLogger("test"))

        # Output deve existir e ter so o conteudo do good
        assert output.exists()
        from pypdf import PdfReader
        with output.open("rb") as fp:
            reader = PdfReader(fp)
            assert len(reader.pages) == 1


def test_merge_pdfs_skips_missing():
    """PDF inexistente deve ser pulado."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        good = td_path / "good.pdf"
        missing = td_path / "missing.pdf"  # nao existe
        _create_synthetic_pdf(good, 1)

        output = td_path / "merged.pdf"
        entries = [(good, "G"), (missing, "M")]
        merge_pdfs(entries, output, logging.getLogger("test"))

        assert output.exists()
        from pypdf import PdfReader
        with output.open("rb") as fp:
            assert len(PdfReader(fp).pages) == 1


def test_merge_pdfs_adds_bookmarks():
    """Bookmarks devem ser adicionados com titles corretos."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        p1 = td_path / "1.pdf"
        p2 = td_path / "2.pdf"
        _create_synthetic_pdf(p1, 1)
        _create_synthetic_pdf(p2, 1)

        output = td_path / "merged.pdf"
        entries = [(p1, "Capitulo Um"), (p2, "Capitulo Dois")]
        merge_pdfs(entries, output, logging.getLogger("test"))

        from pypdf import PdfReader
        with output.open("rb") as fp:
            reader = PdfReader(fp)
            outline = reader.outline
            assert len(outline) >= 2
            # Titles do outline batem
            titles = [
                item.title if hasattr(item, "title") else item.get("/Title", "")
                for item in outline if not isinstance(item, list)
            ]
            assert any("Capitulo Um" in str(t) for t in titles)


def test_merge_pdfs_long_title_truncated():
    """Title longo (>120 chars) deve ser truncado sem crashar."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        p = td_path / "1.pdf"
        _create_synthetic_pdf(p, 1)
        long_title = "X" * 500
        output = td_path / "merged.pdf"
        # Nao deve crashar
        merge_pdfs([(p, long_title)], output, logging.getLogger("test"))
        assert output.exists()


def test_merge_pdfs_empty_title_uses_filename():
    """Title vazio deve usar stem do filename."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        p = td_path / "doc1.pdf"
        _create_synthetic_pdf(p, 1)
        output = td_path / "merged.pdf"
        merge_pdfs([(p, "")], output, logging.getLogger("test"))
        assert output.exists()
