"""Fixtures compartilhadas entre todos os testes."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_output_dir():
    """Pasta temporaria isolada por test (auto-cleanup)."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def temp_pages_dir(temp_output_dir):
    """Pasta pages/ pronta dentro de temp_output_dir."""
    pages = temp_output_dir / "pages"
    pages.mkdir()
    return pages


@pytest.fixture
def mock_logger():
    """Logger silencioso para testes (nivel CRITICAL)."""
    logger = logging.getLogger("test_silent")
    logger.setLevel(logging.CRITICAL)
    logger.handlers.clear()
    return logger


@pytest.fixture
def sample_manifest_v2_data():
    """Dict v2 valido pronto para gravar em manifest.json."""
    return {
        "version": 2,
        "start_url": "https://tdn.totvs.com/display/public/PROT/Test",
        "started_at": "2026-05-25T10:00:00+00:00",
        "last_updated": "2026-05-25T10:30:00+00:00",
        "crawl_complete": False,
        "crawl_max_pages": None,
        "mapped_urls": [],
        "crawl_queue": [],
        "crawl_seen": [],
        "exported": {},
        "failures": {},
    }


@pytest.fixture
def valid_pdf_bytes():
    """Bytes minimos de um PDF valido (>= 3KB, header + EOF)."""
    return b"%PDF-1.4\n" + b"x" * 4000 + b"\n%%EOF"
