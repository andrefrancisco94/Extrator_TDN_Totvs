"""Testes para os fixes/melhorias da v0.9.0."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.main import app

runner = CliRunner()


# =============================================================================
# utc_now_iso: timezone sempre +00:00
# =============================================================================

def test_utc_now_iso_has_timezone():
    """utc_now_iso retorna ISO 8601 com +00:00 ou Z explicito."""
    from src.utils import utc_now_iso
    ts = utc_now_iso()
    assert ts.endswith(("+00:00", "Z"))
    # Pode parsear de volta
    from datetime import datetime
    dt = datetime.fromisoformat(ts)
    assert dt.tzinfo is not None


def test_utc_now_iso_unique_over_time():
    """Timestamps consecutivos diferem (depois de 1s)."""
    import time
    from src.utils import utc_now_iso
    t1 = utc_now_iso()
    time.sleep(1.01)
    t2 = utc_now_iso()
    assert t1 != t2


# =============================================================================
# crawl_seen trim quando crawl completo
# =============================================================================

def test_record_crawl_complete_trims_crawl_seen():
    """record_crawl_complete limpa crawl_seen (redundante com mapped_urls)."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.crawl_seen = ["https://x.com/u1", "https://x.com/u2"] * 100  # gigante
        ck.record_crawl_complete(["https://x.com/u1", "https://x.com/u2"])
        assert ck.manifest.crawl_seen == []
        assert ck.manifest.crawl_complete is True


def test_record_crawl_complete_clears_queue():
    """record_crawl_complete tambem zera crawl_queue."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.crawl_queue = ["https://x.com/u3"]
        ck.record_crawl_complete(["https://x.com/u1"])
        assert ck.manifest.crawl_queue == []


# =============================================================================
# SHA-256 dos PDFs (data integrity)
# =============================================================================

def test_sha256_file_consistent(tmp_path, valid_pdf_bytes):
    """sha256_file retorna mesmo hash em chamadas consecutivas."""
    from src.utils import sha256_file
    p = tmp_path / "doc.pdf"
    p.write_bytes(valid_pdf_bytes)
    h1 = sha256_file(p)
    h2 = sha256_file(p)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_sha256_file_matches_hashlib(tmp_path):
    """Hash bate com hashlib.sha256 direto."""
    from src.utils import sha256_file
    p = tmp_path / "x.bin"
    data = b"hello world" * 100
    p.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    assert sha256_file(p) == expected


def test_sha256_file_streaming_large(tmp_path):
    """Funciona com arquivo grande (streaming, sem OOM)."""
    from src.utils import sha256_file
    p = tmp_path / "big.bin"
    # 5 MB
    with p.open("wb") as fp:
        for _ in range(50):
            fp.write(b"x" * 100_000)
    h = sha256_file(p)
    assert len(h) == 64


def test_record_export_with_hash():
    """record_export aceita pdf_hash e armazena em manifest."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.record_export(
            url="https://x.com/u1",
            filename="f.pdf", title="t", size_bytes=100, elapsed_seconds=1.0,
            pdf_hash="abc123" * 10 + "0000",  # 64 chars
        )
        ck.flush()
        entry = ck.manifest.exported["https://x.com/u1"]
        assert entry["sha256"] == "abc123" * 10 + "0000"


def test_record_export_without_hash_backward_compat():
    """record_export sem pdf_hash ainda funciona (campo opcional)."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.record_export(
            url="https://x.com/u1",
            filename="f.pdf", title="t", size_bytes=100, elapsed_seconds=1.0,
        )
        ck.flush()
        entry = ck.manifest.exported["https://x.com/u1"]
        assert "sha256" not in entry  # nao adiciona se nao fornecido


# =============================================================================
# Comando status
# =============================================================================

def test_status_command_no_manifest():
    """status em pasta sem manifest falha graciosamente."""
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(app, ["status", "--output-dir", td])
        assert result.exit_code == 1


def test_status_command_shows_progress():
    """status mostra mapeadas/exportadas/falhas/progresso."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com/a",
            "crawl_complete": True,
            "mapped_urls": ["u1", "u2", "u3", "u4"],
            "exported": {"u1": {"filename": "a.pdf", "title": "A", "size_bytes": 100, "elapsed_seconds": 1.0}},
            "failures": {"u2": {"error": "x", "attempts": 1, "last_attempt": ""}},
            "crawl_queue": [], "crawl_seen": [],
            "last_updated": "2026-01-01T10:00:00+00:00",
        }))
        result = runner.invoke(app, ["status", "--output-dir", str(root)])
        assert result.exit_code == 0
        # mapped_count=4, exported=1, failures=1, pending=2
        assert "4" in result.stdout  # mapeadas
        assert "1" in result.stdout  # exportados


def test_status_command_json():
    """status --json retorna JSON estruturado."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com/a",
            "crawl_complete": False,
            "mapped_urls": ["u1", "u2"],
            "exported": {"u1": {"filename": "a.pdf", "title": "A", "size_bytes": 100, "elapsed_seconds": 1.0}},
            "failures": {}, "crawl_queue": ["u3"],
            "crawl_seen": [], "last_updated": "2026-01-01T10:00:00+00:00",
        }))
        result = runner.invoke(app, ["status", "--output-dir", str(root), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["mapped_count"] == 2
        assert data["exported_count"] == 1
        assert data["queue_remaining"] == 1
        assert data["crawl_complete"] is False
        assert data["progress_pct"] == 50.0


# =============================================================================
# Comando clean-tmp
# =============================================================================

def test_clean_tmp_empty_pages():
    """clean-tmp em pasta sem pages/ avisa e sai."""
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(app, ["clean-tmp", "--output-dir", td, "--yes"])
        assert result.exit_code == 0


def test_clean_tmp_removes_tmp_files():
    """clean-tmp remove apenas arquivos .pdf.tmp."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pages = root / "pages"
        pages.mkdir()
        (pages / "doc.pdf").write_bytes(b"%PDF-1.4\n" + b"x" * 4000 + b"\n%%EOF")
        (pages / "doc.pdf.tmp").write_bytes(b"partial")
        (pages / "another.pdf.tmp").write_bytes(b"partial2")
        result = runner.invoke(app, ["clean-tmp", "--output-dir", str(root), "--yes"])
        assert result.exit_code == 0
        # PDF normal preservado
        assert (pages / "doc.pdf").exists()
        # .tmp removidos
        assert not (pages / "doc.pdf.tmp").exists()
        assert not (pages / "another.pdf.tmp").exists()


def test_clean_tmp_no_tmp_files():
    """clean-tmp sem .tmp avisa 'Nenhum'."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pages = root / "pages"
        pages.mkdir()
        (pages / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        result = runner.invoke(app, ["clean-tmp", "--output-dir", str(root), "--yes"])
        assert result.exit_code == 0
        assert "Nenhum" in result.stdout or "orfao" in result.stdout.lower()


# =============================================================================
# Flags --retry-failed-only e --quiet em help
# =============================================================================

def test_run_help_shows_new_flags():
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    for flag in ["--retry-failed-only", "--quiet"]:
        assert flag in result.stdout


# =============================================================================
# Help dos novos comandos
# =============================================================================

def test_status_help():
    result = runner.invoke(app, ["status", "--help"])
    assert result.exit_code == 0
    assert "snapshot" in result.stdout.lower() or "status" in result.stdout.lower()


def test_clean_tmp_help():
    result = runner.invoke(app, ["clean-tmp", "--help"])
    assert result.exit_code == 0
    assert ".pdf.tmp" in result.stdout or "tmp" in result.stdout


# =============================================================================
# Help command lista comandos novos
# =============================================================================

def test_main_help_lists_v09_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["status", "clean-tmp"]:
        assert cmd in result.stdout


# =============================================================================
# Integration: hash + record_export end-to-end
# =============================================================================

def test_hash_in_manifest_after_record_export(tmp_path, valid_pdf_bytes):
    """sha256_file + record_export -> manifest tem sha256."""
    from src.utils import Checkpoint, sha256_file
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(valid_pdf_bytes)
    h = sha256_file(pdf)

    ck = Checkpoint(tmp_path, "https://x.com/a")
    ck.record_export(
        url="https://x.com/page", filename="doc.pdf", title="T",
        size_bytes=len(valid_pdf_bytes), elapsed_seconds=1.0, pdf_hash=h,
    )
    ck.flush()
    entry = ck.manifest.exported["https://x.com/page"]
    assert entry["sha256"] == h
    # Hash bate com hashlib direto
    assert entry["sha256"] == hashlib.sha256(valid_pdf_bytes).hexdigest()
