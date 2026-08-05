"""Testes unitarios das utilidades (sem dependencia de Playwright/rede)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from src.utils import (
    MANIFEST_FILENAME,
    MAX_RETRY_ATTEMPTS,
    Checkpoint,
    Manifest,
    RateLimitConfig,
    RateLimiter,
    build_browser_context_args,
    build_pdf_file_name,
    canonicalize_url,
    count_pdf_pages,
    find_pending_jobs,
    format_bytes,
    format_duration,
    is_cloudflare_challenge,
    is_blocking_error,
    is_valid_pdf,
    parse_proxy_arg,
    parse_scope,
    pick_user_agent,
    pick_viewport,
    slugify,
    storage_state_path,
    validate_start_url,
)


def test_slugify_preserva_acentos_basicos():
    assert slugify("Configurações de Sistema") == "configuracoes-de-sistema"


def test_slugify_simbolos_semanticos():
    assert "plus-plus" in slugify("Setup C++ no Windows")
    assert "sharp" in slugify("Aprenda C# rapido")
    assert "-and-" in slugify("Foo & Bar")


def test_slugify_chines_fallback_hash():
    # Chinês não normaliza para ASCII — deve cair no fallback de hash
    result = slugify("中文页面")
    assert result.startswith("pagina-")
    assert len(result) == len("pagina-") + 8


def test_slugify_max_len():
    long = "a" * 200
    assert len(slugify(long)) <= 80


def test_slugify_windows_reserved():
    assert slugify("con") == "con-page"
    assert slugify("NUL") == "nul-page"


def test_slugify_string_vazia():
    assert slugify("").startswith("pagina")


def test_canonicalize_url_remove_fragment():
    assert canonicalize_url("https://x.com/y#section") == "https://x.com/y"


def test_canonicalize_url_normaliza_query():
    # Mantém apenas pageId/spaceKey/title
    url = "https://x.com/y?pageId=123&foo=bar&spaceKey=PROT"
    canonical = canonicalize_url(url)
    assert "pageId=123" in canonical
    assert "spaceKey=PROT" in canonical
    assert "foo=bar" not in canonical


def test_validate_start_url_rejects_invalid():
    with pytest.raises(Exception):
        validate_start_url("ftp://x.com")
    with pytest.raises(Exception):
        validate_start_url("")


def test_validate_start_url_aceita_http():
    assert validate_start_url("https://x.com/y").startswith("https://")


def test_parse_scope_display_public():
    scope = parse_scope("https://tdn.totvs.com/display/public/PROT/Page")
    assert scope.domain == "tdn.totvs.com"
    assert scope.space_key == "PROT"
    assert "/display/public/PROT/" in scope.path_prefix


def test_format_bytes():
    assert format_bytes(500) == "500.0 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"


def test_format_duration():
    assert format_duration(45.5) == "45.5s"
    assert "min" in format_duration(120)
    assert "h" in format_duration(3700)


def test_build_pdf_file_name_unique():
    name1 = build_pdf_file_name(1, "Pagina A", set())
    assert name1 == "0001-pagina-a.pdf"
    name2 = build_pdf_file_name(1, "Pagina A", {name1})
    assert name2 == "0001-pagina-a-2.pdf"


def test_is_blocking_error_detecta_codes():
    assert is_blocking_error(Exception("HTTP 522 from server"))
    assert is_blocking_error(Exception("net::ERR_TIMED_OUT"))
    assert is_blocking_error(Exception("429 Too Many Requests"))
    assert not is_blocking_error(Exception("File not found"))


def test_is_blocking_error_ignora_digitos_embutidos_em_ids():
    """Regressao: um ID/timestamp maior que contem '503' como substring
    nao deve disparar falso positivo (ex: pageId ou cookie do GA)."""
    assert not is_blocking_error(Exception("pageId=1785936503 nao encontrado"))
    assert not is_blocking_error(Exception("timeout apos 5039 tentativas"))


def test_is_blocking_error_ignora_call_log_echoado_pelo_playwright():
    """Regressao real: erro local (cert TLS) tinha '503' embutido no cookie
    ecoado pelo 'Call log' do Playwright, e era classificado como bloqueio."""
    msg = (
        "APIRequestContext.get: self-signed certificate in certificate chain\n"
        "Call log:\n"
        "  - -> GET https://tdn.totvs.com/rest/api/content/1/child/page\n"
        "    - cookie: _ga_8RWQ11H2P1=GS2.1.s1785936468$o1$g1$t1785936503$j25$l0$h0"
    )
    assert not is_blocking_error(Exception(msg))


def test_is_blocking_error_detecta_codigo_real_mesmo_com_call_log():
    """Se o codigo de bloqueio estiver na headline (antes do Call log), ainda detecta."""
    msg = (
        "Request failed with status 503\n"
        "Call log:\n"
        "  - -> GET https://tdn.totvs.com/rest/api/content/1/child/page"
    )
    assert is_blocking_error(Exception(msg))


def test_is_cloudflare_challenge_detecta():
    assert is_cloudflare_challenge("checking your browser before access")
    assert is_cloudflare_challenge("page contains ray id: abc123 marker")
    assert is_cloudflare_challenge("<div>cf-browser-verification</div>")
    assert not is_cloudflare_challenge("a normal page about cars")


def test_pick_user_agent_returns_chromium_or_firefox():
    ua = pick_user_agent()
    assert "Mozilla" in ua


def test_pick_viewport_has_width_height():
    vp = pick_viewport()
    assert "width" in vp and "height" in vp
    assert vp["width"] > 0 and vp["height"] > 0


def test_pick_user_agent_variando():
    uas = {pick_user_agent() for _ in range(100)}
    assert len(uas) >= 2


def test_parse_proxy_arg_http():
    result = parse_proxy_arg("http://localhost:8080")
    assert result is not None
    assert result["server"] == "http://localhost:8080"


def test_parse_proxy_arg_with_auth():
    result = parse_proxy_arg("http://user:pass@host:80")
    assert result is not None
    assert result["username"] == "user"
    assert result["password"] == "pass"


def test_parse_proxy_arg_none():
    import os as _os
    saved = _os.environ.pop("HTTP_PROXY", None)
    saved2 = _os.environ.pop("HTTPS_PROXY", None)
    try:
        assert parse_proxy_arg(None) is None
    finally:
        if saved:
            _os.environ["HTTP_PROXY"] = saved
        if saved2:
            _os.environ["HTTPS_PROXY"] = saved2


def test_storage_state_path():
    p = storage_state_path(Path("/tmp/myjob"))
    assert p.name == "browser_state.json"
    assert p.parent.name == "myjob"


def test_build_browser_context_args_sem_state():
    args = build_browser_context_args()
    assert "user_agent" in args
    assert "viewport" in args
    assert args["locale"] == "pt-BR"


# Testes do RateLimiter

def test_rate_limiter_no_deadlock_with_concurrent_waits():
    cfg = RateLimitConfig(base_delay_seconds=0.01, backoff_initial_seconds=0.1)
    lim = RateLimiter(cfg)

    async def run():
        await lim.report_block(reason="teste")
        start = time.monotonic()
        await asyncio.gather(lim.wait(), lim.wait(), lim.wait())
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    # Termina em tempo finito; sem deadlock global (que seria infinito)
    assert elapsed < 5.0


def test_rate_limiter_report_success_resets_blocks():
    cfg = RateLimitConfig(base_delay_seconds=0.01)
    lim = RateLimiter(cfg)

    async def run():
        await lim.report_block(reason="x")
        assert lim.consecutive_blocks == 1
        await lim.report_success()  # agora async com lock
        assert lim.consecutive_blocks == 0

    asyncio.run(run())


# Testes do Checkpoint / Manifest

def test_manifest_defaults_v2():
    m = Manifest()
    assert m.version == 2
    assert m.crawl_queue == []
    assert m.crawl_seen == []
    assert m.failures == {}


def test_checkpoint_save_and_reload():
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.mapped_urls = ["a", "b", "c"]
        ck.save()
        ck2 = Checkpoint(Path(td), "https://x.com/a")
        assert ck2.manifest.mapped_urls == ["a", "b", "c"]


def test_checkpoint_migration_v1_to_v2():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / MANIFEST_FILENAME
        p.write_text(json.dumps({
            "version": 1, "start_url": "https://x.com/a",
            "crawl_complete": True, "mapped_urls": ["a"],
            "exported": {}, "failures": {"b": "erro v1"},
        }))
        ck = Checkpoint(Path(td), "https://x.com/a")
        assert ck.manifest.version == 2
        assert isinstance(ck.manifest.failures["b"], dict)
        assert ck.manifest.failures["b"]["error"] == "erro v1"


def test_checkpoint_save_threadsafe():
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        errors = []
        def w(n):
            try:
                for i in range(20):
                    ck.manifest.exported[f"u{n}_{i}"] = {
                        "filename": "f.pdf", "title": "t",
                        "size_bytes": 10, "elapsed_seconds": 1.0,
                    }
                    ck.save()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=w, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors
        ck2 = Checkpoint(Path(td), "https://x.com/a")
        assert len(ck2.manifest.exported) == 80


def test_checkpoint_record_failure_increments_attempts():
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.record_failure("url1", "timeout")
        assert ck.failure_attempts("url1") == 1
        ck.record_failure("url1", "timeout again")
        assert ck.failure_attempts("url1") == 2


def test_checkpoint_urls_pending_retry():
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.record_failure("url1", "x")
        ck.record_failure("url2", "y")
        ck.record_failure("url2", "y2")
        pending = ck.urls_pending_retry(MAX_RETRY_ATTEMPTS)
        assert "url1" in pending
        assert "url2" in pending
        # Esgotar tentativas
        for _ in range(MAX_RETRY_ATTEMPTS + 1):
            ck.record_failure("url1", "x")
        pending = ck.urls_pending_retry(MAX_RETRY_ATTEMPTS)
        assert "url1" not in pending


def test_checkpoint_backup_creates_file():
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.mapped_urls = ["a"]
        ck.save()
        backup = ck.backup()
        assert backup is not None
        assert backup.exists()


def test_find_pending_jobs_empty():
    with tempfile.TemporaryDirectory() as td:
        assert find_pending_jobs(Path(td)) == []


def test_find_pending_jobs_detects_incomplete():
    with tempfile.TemporaryDirectory() as td:
        sub = Path(td) / "projA"
        sub.mkdir()
        (sub / MANIFEST_FILENAME).write_text(json.dumps({
            "version": 2, "start_url": "x",
            "crawl_complete": False,
            "mapped_urls": ["1", "2"],
            "crawl_queue": ["3"],
            "crawl_seen": ["1"],
            "exported": {}, "failures": {},
            "last_updated": "2026-01-01",
        }))
        jobs = find_pending_jobs(Path(td))
        assert len(jobs) == 1
        assert jobs[0].queue_count == 1
        assert jobs[0].is_crawl_pending


def test_is_valid_pdf_rejects_small_file():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "tiny.pdf"
        p.write_bytes(b"%PDF-1.4\n%%EOF")  # < 3KB
        assert not is_valid_pdf(p)


def test_is_valid_pdf_rejects_no_header():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "fake.pdf"
        p.write_bytes(b"X" * 5000)
        assert not is_valid_pdf(p)


def test_count_pdf_pages_invalid_returns_negative():
    """count_pdf_pages retorna -1 em erro, 0+ se valido (distingue casos)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "fake.pdf"
        p.write_bytes(b"not a pdf")
        # Valor < 0 indica erro (-1); >= 0 seria PDF valido
        assert count_pdf_pages(p) < 0


def test_reconcile_with_disk_removes_missing():
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        pages = Path(td) / "pages"
        pages.mkdir()
        # Registra entrada mas nao cria arquivo
        ck.manifest.exported["url1"] = {
            "filename": "ghost.pdf", "title": "t",
            "size_bytes": 10, "elapsed_seconds": 1.0,
        }
        ck.save()
        removed = ck.reconcile_with_disk(pages)
        assert removed == 1
        assert "url1" not in ck.manifest.exported


def test_find_orphan_pdfs():
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        pages = Path(td) / "pages"
        pages.mkdir()
        # PDF orfao (sem entrada no manifest)
        (pages / "orphan.pdf").write_bytes(b"%PDF-1.4\nxxx\n%%EOF")
        orphans = ck.find_orphan_pdfs(pages)
        assert len(orphans) == 1
        assert orphans[0].name == "orphan.pdf"
