from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from .crawler import crawl_confluence_tree
from .pdf_exporter import export_pages_to_pdf
from .pdf_merge import merge_pdfs
from .utils import (
    Checkpoint,
    InvalidStartUrlError,
    ensure_output_dirs,
    format_bytes,
    format_duration,
    get_console,
    setup_logger,
    validate_start_url,
    write_slow_pages_log,
)

__version__ = "0.5.0"

LARGE_BATCH_THRESHOLD = 500
MIN_DISK_FREE_BYTES = 500 * 1024 * 1024  # 500MB minimo razoavel

app = typer.Typer(add_completion=False, no_args_is_help=True)


async def run_pipeline(
    start_url: str,
    output_dir: Path,
    consolidated_name: str,
    headless: bool,
    timeout_seconds: int,
    max_pages: int | None,
    slow_threshold_seconds: float,
    auto_confirm: bool,
    force_recrawl: bool,
    force_reexport: bool,
) -> int:
    _, log_file = ensure_output_dirs(output_dir)
    logger = setup_logger(log_file)
    console = get_console()

    timeout_ms = timeout_seconds * 1000
    pipeline_start = time.monotonic()

    # Checkpoint emite warning no logger se encontrar manifest invalido/corrompido.
    checkpoint = Checkpoint(output_dir, start_url, logger=logger)
    resume_active = bool(
        checkpoint.manifest.crawl_complete and checkpoint.manifest.mapped_urls
    )
    resume_pdfs = len(checkpoint.manifest.exported)

    # Se max_pages mudou desde o run anterior, force re-crawl para evitar
    # reusar uma lista cropped quando o usuario agora quer mais paginas.
    if (
        not force_recrawl
        and checkpoint.manifest.crawl_complete
        and checkpoint.manifest.crawl_max_pages != max_pages
    ):
        logger.warning(
            "max_pages mudou (era %s, agora %s) — forcando re-crawl.",
            checkpoint.manifest.crawl_max_pages, max_pages,
        )
        force_recrawl = True

    _print_intro_panel(
        console, start_url, output_dir, timeout_seconds,
        slow_threshold_seconds, headless, max_pages,
        resume_active, resume_pdfs,
    )

    logger.info("URL inicial: %s", start_url)
    logger.info("Diretorio de saida: %s", output_dir)
    if resume_active:
        logger.info(
            "Resume detectado: %d URLs mapeadas, %d PDFs ja exportados",
            len(checkpoint.manifest.mapped_urls), resume_pdfs,
        )

    # Captura estado anterior do crawl ANTES de re-executar (para computar diff)
    previous_mapped = set(checkpoint.manifest.mapped_urls)

    try:
        urls, slow_crawl = await crawl_confluence_tree(
            start_url=start_url,
            checkpoint=checkpoint,
            logger=logger,
            headless=headless,
            timeout_ms=timeout_ms,
            max_pages=max_pages,
            slow_threshold_seconds=slow_threshold_seconds,
            force_recrawl=force_recrawl,
        )
    except KeyboardInterrupt:
        logger.warning("Crawl interrompido pelo usuario (Ctrl+C).")
        raise

    if not urls:
        logger.error("Nenhuma pagina foi encontrada a partir da URL inicial.")
        return 1

    # Diff de URLs (so relevante em modo update/regenerate)
    current_set = set(urls)
    added_urls = sorted(current_set - previous_mapped) if previous_mapped else []
    removed_urls = sorted(previous_mapped - current_set) if previous_mapped else []
    if force_recrawl and previous_mapped:
        logger.info(
            "Diff do re-crawl: %d novas, %d removidas, %d inalteradas.",
            len(added_urls), len(removed_urls),
            len(current_set & previous_mapped),
        )

    # Confirmacao para batches grandes (apenas em TTY)
    if not _confirm_large_batch(urls, auto_confirm, logger, console):
        logger.info("Usuario cancelou antes da exportacao.")
        return 0

    try:
        pdf_entries, failures, slow_pdf = await export_pages_to_pdf(
            urls=urls,
            output_dir=output_dir,
            checkpoint=checkpoint,
            logger=logger,
            headless=headless,
            timeout_ms=timeout_ms,
            slow_threshold_seconds=slow_threshold_seconds,
            force_reexport=force_reexport,
        )
    except KeyboardInterrupt:
        logger.warning("Exportacao de PDFs interrompida pelo usuario (Ctrl+C).")
        raise

    if not pdf_entries:
        logger.error("Nenhum PDF individual foi gerado.")
        return 1

    consolidated_path: Path | None = output_dir / consolidated_name
    try:
        merge_pdfs(pdf_entries, consolidated_path, logger)
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao consolidar PDFs")
        consolidated_path = None

    all_slow = slow_crawl + slow_pdf
    slow_log_path = _maybe_write_slow_log(all_slow, output_dir, logger)

    total_elapsed = time.monotonic() - pipeline_start
    logger.info(
        "Resumo: %s URL(s), %s PDF(s), %s falha(s), %s lenta(s), tempo total %s",
        len(urls), len(pdf_entries), len(failures), len(all_slow),
        format_duration(total_elapsed),
    )

    _print_summary_tables(
        console=console,
        urls=urls, pdf_entries=pdf_entries, failures=failures, all_slow=all_slow,
        slow_threshold_seconds=slow_threshold_seconds,
        consolidated_path=consolidated_path, log_file=log_file,
        slow_log_path=slow_log_path, manifest_path=checkpoint.path,
        total_elapsed=total_elapsed,
        added_urls=added_urls, removed_urls=removed_urls,
    )

    return 0 if consolidated_path else 2


def _print_intro_panel(
    console,
    start_url: str,
    output_dir: Path,
    timeout_seconds: int,
    slow_threshold_seconds: float,
    headless: bool,
    max_pages: int | None,
    resume_active: bool,
    resume_pdfs: int,
) -> None:
    resume_line = ""
    if resume_active:
        resume_line = f"\n[bold yellow]Resume:[/bold yellow] {resume_pdfs} PDFs ja existem"
    console.print(
        Panel.fit(
            f"[bold]URL inicial:[/bold] {start_url}\n"
            f"[bold]Saida:[/bold] {output_dir}\n"
            f"[bold]Timeout:[/bold] {timeout_seconds}s | "
            f"[bold]Slow:[/bold] >={slow_threshold_seconds:.0f}s | "
            f"[bold]Headless:[/bold] {headless} | "
            f"[bold]Max:[/bold] {max_pages or 'sem limite'}"
            f"{resume_line}",
            title="Extrator TDN TOTVS",
            border_style="cyan",
        )
    )


def _confirm_large_batch(
    urls: list[str], auto_confirm: bool, logger, console,
) -> bool:
    if len(urls) <= LARGE_BATCH_THRESHOLD or auto_confirm:
        return True

    estimate_min = (len(urls) * 30) / 60  # ~30s por pagina como estimativa
    if not sys.stdin.isatty():
        logger.info(
            "Batch grande (%d URLs ~ %dmin) sem TTY, prosseguindo automaticamente.",
            len(urls), int(estimate_min),
        )
        return True

    console.print(
        f"\n[bold yellow]Atencao:[/bold yellow] {len(urls)} paginas mapeadas. "
        f"Exportacao pode levar ~{int(estimate_min)} minutos.\n"
        "Use [bold]--yes[/bold] no CLI para pular esta confirmacao.\n"
    )
    return typer.confirm("Prosseguir com a exportacao?", default=True)


def _maybe_write_slow_log(all_slow, output_dir: Path, logger) -> Path | None:
    if not all_slow:
        return None
    try:
        path = write_slow_pages_log(output_dir, all_slow)
        logger.info("Log de paginas lentas: %s (%s registros)", path, len(all_slow))
        return path
    except OSError as exc:
        logger.warning("Falha ao escrever slow_pages.log: %s", exc)
        return None


def _print_summary_tables(
    *,
    console,
    urls,
    pdf_entries,
    failures,
    all_slow,
    slow_threshold_seconds: float,
    consolidated_path: Path | None,
    log_file: Path,
    slow_log_path: Path | None,
    manifest_path: Path,
    total_elapsed: float,
    added_urls: list[str] | None = None,
    removed_urls: list[str] | None = None,
) -> None:
    main_table = _build_main_summary_table(
        urls=urls, pdf_entries=pdf_entries, failures=failures, all_slow=all_slow,
        slow_threshold_seconds=slow_threshold_seconds,
        consolidated_path=consolidated_path, log_file=log_file,
        slow_log_path=slow_log_path, manifest_path=manifest_path,
        total_elapsed=total_elapsed,
        added_urls=added_urls, removed_urls=removed_urls,
    )
    console.print(main_table)

    if added_urls:
        added_table = Table(
            title=f"URLs novas detectadas no re-crawl ({len(added_urls)})",
            border_style="green",
        )
        added_table.add_column("URL", overflow="fold")
        for url in added_urls:
            added_table.add_row(url)
        console.print(added_table)

    if removed_urls:
        removed_table = Table(
            title=f"URLs removidas da TDN ({len(removed_urls)}) — PDFs preservados em pages/",
            border_style="bright_black",
        )
        removed_table.add_column("URL", overflow="fold")
        for url in removed_urls:
            removed_table.add_row(url)
        console.print(removed_table)

    if all_slow:
        slow_table = Table(
            title=f"Paginas lentas (>= {slow_threshold_seconds:.0f}s)",
            border_style="yellow",
        )
        slow_table.add_column("Tempo", justify="right", style="bold yellow")
        slow_table.add_column("Fase")
        slow_table.add_column("URL", overflow="fold")
        for r in sorted(all_slow, key=lambda x: x.elapsed_seconds, reverse=True):
            slow_table.add_row(f"{r.elapsed_seconds:.1f}s", r.phase, r.url)
        console.print(slow_table)

    if failures:
        fail_table = Table(title="Falhas registradas", border_style="red")
        fail_table.add_column("URL", overflow="fold")
        fail_table.add_column("Erro", overflow="fold")
        for url, error in failures:
            fail_table.add_row(url, error)
        console.print(fail_table)


def _build_main_summary_table(
    *,
    urls,
    pdf_entries,
    failures,
    all_slow,
    slow_threshold_seconds: float,
    consolidated_path: Path | None,
    log_file: Path,
    slow_log_path: Path | None,
    manifest_path: Path,
    total_elapsed: float,
    added_urls: list[str] | None,
    removed_urls: list[str] | None,
) -> Table:
    total_bytes = 0
    for path, _ in pdf_entries:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            continue

    consolidated_bytes = 0
    if consolidated_path and consolidated_path.exists():
        try:
            consolidated_bytes = consolidated_path.stat().st_size
        except OSError:
            pass

    avg_per_page = total_elapsed / len(pdf_entries) if pdf_entries else 0

    table = Table(title="Resumo da execucao", border_style="green", show_header=False)
    table.add_column("Metrica", style="bold cyan")
    table.add_column("Valor")
    table.add_row("URLs mapeadas", str(len(urls)))
    if added_urls is not None and (added_urls or removed_urls):
        table.add_row("URLs novas", f"[green]+{len(added_urls)}[/green]")
        table.add_row("URLs removidas", f"[bright_black]-{len(removed_urls or [])}[/bright_black]")
    table.add_row("PDFs individuais", str(len(pdf_entries)))
    table.add_row("Falhas", str(len(failures)))
    table.add_row("Paginas lentas", f"{len(all_slow)} (>= {slow_threshold_seconds:.0f}s)")
    table.add_row("Tempo total", format_duration(total_elapsed))
    table.add_row("Media por pagina", format_duration(avg_per_page))
    table.add_row("Tamanho total PDFs", format_bytes(total_bytes))
    if consolidated_bytes:
        table.add_row("Tamanho consolidado", format_bytes(consolidated_bytes))
    table.add_row(
        "Consolidado",
        str(consolidated_path) if consolidated_path else "[red]nao gerado[/red]",
    )
    table.add_row("Log execucao", str(log_file))
    table.add_row("Manifest (resume)", str(manifest_path))
    if slow_log_path:
        table.add_row("Log paginas lentas", str(slow_log_path))
    return table


def _check_disk_space(output_dir: Path, console) -> bool:
    try:
        usage = shutil.disk_usage(str(output_dir))
    except OSError:
        return True  # nao consegue checar, prossegue
    if usage.free < MIN_DISK_FREE_BYTES:
        console.print(
            f"[bold yellow]Aviso:[/bold yellow] apenas "
            f"{format_bytes(usage.free)} livres em {output_dir}. "
            f"Recomendado pelo menos {format_bytes(MIN_DISK_FREE_BYTES)}."
        )
        if sys.stdin.isatty():
            return typer.confirm("Continuar mesmo assim?", default=False)
    return True


@app.command()
def run(
    start_url: str = typer.Argument(..., help="URL inicial da documentacao Confluence/TDN."),
    output_dir: Path = typer.Option(Path("output"), help="Pasta de saida para PDFs e log."),
    consolidated_name: str = typer.Option(
        "TDN_TOTVS_consolidado.pdf",
        help="Nome do PDF final consolidado.",
    ),
    headless: bool = typer.Option(True, "--headless/--headed", help="Executa navegador sem interface."),
    timeout_seconds: int = typer.Option(
        120, min=10, max=1800,
        help="Limite total por pagina (segundos). Default 120s = 2min.",
    ),
    slow_threshold_seconds: float = typer.Option(
        60.0, min=5.0,
        help="Paginas que demorarem >= este valor sao registradas em slow_pages.log.",
    ),
    max_pages: int | None = typer.Option(None, min=1, max=100_000, help="Limite de paginas."),
    yes: bool = typer.Option(False, "--yes/--no-yes", "-y", help="Pula confirmacoes interativas."),
    fresh: bool = typer.Option(False, "--fresh", help="Ignora manifest.json e comeca do zero."),
    update: bool = typer.Option(
        False, "--update",
        help="Re-mapeia a arvore para detectar paginas novas (reusa PDFs existentes).",
    ),
    regenerate: bool = typer.Option(
        False, "--regenerate",
        help="Re-mapeia + regenera TODOS os PDFs (mantem manifest para historico).",
    ),
) -> None:
    """Mapeia a arvore lateral do Confluence, exporta PDFs e gera consolidado.

    Modos:
      (default)      Resume automatico: skip crawl se ja feito, skip PDFs que existem.
      --update       Re-mapeia para achar paginas novas; PDFs existentes reusados.
      --regenerate   Re-mapeia + regenera TODOS os PDFs (forca refresh de conteudo).
      --fresh        Ignora manifest.json e comeca do zero.
    """
    console = get_console()

    try:
        validated_url = validate_start_url(start_url)
    except InvalidStartUrlError as exc:
        console.print(f"[bold red]URL invalida:[/bold red] {exc}")
        raise typer.Exit(2)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        console.print(f"[bold red]Falha ao criar pasta de saida {output_dir}:[/bold red] {exc}")
        raise typer.Exit(2)

    invalid_chars = set('<>:"/\\|?*')
    if any(c in consolidated_name for c in invalid_chars):
        console.print(
            f"[bold red]Nome do consolidado contem caracteres invalidos[/bold red] "
            f"(<>:\"/\\|?*): {consolidated_name!r}"
        )
        raise typer.Exit(2)
    if not consolidated_name.lower().endswith(".pdf"):
        consolidated_name = consolidated_name + ".pdf"

    # --fresh: apaga manifest existente
    if fresh:
        manifest = output_dir / "manifest.json"
        if manifest.exists():
            try:
                manifest.unlink()
                console.print("[yellow]Manifest anterior removido[/yellow] (--fresh).")
            except OSError as exc:
                console.print(f"[yellow]Aviso: nao foi possivel remover manifest:[/yellow] {exc}")

    if not _check_disk_space(output_dir, console):
        console.print("[red]Cancelado por falta de espaco.[/red]")
        raise typer.Exit(2)

    # --regenerate implica --update (recrawl). --update sozinho so recrawl.
    force_recrawl = update or regenerate
    force_reexport = regenerate

    try:
        exit_code = asyncio.run(
            run_pipeline(
                start_url=validated_url,
                output_dir=output_dir,
                consolidated_name=consolidated_name,
                headless=headless,
                timeout_seconds=timeout_seconds,
                max_pages=max_pages,
                slow_threshold_seconds=slow_threshold_seconds,
                auto_confirm=yes,
                force_recrawl=force_recrawl,
                force_reexport=force_reexport,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrompido pelo usuario (Ctrl+C).[/bold yellow]")
        console.print("PDFs ja gerados foram preservados em pages/")
        console.print("Rode novamente para continuar de onde parou (resume automatico).")
        exit_code = 130
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[bold red]Erro fatal:[/bold red] {exc}")
        console.print_exception(show_locals=False)
        exit_code = 1

    raise typer.Exit(exit_code)


@app.command()
def version() -> None:
    """Mostra a versao do extrator."""
    typer.echo(__version__)


if __name__ == "__main__":
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
