"""Testes para os fixes da v0.10.0 (rodada 3 de auditoria: 116 achados)."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from src.main import app

runner = CliRunner()


# =============================================================================
# SSRF: IPv4 em representacoes alternativas (hex/decimal/octal)
# =============================================================================

def test_ssrf_blocks_ipv4_hex():
    """Bug r3 #13: 0x7f000001 = 127.0.0.1 deve ser bloqueado."""
    from src.utils import validate_start_url, InvalidStartUrlError
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://0x7f000001/admin")


def test_ssrf_blocks_ipv4_decimal():
    """2130706433 = 127.0.0.1 (32-bit int) deve ser bloqueado."""
    from src.utils import validate_start_url, InvalidStartUrlError
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://2130706433/")


def test_ssrf_blocks_ipv4_octal():
    """017700000001 = 127.0.0.1 em octal deve ser bloqueado."""
    from src.utils import validate_start_url, InvalidStartUrlError
    # urlparse considera dígitos como host válido
    with pytest.raises(InvalidStartUrlError):
        validate_start_url("http://017700000001/")


def test_normalize_ipv4_returns_unchanged_for_normal():
    from src.utils import _normalize_ipv4_alt_repr
    assert _normalize_ipv4_alt_repr("example.com") == "example.com"
    assert _normalize_ipv4_alt_repr("192.168.1.1") == "192.168.1.1"


def test_normalize_ipv4_hex():
    from src.utils import _normalize_ipv4_alt_repr
    assert _normalize_ipv4_alt_repr("0x7f000001") == "127.0.0.1"


def test_normalize_ipv4_decimal():
    from src.utils import _normalize_ipv4_alt_repr
    assert _normalize_ipv4_alt_repr("2130706433") == "127.0.0.1"


# =============================================================================
# Jitter > 0 (nunca exato 0.0)
# =============================================================================

def test_rate_limiter_jitter_always_positive():
    """Bug r3 #12: jitter `random.uniform(0.01, 0.25)` nunca retorna 0.0."""
    from src.utils import RateLimiter, RateLimitConfig

    cfg = RateLimitConfig(base_delay_seconds=0.01, backoff_initial_seconds=1.0)

    async def run():
        lim = RateLimiter(cfg)
        cooldowns = []
        for _ in range(20):
            cd = await lim.report_block(reason="t")
            cooldowns.append(cd)
        # Cada cooldown SEMPRE > base (jitter min 1%)
        for cd, expected_base in zip(cooldowns, [1.0 * (2 ** i) for i in range(20)]):
            actual_base = min(900.0, expected_base)  # max cap
            assert cd > actual_base * 1.01, f"Cooldown {cd} <= base {actual_base}*1.01"

    asyncio.run(run())


# =============================================================================
# sanitize_proxy_for_log: mascara hostname interno
# =============================================================================

def test_sanitize_proxy_masks_internal_hostnames():
    """Bug r3 #15: hostnames .internal/.local/.lan sao mascarados."""
    from src.utils import sanitize_proxy_for_log
    assert "[REDACTED]" in sanitize_proxy_for_log("http://proxy.corp.internal:8080")
    assert "[REDACTED]" in sanitize_proxy_for_log("http://srv.local:9000")


def test_sanitize_proxy_masks_private_ips():
    from src.utils import sanitize_proxy_for_log
    sanitized = sanitize_proxy_for_log("http://10.0.0.1:8080")
    assert "10.0.0.1" not in sanitized
    assert "[REDACTED]" in sanitized


def test_sanitize_proxy_preserves_public():
    from src.utils import sanitize_proxy_for_log
    # Hostname publico preservado (sem credenciais)
    sanitized = sanitize_proxy_for_log("http://public-proxy.example.com:8080")
    assert "public-proxy.example.com" in sanitized
    assert "8080" in sanitized


def test_sanitize_proxy_creds_both_masked():
    """Username E password mascarados."""
    from src.utils import sanitize_proxy_for_log
    sanitized = sanitize_proxy_for_log("http://user:secret@public.example.com:80")
    assert "user" not in sanitized
    assert "secret" not in sanitized
    assert "public.example.com" in sanitized


# =============================================================================
# Manifest invariants check
# =============================================================================

def test_check_invariants_empty_manifest():
    """Manifest vazio nao tem inconsistencias."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        assert ck.check_invariants() == []


def test_check_invariants_detects_exported_and_failures_overlap():
    """URL em ambos exported e failures eh inconsistente."""
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.exported["url1"] = {"filename": "f.pdf", "title": "t", "size_bytes": 100, "elapsed_seconds": 1.0}
        ck.manifest.failures["url1"] = {"error": "x", "attempts": 1, "last_attempt": ""}
        issues = ck.check_invariants()
        assert any("exported" in i and "failures" in i for i in issues)


def test_check_invariants_detects_duplicates_in_mapped():
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.mapped_urls = ["url1", "url2", "url1", "url3", "url2"]
        issues = ck.check_invariants()
        assert any("duplicata" in i.lower() for i in issues)


def test_check_invariants_detects_negative_max_pages():
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.crawl_max_pages = -10
        issues = ck.check_invariants()
        assert any("negativo" in i.lower() or "max_pages" in i.lower() for i in issues)


def test_check_invariants_detects_complete_with_empty_mapped():
    from src.utils import Checkpoint
    with tempfile.TemporaryDirectory() as td:
        ck = Checkpoint(Path(td), "https://x.com/a")
        ck.manifest.crawl_complete = True
        ck.manifest.mapped_urls = []
        issues = ck.check_invariants()
        assert any("crawl_complete" in i.lower() for i in issues)


# =============================================================================
# clean-tmp: is_dir + recursivo
# =============================================================================

def test_clean_tmp_recursive(tmp_path):
    """clean-tmp pega .tmp em subdiretorios."""
    pages = tmp_path / "pages"
    sub = pages / "sub"
    sub.mkdir(parents=True)
    (pages / "root.pdf.tmp").write_bytes(b"x")
    (sub / "deep.pdf.tmp").write_bytes(b"y")
    result = runner.invoke(app, ["clean-tmp", "--output-dir", str(tmp_path), "--yes"])
    assert result.exit_code == 0
    assert not (pages / "root.pdf.tmp").exists()
    assert not (sub / "deep.pdf.tmp").exists()


def test_clean_tmp_rejects_pages_as_file(tmp_path):
    """clean-tmp falha se pages_dir for arquivo."""
    pages = tmp_path / "pages"
    pages.write_text("not a dir")
    result = runner.invoke(app, ["clean-tmp", "--output-dir", str(tmp_path), "--yes"])
    assert result.exit_code == 1


# =============================================================================
# --retry-failed-only valida manifest
# =============================================================================

def test_retry_failed_only_help_exists():
    """Flag --retry-failed-only documentada."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--retry-failed-only" in result.stdout
    assert "failures" in result.stdout.lower()


# =============================================================================
# verify --limit e empty handling
# =============================================================================

def test_verify_empty_exported(tmp_path):
    """verify em manifest sem exports avisa graciosamente."""
    (tmp_path / "manifest.json").write_text(json.dumps({
        "version": 2, "start_url": "https://x.com",
        "crawl_complete": True, "mapped_urls": ["u1"],
        "exported": {}, "failures": {},
        "crawl_queue": [], "crawl_seen": [],
    }))
    result = runner.invoke(app, ["verify", "--output-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert "Nada para verificar" in result.stdout or "sem PDFs" in result.stdout.lower()


def test_verify_help_shows_limit_flag():
    result = runner.invoke(app, ["verify", "--help"])
    assert result.exit_code == 0
    assert "--limit" in result.stdout


# =============================================================================
# api_cache bounded
# =============================================================================

def test_bounded_cache_set_caps_size():
    """Cache nao excede _API_CACHE_MAX_ENTRIES."""
    from src.crawler import _bounded_cache_set, _API_CACHE_MAX_ENTRIES
    cache = {}
    # Insere mais que o cap
    for i in range(_API_CACHE_MAX_ENTRIES + 100):
        _bounded_cache_set(cache, (f"page{i}", 0), [{"id": i}])
    assert len(cache) <= _API_CACHE_MAX_ENTRIES


def test_bounded_cache_drops_oldest():
    """Cache evicta entries antigas (FIFO)."""
    from src.crawler import _bounded_cache_set, _API_CACHE_MAX_ENTRIES
    cache = {}
    for i in range(_API_CACHE_MAX_ENTRIES):
        _bounded_cache_set(cache, (f"page{i}", 0), [{"id": i}])
    # Adiciona uma a mais
    _bounded_cache_set(cache, ("newest", 0), [{"new": True}])
    # Primeira entry foi removida
    assert ("page0", 0) not in cache
    # Mais recente esta
    assert ("newest", 0) in cache


# =============================================================================
# Slugify LRU cache + regex compiladas
# =============================================================================

def test_slugify_lru_cache_works():
    """slugify chamado 2x com mesmo arg retorna cached (mesma instancia de string)."""
    from src.utils import slugify
    # Hit cache: mesmo string retornado (id() pode ser igual em CPython)
    s1 = slugify("teste exemplo título")
    s2 = slugify("teste exemplo título")
    assert s1 == s2
    # cache_info disponivel (funcao decorada com lru_cache)
    assert hasattr(slugify, "cache_info")
    info = slugify.cache_info()
    assert info.hits >= 1


def test_slugify_uses_compiled_regex():
    """Verifica que regex pre-compiladas existem no modulo."""
    from src.utils import _SPACE_RE, _NON_ASCII_SLUG_RE, _MULTI_DASH_RE
    import re
    assert isinstance(_SPACE_RE, re.Pattern)
    assert isinstance(_NON_ASCII_SLUG_RE, re.Pattern)
    assert isinstance(_MULTI_DASH_RE, re.Pattern)


# =============================================================================
# /CreationDate em UTC no consolidated PDF
# =============================================================================

def test_pdf_set_metadata_uses_utc(tmp_path):
    """Bug r3 #12: /CreationDate deve incluir +00'00' (UTC)."""
    from src.pdf_merge import _set_metadata
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    _set_metadata(writer, "Test")
    # Salva e le metadata
    out = tmp_path / "test.pdf"
    with out.open("wb") as fp:
        writer.write(fp)
    from pypdf import PdfReader
    with out.open("rb") as fp:
        reader = PdfReader(fp)
        meta = reader.metadata
    creation = str(meta.get("/CreationDate", ""))
    # PDF date format: D:YYYYMMDDHHmmSS+00'00'
    assert "+00'00'" in creation or "+0000" in creation


# =============================================================================
# Version bump
# =============================================================================

def test_version_is_010():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.10.0" in result.stdout
