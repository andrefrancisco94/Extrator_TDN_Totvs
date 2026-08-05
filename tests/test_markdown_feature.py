"""Testes da exportacao em Markdown: checkpoint (exported_md), file naming e merge.

Cobre apenas a logica pura (sem Playwright): a parte que navega/renderiza
paginas ja reusa os mesmos helpers testados em test_v*_fixes.py / test_browser.py
para o pipeline de PDF.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import pytest

from src.markdown_exporter import _download_asset, _localize_and_convert
from src.markdown_merge import merge_markdown
from src.utils import MANIFEST_FILENAME, Checkpoint, build_file_name, build_pdf_file_name


# ---------------------------------------------------------------------------
# build_file_name (generalizacao de build_pdf_file_name)
# ---------------------------------------------------------------------------


def test_build_file_name_md_extension():
    name = build_file_name(1, "Pagina A", set(), ext=".md")
    assert name == "0001-pagina-a.md"


def test_build_file_name_pdf_matches_legacy_helper():
    """build_pdf_file_name deve continuar identico apos a refatoracao."""
    existing = {"0001-pagina-a.pdf"}
    assert build_pdf_file_name(1, "Pagina A", existing) == build_file_name(
        1, "Pagina A", existing, ext=".pdf",
    )


def test_build_file_name_dedup_across_extensions():
    """Colisao de slug com .md nao deve considerar nomes .pdf existentes (namespaces distintos)."""
    md_name = build_file_name(1, "Pagina A", set(), ext=".md")
    pdf_name = build_file_name(1, "Pagina A", set(), ext=".pdf")
    assert md_name != pdf_name
    assert md_name.endswith(".md")
    assert pdf_name.endswith(".pdf")


# ---------------------------------------------------------------------------
# Checkpoint.exported_md (namespace independente de `exported`)
# ---------------------------------------------------------------------------


def test_checkpoint_record_export_md_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        output_dir = Path(td)
        md_dir = output_dir / "markdown" / "pages"
        md_dir.mkdir(parents=True)
        (md_dir / "0001-pagina.md").write_text("# Pagina\n", encoding="utf-8")

        ck = Checkpoint(output_dir, "https://x.com/a")
        ck.record_export_md(
            url="https://x.com/a", filename="0001-pagina.md", title="Pagina",
            size_bytes=10, elapsed_seconds=1.2,
        )
        ck.flush()

        assert ck.is_exported_md("https://x.com/a", md_dir)

        # Reload do disco preserva exported_md
        ck2 = Checkpoint(output_dir, "https://x.com/a")
        assert "https://x.com/a" in ck2.manifest.exported_md
        assert ck2.manifest.exported_md["https://x.com/a"]["filename"] == "0001-pagina.md"


def test_checkpoint_exported_md_independent_from_exported():
    """Uma URL pode estar em `exported` (PDF) sem estar em `exported_md`, e vice-versa."""
    with tempfile.TemporaryDirectory() as td:
        output_dir = Path(td)
        pages_dir = output_dir / "pages"
        pages_dir.mkdir()
        md_dir = output_dir / "markdown" / "pages"
        md_dir.mkdir(parents=True)

        ck = Checkpoint(output_dir, "https://x.com/a")
        ck.record_export_md(
            url="https://x.com/a", filename="0001-a.md", title="A",
            size_bytes=5, elapsed_seconds=0.1,
        )
        (md_dir / "0001-a.md").write_text("conteudo", encoding="utf-8")

        assert ck.is_exported_md("https://x.com/a", md_dir)
        assert not ck.is_exported("https://x.com/a", pages_dir)  # sem PDF gerado


def test_checkpoint_loads_manifest_without_exported_md_field():
    """Manifests antigos (sem `exported_md`) devem carregar normalmente (retrocompat)."""
    with tempfile.TemporaryDirectory() as td:
        output_dir = Path(td)
        manifest_path = output_dir / MANIFEST_FILENAME
        manifest_path.write_text(json.dumps({
            "version": 2, "start_url": "https://x.com/a",
            "crawl_complete": True, "mapped_urls": ["https://x.com/a"],
            "crawl_queue": [], "crawl_seen": [],
            "exported": {}, "failures": {},
        }), encoding="utf-8")

        ck = Checkpoint(output_dir, "https://x.com/a")
        assert ck.load_warning is None
        assert ck.manifest.exported_md == {}


def test_reconcile_md_with_disk_removes_missing_files(mock_logger):
    with tempfile.TemporaryDirectory() as td:
        output_dir = Path(td)
        md_dir = output_dir / "markdown" / "pages"
        md_dir.mkdir(parents=True)

        ck = Checkpoint(output_dir, "https://x.com/a")
        ck.manifest.exported_md["https://x.com/a"] = {
            "filename": "missing.md", "title": "A", "size_bytes": 1, "elapsed_seconds": 0.1,
        }
        removed = ck.reconcile_md_with_disk(md_dir, mock_logger)
        assert removed == 1
        assert "https://x.com/a" not in ck.manifest.exported_md


# ---------------------------------------------------------------------------
# merge_markdown
# ---------------------------------------------------------------------------


def test_merge_markdown_basic():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        p1 = td_path / "0001-a.md"
        p2 = td_path / "0002-b.md"
        p1.write_text("# Pagina A\n\nFonte: https://x.com/a\n\nConteudo A", encoding="utf-8")
        p2.write_text("# Pagina B\n\nFonte: https://x.com/b\n\nConteudo B", encoding="utf-8")

        output = td_path / "consolidado.md"
        entries = [(p1, "Pagina A", "https://x.com/a"), (p2, "Pagina B", "https://x.com/b")]
        merge_markdown(entries, output, logging.getLogger("test"))

        assert output.exists()
        text = output.read_text(encoding="utf-8")
        assert "## Indice" in text
        assert "[Pagina A](#page-1)" in text
        assert "[Pagina B](#page-2)" in text
        assert '<a id="page-1"></a>' in text
        assert '<a id="page-2"></a>' in text
        assert "Conteudo A" in text and "Conteudo B" in text


def test_merge_markdown_empty_raises():
    with tempfile.TemporaryDirectory() as td:
        output = Path(td) / "consolidado.md"
        with pytest.raises(ValueError):
            merge_markdown([], output, logging.getLogger("test"))


def test_merge_markdown_skips_unreadable():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        good = td_path / "good.md"
        good.write_text("# Good\n\nConteudo", encoding="utf-8")
        missing = td_path / "missing.md"  # nao existe

        output = td_path / "consolidado.md"
        entries = [(good, "Good", "https://x.com/a"), (missing, "Missing", "https://x.com/b")]
        merge_markdown(entries, output, logging.getLogger("test"))

        text = output.read_text(encoding="utf-8")
        assert "Good" in text
        assert "Conteudo" in text


def test_merge_markdown_empty_title_uses_filename_stem():
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        p = td_path / "doc1.md"
        p.write_text("# X\n\nY", encoding="utf-8")
        output = td_path / "consolidado.md"
        merge_markdown([(p, "", "https://x.com/a")], output, logging.getLogger("test"))
        text = output.read_text(encoding="utf-8")
        assert "[doc1](#page-1)" in text


# ---------------------------------------------------------------------------
# _localize_and_convert / _download_asset (com Page.request mockado)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, ok: bool, status: int, body: bytes):
        self.ok = ok
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeRequestContext:
    def __init__(self, body: bytes = b"fake-image-bytes", ok: bool = True):
        self._body = body
        self._ok = ok
        self.calls: list[str] = []

    async def get(self, url: str, timeout: int = 30_000):
        self.calls.append(url)
        return _FakeResponse(self._ok, 200 if self._ok else 404, self._body)


class _FakePage:
    def __init__(self, request_ctx: _FakeRequestContext):
        self.request = request_ctx


def test_download_asset_writes_file_and_caches(mock_logger):
    with tempfile.TemporaryDirectory() as td:
        attachments_dir = Path(td) / "attachments" / "pagina-a"
        page = _FakePage(_FakeRequestContext(body=b"hello-bytes"))
        downloaded: dict[str, str] = {}
        used_names: set[str] = set()

        rel1 = asyncio.run(_download_asset(
            "https://tdn.totvs.com/download/attachments/1/foo.png",
            page, attachments_dir, "pagina-a", downloaded, used_names, mock_logger,
        ))
        assert rel1 == "../attachments/pagina-a/foo.png"
        assert (attachments_dir / "foo.png").read_bytes() == b"hello-bytes"
        assert len(page.request.calls) == 1

        # Segunda chamada com a MESMA URL usa cache (nao rebaixa)
        rel2 = asyncio.run(_download_asset(
            "https://tdn.totvs.com/download/attachments/1/foo.png",
            page, attachments_dir, "pagina-a", downloaded, used_names, mock_logger,
        ))
        assert rel2 == rel1
        assert len(page.request.calls) == 1


def test_download_asset_name_collision_deduplicated(mock_logger):
    with tempfile.TemporaryDirectory() as td:
        attachments_dir = Path(td) / "attachments" / "pagina-a"
        page = _FakePage(_FakeRequestContext(body=b"x"))
        downloaded: dict[str, str] = {}
        used_names: set[str] = set()

        rel1 = asyncio.run(_download_asset(
            "https://tdn.totvs.com/download/attachments/1/foo.png",
            page, attachments_dir, "pagina-a", downloaded, used_names, mock_logger,
        ))
        rel2 = asyncio.run(_download_asset(
            "https://tdn.totvs.com/download/attachments/2/foo.png",
            page, attachments_dir, "pagina-a", downloaded, used_names, mock_logger,
        ))
        assert rel1 != rel2
        assert (attachments_dir / "foo.png").exists()
        assert (attachments_dir / "foo-2.png").exists()


def test_download_asset_returns_none_on_http_error(mock_logger):
    with tempfile.TemporaryDirectory() as td:
        attachments_dir = Path(td) / "attachments" / "pagina-a"
        page = _FakePage(_FakeRequestContext(ok=False))
        rel = asyncio.run(_download_asset(
            "https://tdn.totvs.com/download/attachments/1/foo.png",
            page, attachments_dir, "pagina-a", {}, set(), mock_logger,
        ))
        assert rel is None
        assert not attachments_dir.exists()


def test_localize_and_convert_rewrites_image_and_converts_markdown(mock_logger):
    with tempfile.TemporaryDirectory() as td:
        attachments_dir = Path(td) / "attachments"
        page = _FakePage(_FakeRequestContext(body=b"png-bytes"))
        html = (
            '<p>Texto com <strong>negrito</strong>.</p>'
            '<img src="/download/attachments/123/diagrama.png" alt="Diagrama">'
        )
        markdown_text = asyncio.run(_localize_and_convert(
            html, "https://tdn.totvs.com/display/PROT/Pagina", page,
            attachments_dir, "pagina-a", mock_logger,
        ))

        assert "negrito" in markdown_text
        assert "../attachments/pagina-a/diagrama.png" in markdown_text
        assert (attachments_dir / "pagina-a" / "diagrama.png").read_bytes() == b"png-bytes"


def test_localize_and_convert_ignores_unrelated_links(mock_logger):
    with tempfile.TemporaryDirectory() as td:
        attachments_dir = Path(td) / "attachments"
        page = _FakePage(_FakeRequestContext())
        html = '<p><a href="https://outro-site.com/pagina">link externo</a></p>'
        markdown_text = asyncio.run(_localize_and_convert(
            html, "https://tdn.totvs.com/display/PROT/Pagina", page,
            attachments_dir, "pagina-a", mock_logger,
        ))
        assert "link externo" in markdown_text
        assert "https://outro-site.com/pagina" in markdown_text
        assert not page.request.calls
