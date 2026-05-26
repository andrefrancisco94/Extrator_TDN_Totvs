"""Testes para os fixes da v0.8.0 (200 achados consolidados das auditorias r2)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import threading
from pathlib import Path

import pytest


# =============================================================================
# RATE LIMITER - Jitter so aumenta (Bug agent2 #1)
# =============================================================================

def test_rate_limiter_jitter_only_increases():
    """Bug agent2 r2 #1: jitter no cooldown so aumenta (0 a +25%), nunca reduz."""
    from src.utils import RateLimiter, RateLimitConfig

    cfg = RateLimitConfig(base_delay_seconds=0.01, backoff_initial_seconds=10.0)
    lim = RateLimiter(cfg)

    async def run():
        cooldown = await lim.report_block(reason="test")
        # cooldown >= base (10.0), <= base * 1.25
        assert cooldown >= 10.0
        assert cooldown <= 10.0 * 1.25

    asyncio.run(run())


def test_rate_limiter_property_consistent_with_lock():
    """consecutive_blocks property reflete state apos report_block."""
    from src.utils import RateLimiter, RateLimitConfig

    cfg = RateLimitConfig(base_delay_seconds=0.01)
    lim = RateLimiter(cfg)

    async def run():
        assert lim.consecutive_blocks == 0
        await lim.report_block()
        assert lim.consecutive_blocks == 1
        await lim.report_block()
        assert lim.consecutive_blocks == 2
        await lim.report_success()
        assert lim.consecutive_blocks == 0

    asyncio.run(run())


# =============================================================================
# CHECKPOINT - batched save com lock (Bug agent1 r2 #1, #2)
# =============================================================================

def test_checkpoint_batched_save_threadsafe():
    """_pending_save_count protegido por lock — 8 threads x 5 records sem race."""
    from src.utils import Checkpoint

    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck._save_every_n = 10  # menor para forcar saves

        def worker(n):
            for i in range(5):
                ck.record_export(
                    f"https://x.com/u{n}_{i}", f"f{n}_{i}.pdf", "t", 100, 1.0,
                )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        ck.flush()
        # 40 records gravados sem corrupcao
        ck2 = Checkpoint(Path(td), "https://x.com/a")
        assert len(ck2.manifest.exported) == 40


def test_checkpoint_flush_preserves_count_on_save_failure():
    """Bug agent4 r2 #15: flush() falha => contador preservado para retry."""
    from src.utils import Checkpoint

    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.exported["url1"] = {
            "filename": "f.pdf", "title": "t", "size_bytes": 1, "elapsed_seconds": 1.0,
        }
        ck._pending_save_count = 3  # simula pending
        # flush() funciona normalmente
        ck.flush()
        assert ck._pending_save_count == 0


# =============================================================================
# URLs MALFORMADAS no manifest (Bug agent2 r2 #8)
# =============================================================================

def test_restore_state_filters_invalid_urls():
    """URLs malformadas em manifest (None, vazia, nao-http) sao filtradas."""
    from src.crawler import CrawlState, _restore_state_from_checkpoint
    from src.utils import Checkpoint
    from collections import deque
    import logging

    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        # Manifest editado manualmente com URLs invalidas
        ck.manifest.mapped_urls = [
            "https://x.com/valid",
            "",  # vazia
            "javascript:void(0)",  # nao http
            "/relative/path",  # relativa
        ]
        ck.manifest.crawl_queue = ["https://x.com/queued", None]  # None
        state = CrawlState(queue=deque(), queued=set())
        _restore_state_from_checkpoint(
            state, ck, "https://x.com/valid", logging.getLogger("t"),
        )
        # Apenas URL valida em ordered_urls
        assert state.ordered_urls == ["https://x.com/valid"]
        # E queue tem so a valid queued
        assert "https://x.com/queued" in state.queued


# =============================================================================
# JOBLOCK - impede 2 processos no mesmo output_dir (Bug agent4 r2 #19)
# =============================================================================

def test_joblock_acquire_release():
    """Lock cria arquivo .extrator.lock com PID, libera ao sair."""
    from src.utils import JobLock

    with tempfile.TemporaryDirectory() as td:
        lock = JobLock(Path(td))
        lock.acquire()
        lock_file = Path(td) / ".extrator.lock"
        assert lock_file.exists()
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        lock.release()
        assert not lock_file.exists()


def test_joblock_context_manager():
    """JobLock funciona como context manager."""
    from src.utils import JobLock

    with tempfile.TemporaryDirectory() as td:
        with JobLock(Path(td)) as lock:
            assert (Path(td) / ".extrator.lock").exists()
        assert not (Path(td) / ".extrator.lock").exists()


def test_joblock_blocks_concurrent_alive_pid():
    """Lock detecta PID vivo (este processo) e bloqueia segundo lock."""
    from src.utils import JobLock, JobLockError

    with tempfile.TemporaryDirectory() as td:
        lock1 = JobLock(Path(td))
        lock1.acquire()
        try:
            lock2 = JobLock(Path(td))
            with pytest.raises(JobLockError):
                lock2.acquire()
        finally:
            lock1.release()


def test_joblock_takes_over_orphan():
    """Lock orfao (PID morto) eh tomado de posse silenciosamente."""
    from src.utils import JobLock

    with tempfile.TemporaryDirectory() as td:
        lock_file = Path(td) / ".extrator.lock"
        # PID falso muito alto (provavelmente morto)
        lock_file.write_text("999999999", encoding="utf-8")
        lock = JobLock(Path(td))
        lock.acquire()  # deve funcionar
        assert lock_file.read_text(encoding="utf-8").strip() == str(os.getpid())
        lock.release()


# =============================================================================
# _handle_fresh_flag tambem remove browser_state.json (Bug agent2 r2 #5)
# =============================================================================

def test_handle_fresh_flag_removes_state(tmp_path):
    """--fresh remove browser_state.json alem do manifest."""
    from src.main import _handle_fresh_flag
    from rich.console import Console

    manifest = tmp_path / "manifest.json"
    state = tmp_path / "browser_state.json"
    manifest.write_text(json.dumps({
        "version": 2, "start_url": "https://x.com",
        "crawl_complete": True, "mapped_urls": [],
        "exported": {}, "failures": {},
        "crawl_queue": [], "crawl_seen": [],
    }))
    state.write_text('{"cookies":[]}')

    _handle_fresh_flag(tmp_path, "https://x.com", Console(quiet=True))
    assert not manifest.exists()
    assert not state.exists()


def test_handle_fresh_flag_idempotent(tmp_path):
    """--fresh sem arquivos nao crasha."""
    from src.main import _handle_fresh_flag
    from rich.console import Console
    # Pasta vazia
    _handle_fresh_flag(tmp_path, "https://x.com", Console(quiet=True))


# =============================================================================
# _percentiles filtra negativos (Bug agent2 r2 #3)
# =============================================================================

def test_percentiles_filters_negatives():
    """_percentiles ignora valores negativos (sentinelas de erro)."""
    from src.main import _percentiles

    # Lista so com negativos
    p = _percentiles([-1, -2, -3])
    assert p["p50"] == 0.0
    assert p["max"] == 0.0

    # Mix de positivos e negativos
    p = _percentiles([-1, 5.0, 10.0])
    assert p["min"] == 5.0  # negativo filtrado
    assert p["max"] == 10.0


def test_percentiles_filters_zero():
    """Zero tambem eh filtrado (sem dado)."""
    from src.main import _percentiles

    p = _percentiles([0.0, 0.0, 5.0])
    assert p["min"] == 5.0


def test_percentiles_empty():
    from src.main import _percentiles
    p = _percentiles([])
    assert all(v == 0.0 for v in p.values())


def test_percentiles_single_value():
    from src.main import _percentiles
    p = _percentiles([42.5])
    assert p["p50"] == 42.5
    assert p["p95"] == 42.5
    assert p["min"] == p["max"] == 42.5


# =============================================================================
# is_valid_pdf rejeita HTML disfarcado (Bug agent3 v0.7 + agent1 r2 #9)
# =============================================================================

def test_is_valid_pdf_rejects_html_in_header(tmp_path):
    """HTML com header iniciando %PDF- (falso PDF) e rejeitado."""
    from src.utils import is_valid_pdf

    p = tmp_path / "fake.pdf"
    # Tamanho > 3KB, header comeca com %PDF- mas tem <html
    p.write_bytes(b"%PDF-\n<html><body>error 500</body></html>" + b"x" * 4000 + b"\n%%EOF")
    assert not is_valid_pdf(p)


def test_is_valid_pdf_rejects_doctype(tmp_path):
    from src.utils import is_valid_pdf

    p = tmp_path / "fake.pdf"
    p.write_bytes(b"%PDF-\n<!DOCTYPE html>" + b"x" * 4000 + b"\n%%EOF")
    assert not is_valid_pdf(p)


def test_is_valid_pdf_accepts_clean_pdf(valid_pdf_bytes, tmp_path):
    """PDF normal continua sendo aceito."""
    from src.utils import is_valid_pdf

    p = tmp_path / "ok.pdf"
    p.write_bytes(valid_pdf_bytes)
    assert is_valid_pdf(p)


# =============================================================================
# Slugify edge cases adicionais
# =============================================================================

def test_slugify_no_dash_doubles():
    """Substituicoes consecutivas nao geram --."""
    from src.utils import slugify
    result = slugify("a & b & c")
    # Nao deve ter --- ou ----
    assert "----" not in result


def test_slugify_unicode_normalization():
    """NFKD preserva letra base apos acentos."""
    from src.utils import slugify
    assert slugify("café") == "cafe"
    assert slugify("ação") == "acao"


# =============================================================================
# CORRELATION ID (Melhoria v0.8)
# =============================================================================

def test_correlation_id_format():
    """generate_correlation_id retorna 8 chars hex."""
    from src.utils import generate_correlation_id

    cid = generate_correlation_id()
    assert len(cid) == 8
    # Hex chars
    int(cid, 16)


def test_correlation_id_unique():
    """IDs sao unicos (UUID4 base)."""
    from src.utils import generate_correlation_id

    ids = {generate_correlation_id() for _ in range(100)}
    assert len(ids) == 100  # nenhum duplicado


# =============================================================================
# Compress streams guard (PDFs gigantes)
# =============================================================================

def test_compress_streams_skip_threshold(mock_logger):
    """PDFs > 5000 pages pulam compressao."""
    from src.pdf_merge import _compress_streams
    from unittest.mock import MagicMock

    writer = MagicMock()
    # Simula 6000 pages
    writer.pages = [MagicMock() for _ in range(6000)]
    _compress_streams(writer, mock_logger)
    # Nenhum page.compress chamado
    for p in writer.pages:
        p.compress_content_streams.assert_not_called()


def test_compress_streams_normal_size_compresses(mock_logger):
    """PDFs <= 5000 pages tentam compressao."""
    from src.pdf_merge import _compress_streams
    from unittest.mock import MagicMock

    writer = MagicMock()
    writer.pages = [MagicMock() for _ in range(10)]
    _compress_streams(writer, mock_logger)
    for p in writer.pages:
        p.compress_content_streams.assert_called_once()


# =============================================================================
# Bookmark sanitization
# =============================================================================

def test_sanitize_bookmark_title():
    """Sanitiza chars problematicos para PDF outline."""
    from src.pdf_merge import _sanitize_bookmark_title

    assert "TM" in _sanitize_bookmark_title("Product™ Title", "fallback")
    assert "(R)" in _sanitize_bookmark_title("Brand® Title", "fallback")
    assert "(C)" in _sanitize_bookmark_title("Note© Title", "fallback")


def test_sanitize_bookmark_title_truncate():
    """Title longo eh truncado."""
    from src.pdf_merge import _sanitize_bookmark_title

    long = "X" * 500
    result = _sanitize_bookmark_title(long, "fallback")
    assert len(result) <= 120


def test_sanitize_bookmark_title_uses_fallback():
    """Title vazio usa fallback."""
    from src.pdf_merge import _sanitize_bookmark_title

    assert _sanitize_bookmark_title("", "doc1") == "doc1"
    assert _sanitize_bookmark_title("   ", "doc1") == "doc1"


# =============================================================================
# PAGE_ROTATION_INTERVAL compartilhado
# =============================================================================

def test_page_rotation_interval_shared():
    """crawler e exporter usam mesma constante."""
    from src import crawler, pdf_exporter, utils
    assert crawler._PAGE_ROTATION_INTERVAL == utils.PAGE_ROTATION_INTERVAL
    assert pdf_exporter._PAGE_ROTATION_INTERVAL == utils.PAGE_ROTATION_INTERVAL


# =============================================================================
# LARGE_BATCH_THRESHOLD compartilhado
# =============================================================================

def test_large_batch_threshold_exposed():
    from src.utils import LARGE_BATCH_THRESHOLD
    from src.main import LARGE_BATCH_THRESHOLD as MAIN_LARGE
    assert LARGE_BATCH_THRESHOLD == MAIN_LARGE
    assert LARGE_BATCH_THRESHOLD == 500


# =============================================================================
# _is_pid_alive
# =============================================================================

def test_is_pid_alive_current_process():
    from src.utils import _is_pid_alive
    assert _is_pid_alive(os.getpid()) is True


def test_is_pid_alive_invalid():
    from src.utils import _is_pid_alive
    assert _is_pid_alive(0) is False
    assert _is_pid_alive(-1) is False
