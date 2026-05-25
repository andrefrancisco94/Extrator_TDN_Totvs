from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from .crawler import crawl_confluence_tree
from .pdf_exporter import export_pages_to_pdf
from .pdf_merge import merge_pdfs
from .utils import canonicalize_url, ensure_output_dirs, get_console, setup_logger

__version__ = "0.2.0"

app = typer.Typer(add_completion=False, no_args_is_help=True)


async def run_pipeline(
    start_url: str,
    output_dir: Path,
    consolidated_name: str,
    headless: bool,
    timeout_seconds: int,
    max_pages: int | None,
) -> int:
    _, log_file = ensure_output_dirs(output_dir)
    logger = setup_logger(log_file)
    console = get_console()

    start_url = canonicalize_url(start_url)
    timeout_ms = timeout_seconds * 1000

    console.print(
        Panel.fit(
            f"[bold]URL inicial:[/bold] {start_url}\n"
            f"[bold]Saída:[/bold] {output_dir}\n"
            f"[bold]Timeout:[/bold] {timeout_seconds}s | "
            f"[bold]Headless:[/bold] {headless} | "
            f"[bold]Max:[/bold] {max_pages or '∞'}",
            title="Extrator TDN TOTVS",
            border_style="cyan",
        )
    )

    logger.info("URL inicial: %s", start_url)
    logger.info("Diretório de saída: %s", output_dir)

    urls = await crawl_confluence_tree(
        start_url=start_url,
        logger=logger,
        headless=headless,
        timeout_ms=timeout_ms,
        max_pages=max_pages,
    )

    if not urls:
        logger.error("Nenhuma página foi encontrada a partir da URL inicial.")
        return 1

    pdf_files, failures = await export_pages_to_pdf(
        urls=urls,
        output_dir=output_dir,
        logger=logger,
        headless=headless,
        timeout_ms=timeout_ms,
    )

    if not pdf_files:
        logger.error("Nenhum PDF individual foi gerado.")
        return 1

    consolidated_path = output_dir / consolidated_name
    merge_pdfs(pdf_files, consolidated_path, logger)

    logger.info(
        "Resumo: %s URL(s), %s PDF(s), %s falha(s)",
        len(urls), len(pdf_files), len(failures),
    )

    table = Table(title="Resumo da execução", border_style="green", show_header=False)
    table.add_column("Métrica", style="bold cyan")
    table.add_column("Valor")
    table.add_row("URLs mapeadas", str(len(urls)))
    table.add_row("PDFs individuais", str(len(pdf_files)))
    table.add_row("Falhas", str(len(failures)))
    table.add_row("Consolidado", str(consolidated_path))
    table.add_row("Log", str(log_file))
    console.print(table)

    if failures:
        fail_table = Table(title="Falhas registradas", border_style="red")
        fail_table.add_column("URL", overflow="fold")
        fail_table.add_column("Erro", overflow="fold")
        for url, error in failures:
            fail_table.add_row(url, error)
        console.print(fail_table)

    return 0


@app.command()
def run(
    start_url: str = typer.Argument(..., help="URL inicial da documentação Confluence/TDN."),
    output_dir: Path = typer.Option(Path("output"), help="Pasta de saída para PDFs e log."),
    consolidated_name: str = typer.Option(
        "TDN_TOTVS_consolidado.pdf",
        help="Nome do PDF final consolidado.",
    ),
    headless: bool = typer.Option(True, "--headless/--headed", help="Executa navegador sem interface."),
    timeout_seconds: int = typer.Option(60, min=10, help="Timeout por página (segundos)."),
    max_pages: int | None = typer.Option(None, min=1, help="Limite de páginas para teste."),
) -> None:
    """Mapeia a árvore lateral do Confluence, exporta PDFs individuais e gera consolidado."""
    exit_code = asyncio.run(
        run_pipeline(
            start_url=start_url,
            output_dir=output_dir,
            consolidated_name=consolidated_name,
            headless=headless,
            timeout_seconds=timeout_seconds,
            max_pages=max_pages,
        )
    )
    raise typer.Exit(exit_code)


@app.command()
def version() -> None:
    """Mostra a versão do extrator."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
