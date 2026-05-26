"""Testes da CLI usando typer CliRunner (sem executar Playwright)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from src.main import app

runner = CliRunner()


def test_version_command():
    """`version` retorna a versao."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0." in result.stdout  # 0.x.x


def test_help_command():
    """`--help` lista comandos."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ["run", "version", "jobs", "failures", "reset", "clean-orphans", "report"]:
        assert cmd in result.stdout


def test_run_help_shows_new_flags():
    """`run --help` mostra todas as novas flags."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    for flag in [
        "--request-delay", "--max-workers", "--backoff-initial",
        "--backoff-max", "--proxy", "--dry-run",
    ]:
        assert flag in result.stdout


def test_jobs_command_empty_dir():
    """`jobs` em pasta vazia."""
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(app, ["jobs", "--output-dir", td])
        assert result.exit_code == 0
        assert "Nenhum job" in result.stdout


def test_jobs_command_json_output():
    """`jobs --json` retorna JSON parseavel."""
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(app, ["jobs", "--output-dir", td, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)


def test_jobs_command_detects_pending():
    """`jobs` detecta job incompleto."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sub = root / "projetoA"
        sub.mkdir()
        (sub / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com",
            "crawl_complete": False,
            "mapped_urls": ["a", "b"], "crawl_queue": ["c"],
            "crawl_seen": ["a"], "exported": {}, "failures": {},
            "last_updated": "2026-01-01",
        }))
        result = runner.invoke(app, ["jobs", "--output-dir", str(root)])
        assert result.exit_code == 0
        assert "projetoA" in result.stdout


def test_failures_command_no_manifest():
    """`failures` em pasta sem manifest."""
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(app, ["failures", "--output-dir", td])
        assert result.exit_code == 1


def test_failures_command_json_output():
    """`failures --json` retorna lista de URLs."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com", "crawl_complete": True,
            "mapped_urls": [], "exported": {},
            "failures": {
                "url1": {"error": "timeout", "attempts": 2, "last_attempt": "2026-01-01"},
            },
            "crawl_queue": [], "crawl_seen": [],
        }))
        result = runner.invoke(app, ["failures", "--output-dir", td, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert len(data) == 1
        assert data[0]["url"] == "url1"
        assert data[0]["attempts"] == 2


def test_reset_command_empty_dir():
    """`reset` em pasta vazia retorna 'nada para resetar'."""
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(app, ["reset", td, "--yes"])
        assert result.exit_code == 0
        assert "Nada para" in result.stdout or "Reset" in result.stdout


def test_reset_command_soft():
    """`reset` soft remove manifest mas mantem pages/."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest = root / "manifest.json"
        pages = root / "pages"
        pages.mkdir()
        (pages / "doc.pdf").write_bytes(b"%PDF-1.4\n" + b"x"*4000 + b"\n%%EOF")
        manifest.write_text(json.dumps({
            "version": 2, "start_url": "https://x.com",
            "crawl_complete": True, "mapped_urls": [],
            "exported": {}, "failures": {},
            "crawl_queue": [], "crawl_seen": [],
        }))
        result = runner.invoke(app, ["reset", str(root), "--yes"])
        assert result.exit_code == 0
        assert not manifest.exists()
        assert (pages / "doc.pdf").exists()  # PDFs preservados


def test_reset_command_hard():
    """`reset --hard` remove manifest E pages/."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        manifest = root / "manifest.json"
        pages = root / "pages"
        pages.mkdir()
        (pages / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        manifest.write_text("{}")

        result = runner.invoke(app, ["reset", str(root), "--hard", "--yes"])
        assert result.exit_code == 0
        assert not manifest.exists()
        assert not pages.exists()


def test_reset_command_rejects_file():
    """Bug agent #2 #13: reset com arquivo (nao pasta) deve falhar limpo."""
    with tempfile.TemporaryDirectory() as td:
        file_path = Path(td) / "not_a_dir.txt"
        file_path.write_text("x")
        result = runner.invoke(app, ["reset", str(file_path), "--yes"])
        assert result.exit_code != 0


def test_clean_orphans_command_no_manifest():
    """`clean-orphans` sem manifest deve falhar."""
    with tempfile.TemporaryDirectory() as td:
        result = runner.invoke(app, ["clean-orphans", "--output-dir", td])
        assert result.exit_code == 1


def test_clean_orphans_command_removes_orphans():
    """`clean-orphans` remove PDFs orfaos."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pages = root / "pages"
        pages.mkdir()
        # PDF orfao (nao no manifest)
        (pages / "orphan.pdf").write_bytes(b"%PDF-1.4\n" + b"x"*4000 + b"\n%%EOF")
        (root / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com",
            "crawl_complete": True, "mapped_urls": [],
            "exported": {}, "failures": {},
            "crawl_queue": [], "crawl_seen": [],
        }))
        result = runner.invoke(app, ["clean-orphans", "--output-dir", str(root), "--yes"])
        assert result.exit_code == 0
        assert not (pages / "orphan.pdf").exists()


def test_report_command_csv():
    """`report --format csv` gera CSV."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com",
            "crawl_complete": True,
            "mapped_urls": ["u1", "u2"],
            "exported": {"u1": {"filename": "a.pdf", "title": "A", "size_bytes": 100, "elapsed_seconds": 1.0}},
            "failures": {"u2": {"error": "x", "attempts": 1, "last_attempt": ""}},
            "crawl_queue": [], "crawl_seen": [],
        }))
        result = runner.invoke(app, ["report", "--output-dir", str(root), "--format", "csv"])
        assert result.exit_code == 0
        report = root / "report.csv"
        assert report.exists()
        content = report.read_text(encoding="utf-8")
        assert "u1" in content
        assert "u2" in content


def test_report_command_json():
    """`report --format json` gera JSON com summary."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com",
            "crawl_complete": True,
            "mapped_urls": ["u1"],
            "exported": {"u1": {"filename": "a.pdf", "title": "A", "size_bytes": 100, "elapsed_seconds": 1.0}},
            "failures": {}, "crawl_queue": [], "crawl_seen": [],
        }))
        result = runner.invoke(app, ["report", "--output-dir", str(root), "--format", "json"])
        assert result.exit_code == 0
        report = root / "report.json"
        assert report.exists()
        data = json.loads(report.read_text(encoding="utf-8"))
        assert "summary" in data
        assert data["summary"]["total"] == 1
        assert data["summary"]["exported"] == 1


def test_report_command_invalid_format():
    """Format invalido deve falhar."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "x", "crawl_complete": True,
            "mapped_urls": [], "exported": {}, "failures": {},
            "crawl_queue": [], "crawl_seen": [],
        }))
        result = runner.invoke(app, ["report", "--output-dir", td, "--format", "xml"])
        assert result.exit_code != 0


def test_csv_report_escapes_commas_in_urls():
    """Bug agent #2 #15: URLs com virgulas/aspas escapadas corretamente."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        url_with_comma = "https://x.com/page?title=foo,bar"
        (root / "manifest.json").write_text(json.dumps({
            "version": 2, "start_url": "https://x.com",
            "crawl_complete": True,
            "mapped_urls": [url_with_comma],
            "exported": {url_with_comma: {
                "filename": "a.pdf", "title": "Titulo, com virgula",
                "size_bytes": 100, "elapsed_seconds": 1.0,
            }},
            "failures": {}, "crawl_queue": [], "crawl_seen": [],
        }))
        result = runner.invoke(app, ["report", "--output-dir", str(root), "--format", "csv"])
        assert result.exit_code == 0
        # Le com csv module para garantir parsing correto
        import csv
        with (root / "report.csv").open("r", encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["url"] == url_with_comma
        assert rows[0]["title"] == "Titulo, com virgula"
