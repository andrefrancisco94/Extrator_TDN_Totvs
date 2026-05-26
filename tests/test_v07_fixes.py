"""Testes para os fixes da v0.7.0 (132 achados consolidados das auditorias)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import pytest


# =============================================================================
# CONCORRENCIA / ASYNC
# =============================================================================

def test_ratelimiter_report_success_thread_safe():
    """Bug agent #1 #12: report_success agora eh async com lock."""
    from src.utils import RateLimiter, RateLimitConfig

    cfg = RateLimitConfig(base_delay_seconds=0.01)
    lim = RateLimiter(cfg)

    async def stress():
        # 50 reports em paralelo nao devem corromper estado
        await asyncio.gather(*[lim.report_success() for _ in range(50)])
        assert lim.consecutive_blocks == 0

    asyncio.run(stress())


def test_ratelimiter_no_deadlock_after_block():
    """Bug agent #1 #1: lock nao deve segurar durante asyncio.sleep."""
    from src.utils import RateLimiter, RateLimitConfig

    cfg = RateLimitConfig(base_delay_seconds=0.01, backoff_initial_seconds=0.05)

    async def stress():
        lim = RateLimiter(cfg)
        await lim.report_block(reason="x")
        start = time.monotonic()
        # 5 waits em paralelo devem terminar em tempo finito (sem deadlock)
        await asyncio.gather(*[lim.wait() for _ in range(5)])
        return time.monotonic() - start

    elapsed = asyncio.run(stress())
    assert elapsed < 10.0  # sem deadlock


# =============================================================================
# LOGICA / EDGE CASES
# =============================================================================

def test_slugify_zero_width_chars():
    """Bug agent #2 #6: zero-width chars devem ser removidos."""
    from src.utils import slugify
    # U+200B = zero-width space
    result = slugify("texto​com‌invisivel")
    assert "​" not in result
    assert "‌" not in result


def test_slugify_rtl_markers():
    """Bug agent #2 #6: RTL markers removidos."""
    from src.utils import slugify
    # U+202E = right-to-left override
    result = slugify("texto‮rcom‬rtl")
    assert "‮" not in result
    assert "‬" not in result


def test_slugify_trademark_symbols():
    """Bug agent #2 #6: ™, ®, © com substituicao semantica."""
    from src.utils import slugify
    result = slugify("Product™ Brand® Note©")
    assert "-tm" in result
    assert "-r" in result
    assert "-c" in result


def test_slugify_at_symbol():
    """@ substituido por -at-."""
    from src.utils import slugify
    result = slugify("user@domain")
    assert "-at-" in result


def test_count_pdf_pages_returns_negative_on_error():
    """Bug agent #2 #9: count_pdf_pages deve distinguir erro de 0 paginas."""
    from src.utils import count_pdf_pages
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.pdf"
        bad.write_bytes(b"not a pdf at all")
        # Erro = -1, nao 0
        assert count_pdf_pages(bad) < 0


def test_restore_state_dedups_mapped_urls():
    """Bug agent #2 #23: manifest editado com URL duplicada nao deve gerar duplicado."""
    from src.crawler import CrawlState, _restore_state_from_checkpoint
    from src.utils import Checkpoint
    from collections import deque

    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        # Simula manifest editado manualmente com duplicata
        ck.manifest.mapped_urls = ["url1", "url2", "url1", "url3"]
        ck.manifest.crawl_queue = []
        ck.manifest.crawl_seen = ["url1", "url2"]
        state = CrawlState(queue=deque(), queued=set())
        import logging
        _restore_state_from_checkpoint(state, ck, "url1", logging.getLogger("t"))
        # ordered_urls nao deve ter duplicatas
        assert len(state.ordered_urls) == len(set(state.ordered_urls))


def test_find_pending_jobs_depth_3():
    """Bug agent #2 #8: find_pending_jobs deve cobrir 3+ niveis."""
    from src.utils import find_pending_jobs, MANIFEST_FILENAME

    with tempfile.TemporaryDirectory() as td:
        deep = Path(td) / "area" / "projeto" / "versao"
        deep.mkdir(parents=True)
        (deep / MANIFEST_FILENAME).write_text(json.dumps({
            "version": 2, "start_url": "x", "crawl_complete": False,
            "mapped_urls": ["1"], "crawl_queue": [], "crawl_seen": [],
            "exported": {}, "failures": {},
            "last_updated": "2026-01-01",
        }))
        jobs = find_pending_jobs(Path(td))
        # Deve encontrar (profundidade 3)
        assert len(jobs) == 1


def test_migration_v1_extracts_attempts():
    """Bug agent #2 #4: migracao v1 deve extrair attempts da mensagem."""
    from src.utils import Checkpoint, MANIFEST_FILENAME

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / MANIFEST_FILENAME
        p.write_text(json.dumps({
            "version": 1, "start_url": "https://x.com/a",
            "crawl_complete": True, "mapped_urls": ["a"],
            "exported": {},
            "failures": {
                "url1": "timeout apos 3 tentativas",
                "url2": "erro generico",
            },
        }))
        ck = Checkpoint(Path(td), "https://x.com/a")
        # url1 com "3 tentativas" extraido
        assert ck.manifest.failures["url1"]["attempts"] == 3
        # url2 sem numero: fallback para 1
        assert ck.manifest.failures["url2"]["attempts"] == 1


def test_reconcile_with_disk_handles_non_dir():
    """Bug agent #2 #5: reconcile_with_disk nao deve crashar se pages_dir nao eh pasta."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        # Passa um arquivo (nao pasta) - deve retornar 0 sem crashar
        bogus = Path(td) / "file.txt"
        bogus.write_text("x")
        assert ck.reconcile_with_disk(bogus) == 0


# =============================================================================
# WINDOWS + SEGURANCA
# =============================================================================

def test_validate_start_url_blocks_localhost():
    """Bug agent #4 #16: SSRF protection bloqueia localhost."""
    from src.utils import validate_start_url, InvalidStartUrlError
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://localhost/api")
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://127.0.0.1:8080/")


def test_validate_start_url_blocks_aws_metadata():
    """Bug agent #4 #16: bloqueia metadata endpoint AWS."""
    from src.utils import validate_start_url, InvalidStartUrlError
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://169.254.169.254/latest/meta-data/")


def test_validate_start_url_blocks_private_ip():
    """Bug agent #4 #16: bloqueia IPs privados."""
    from src.utils import validate_start_url, InvalidStartUrlError
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://10.0.0.1/")
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://192.168.1.1/admin")


def test_validate_start_url_allow_local():
    """allow_local=True desativa bloqueio para uso intencional."""
    from src.utils import validate_start_url
    # Nao deve levantar
    validate_start_url("http://localhost/api", allow_local=True)


def test_validate_safe_path_max_path():
    """Bug agent #4 #1: MAX_PATH no Windows."""
    from src.utils import validate_safe_path
    if os.name == "nt":
        long_path = Path("C:\\" + "a" * 300)
        with pytest.raises(ValueError):
            validate_safe_path(long_path)


def test_sanitize_proxy_for_log():
    """Bug agent #4 #5: proxy com credenciais nao deve vazar no log."""
    from src.utils import sanitize_proxy_for_log
    sanitized = sanitize_proxy_for_log("http://user:secret123@proxy:8080")
    assert "secret123" not in sanitized
    assert "user" not in sanitized
    assert "***" in sanitized
    assert "8080" in sanitized  # host:port preservado


def test_sanitize_proxy_for_log_no_creds():
    """Proxy sem credenciais retorna intacto."""
    from src.utils import sanitize_proxy_for_log
    assert sanitize_proxy_for_log("http://proxy:8080") == "http://proxy:8080"
    assert sanitize_proxy_for_log(None) == ""


def test_atomic_replace_with_retry():
    """Helper de replace com retry para Windows antivirus."""
    from src.utils import atomic_replace_with_retry
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src.txt"
        dst = Path(td) / "dst.txt"
        src.write_text("hello")
        atomic_replace_with_retry(str(src), str(dst))
        assert dst.read_text() == "hello"
        assert not src.exists()


def test_get_free_disk_bytes():
    """Reporta espaco livre em disco."""
    from src.utils import get_free_disk_bytes
    with tempfile.TemporaryDirectory() as td:
        free = get_free_disk_bytes(Path(td))
        # Deve ser positivo (algum espaco livre)
        assert free > 0


# =============================================================================
# CHECKPOINT BATCHED SAVES + BACKUP
# =============================================================================

def test_checkpoint_batched_saves():
    """Bug agent #4 #10: record_export usa batched save (nao O(N²))."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck._save_every_n = 5  # menor para testar
        # 4 records nao devem disparar save fisico (batched)
        for i in range(4):
            ck.record_export(f"url{i}", f"f{i}.pdf", "t", 100, 1.0)
        # Forca flush e checa que persiste
        ck.flush()
        ck2 = Checkpoint(Path(td), "https://x.com/a")
        assert len(ck2.manifest.exported) == 4


def test_checkpoint_flush_explicit():
    """flush() forca save imediato."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.exported["url1"] = {
            "filename": "f.pdf", "title": "t",
            "size_bytes": 1, "elapsed_seconds": 1.0,
        }
        ck.flush()
        ck2 = Checkpoint(Path(td), "https://x.com/a")
        assert "url1" in ck2.manifest.exported


def test_find_orphan_pdfs_protects_special_names():
    """Bug agent #2 #14: clean-orphans nao deve deletar nomes especiais."""
    from src.utils import Checkpoint, MANIFEST_FILENAME
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        pages = Path(td) / "pages"
        pages.mkdir()
        # Cria PDF com nome reservado e PDF .tmp em geracao
        (pages / MANIFEST_FILENAME).write_bytes(b"%PDF-1.4\n" + b"x"*4000 + b"\n%%EOF")
        (pages / "real.pdf.tmp").write_bytes(b"%PDF-1.4\n" + b"x"*4000 + b"\n%%EOF")
        (pages / "real-orphan.pdf").write_bytes(b"%PDF-1.4\n" + b"x"*4000 + b"\n%%EOF")
        orphans = ck.find_orphan_pdfs(pages)
        names = [p.name for p in orphans]
        # Apenas o orfao real
        assert "real-orphan.pdf" in names
        assert MANIFEST_FILENAME not in names
        assert "real.pdf.tmp" not in names


def test_expand_urls_with_retries_no_duplicates_with_exported():
    """Bug agent #2 #2: URLs ja exportadas nao devem ser re-tentadas via failures."""
    from src.pdf_exporter import _expand_urls_with_retries
    from src.utils import Checkpoint
    import logging
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        # url1 esta em failures E em exported (caso bizarro mas possivel)
        ck.manifest.failures["url1"] = {
            "error": "old timeout", "attempts": 2, "last_attempt": "",
        }
        ck.manifest.exported["url1"] = {
            "filename": "f.pdf", "title": "t",
            "size_bytes": 100, "elapsed_seconds": 1.0,
        }
        urls = ["url2"]
        result = _expand_urls_with_retries(urls, ck, logging.getLogger("t"))
        # url1 ja exportada NAO deve ser re-tentada
        assert "url1" not in result
        assert "url2" in result
