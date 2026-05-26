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
    MANIFEST_FILENAME,
    RateLimitConfig,
    RateLimiter,
    ensure_output_dirs,
    find_pending_jobs,
    format_bytes,
    format_duration,
    get_console,
    sanitize_proxy_for_log,
    setup_logger,
    storage_state_path,
    validate_safe_path,
    validate_start_url,
    write_slow_pages_log,
)

__version__ = "0.7.0"

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
    rate_limit: RateLimitConfig,
    max_workers: int,
    proxy: str | None = None,
    dry_run: bool = False,
) -> int:
    pages_dir, log_file = ensure_output_dirs(output_dir)
    logger = setup_logger(log_file)
    console = get_console()

    timeout_ms = timeout_seconds * 1000
    pipeline_start = time.monotonic()

    checkpoint = _setup_checkpoint(output_dir, start_url, pages_dir, logger)
    resume_active = bool(
        checkpoint.manifest.crawl_complete and checkpoint.manifest.mapped_urls
    )
    resume_pdfs = len(checkpoint.manifest.exported)

    force_recrawl = _maybe_force_recrawl_for_max_pages(
        force_recrawl, checkpoint, max_pages, logger,
    )

    _print_intro_panel(
        console, start_url, output_dir, timeout_seconds,
        slow_threshold_seconds, headless, max_pages,
        resume_active, resume_pdfs,
        rate_limit=rate_limit, max_workers=max_workers,
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

    # Compartilha RateLimiter entre crawl e export: se o servidor bloquear no
    # crawl, o cooldown propaga para o export — evita o pipeline pegar 522
    # logo no inicio do export depois de ter sido bloqueado no crawl.
    shared_limiter = RateLimiter(rate_limit)

    # Storage state: cookies/localStorage persistidos entre runs.
    state_path = storage_state_path(output_dir)
    if proxy:
        # Mascara credenciais no log para nao vazar password/token
        logger.info("Proxy configurado: %s", sanitize_proxy_for_log(proxy))

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
            rate_limit=rate_limit,
            max_workers=max_workers,
            limiter=shared_limiter,
            state_path=state_path,
            proxy=proxy,
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

    if dry_run:
        _print_dry_run_summary(console, logger, urls, output_dir)
        return 0

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
            rate_limit=rate_limit,
            max_workers=max_workers,
            limiter=shared_limiter,
            state_path=state_path,
            proxy=proxy,
        )
    except KeyboardInterrupt:
        logger.warning("Exportacao de PDFs interrompida pelo usuario (Ctrl+C).")
        raise

    # Garante que ultimo batch de record_export foi salvo
    try:
        checkpoint.flush()
    except OSError as exc:
        logger.warning("Falha ao flush manifest: %s", exc)

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


def _maybe_force_recrawl_for_max_pages(
    force_recrawl: bool, checkpoint: Checkpoint, max_pages: int | None, logger,
) -> bool:
    """Se max_pages mudou SIGNIFICATIVAMENTE, forca re-crawl.

    Casos que NAO forcam recrawl:
      - Run anterior sem limite (None) + atual com limite: run anterior ja tem
        tudo, basta truncar — mas truncar implica perda, entao forca recrawl
      - Run anterior com limite + atual sem limite: pode ter mais URLs, forca
      - Mesmo limite: nao forca
      - Run anterior sem limite + atual sem limite: nao forca

    Resumo: forca apenas quando ambos definidos e diferentes, OU quando muda
    de definido para indefinido (e vice-versa).
    """
    if force_recrawl or not checkpoint.manifest.crawl_complete:
        return force_recrawl
    prev = checkpoint.manifest.crawl_max_pages
    if prev == max_pages:
        return False
    # Diferentes (incluindo None vs valor): forca recrawl
    logger.warning(
        "max_pages mudou (era %s, agora %s) — forcando re-crawl.",
        prev, max_pages,
    )
    return True


def _setup_checkpoint(
    output_dir: Path, start_url: str, pages_dir: Path, logger,
) -> Checkpoint:
    """Inicializa checkpoint e reconcilia com disco."""
    checkpoint = Checkpoint(output_dir, start_url, logger=logger)
    if checkpoint.previous_start_url:
        logger.warning(
            "Atencao: manifest tinha outro start_url (%s). "
            "Estado anterior descartado — comecando novo crawl. "
            "Use --output-dir diferente se quer manter o crawl anterior.",
            checkpoint.previous_start_url,
        )
    removed = checkpoint.reconcile_with_disk(pages_dir, logger=logger)
    if removed > 0:
        logger.warning(
            "Reconciliacao manifest x disco: %d PDFs faltando/invalidos. "
            "Serao regenerados.", removed,
        )
    return checkpoint


def _print_dry_run_summary(
    console, logger, urls: list[str], output_dir: Path,
) -> None:
    """Imprime resumo de dry-run (mapeou mas nao exportou)."""
    logger.info(
        "Dry-run: %d URLs mapeadas. Exportacao pulada.",
        len(urls),
    )
    estimated_min = int(len(urls) * 30 / 60)
    console.print(
        f"\n[bold cyan]Dry-run finalizado:[/bold cyan] {len(urls)} URLs mapeadas em "
        f"{output_dir}.\n"
        f"Estimativa: ~30s por PDF, total ~{estimated_min} minutos.\n"
        "Rode sem --dry-run para gerar PDFs.\n"
    )


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
    *,
    rate_limit: RateLimitConfig,
    max_workers: int,
) -> None:
    resume_line = ""
    if resume_active:
        resume_line = f"\n[bold yellow]Resume:[/bold yellow] {resume_pdfs} PDFs ja existem"
    rate_line = (
        f"\n[bold]Rate limit:[/bold] {rate_limit.base_delay_seconds:.1f}s/req | "
        f"[bold]Backoff:[/bold] {rate_limit.backoff_initial_seconds:.0f}s -> "
        f"{rate_limit.backoff_max_seconds:.0f}s | "
        f"[bold]Workers:[/bold] {max_workers}"
    )
    console.print(
        Panel.fit(
            f"[bold]URL inicial:[/bold] {start_url}\n"
            f"[bold]Saida:[/bold] {output_dir}\n"
            f"[bold]Timeout:[/bold] {timeout_seconds}s | "
            f"[bold]Slow:[/bold] >={slow_threshold_seconds:.0f}s | "
            f"[bold]Headless:[/bold] {headless} | "
            f"[bold]Max:[/bold] {max_pages or 'sem limite'}"
            f"{rate_line}"
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


def _handle_fresh_flag(output_dir: Path, start_url: str, console) -> None:
    """Backup + remocao do manifest quando --fresh."""
    manifest = output_dir / MANIFEST_FILENAME
    if not manifest.exists():
        return
    try:
        temp_ck = Checkpoint(output_dir, start_url)
        backup = temp_ck.backup()
        if backup:
            console.print(f"[dim]Backup criado: {backup}[/dim]")
    except (OSError, ValueError):
        pass
    try:
        manifest.unlink()
        console.print("[yellow]Manifest anterior removido[/yellow] (--fresh).")
    except OSError as exc:
        console.print(f"[yellow]Aviso: nao foi possivel remover manifest:[/yellow] {exc}")


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
        180, min=10, max=1800,
        help="Limite total por pagina (segundos). Default 180s = 3min (TDN tem paginas pesadas).",
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
    request_delay: float = typer.Option(
        2.0, min=0.0, max=60.0,
        help="Pausa em segundos entre requests (anti-rate-limit). Default 2s.",
    ),
    backoff_initial: float = typer.Option(
        30.0, min=5.0, max=600.0,
        help="Cooldown inicial em segundos apos detectar bloqueio (5xx/429/timeout).",
    ),
    backoff_max: float = typer.Option(
        900.0, min=60.0, max=7200.0,
        help="Teto do cooldown (default 900s = 15min) apos bloqueios sucessivos.",
    ),
    max_workers: int = typer.Option(
        1, min=1, max=8,
        help="Workers paralelos para exportar PDFs (cuidado: muitos = bloqueio do servidor).",
    ),
    proxy: str | None = typer.Option(
        None, help="HTTP/SOCKS proxy. Ex: http://host:8080 ou socks5://host:1080. "
        "Fallback: env HTTP_PROXY/HTTPS_PROXY.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Mapeia a arvore mas nao exporta PDFs (estimativa rapida).",
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

    # Valida path contra traversal e MAX_PATH (Windows legacy 260 chars)
    try:
        validate_safe_path(output_dir)
    except ValueError as exc:
        console.print(f"[bold red]Path invalido:[/bold red] {exc}")
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
        _handle_fresh_flag(output_dir, validated_url, console)

    if not _check_disk_space(output_dir, console):
        console.print("[red]Cancelado por falta de espaco.[/red]")
        raise typer.Exit(2)

    # --regenerate implica --update (recrawl). --update sozinho so recrawl.
    force_recrawl = update or regenerate
    force_reexport = regenerate

    rate_limit = RateLimitConfig(
        base_delay_seconds=request_delay,
        backoff_initial_seconds=backoff_initial,
        backoff_max_seconds=backoff_max,
    )

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
                rate_limit=rate_limit,
                max_workers=max_workers,
                proxy=proxy,
                dry_run=dry_run,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrompido pelo usuario (Ctrl+C).[/bold yellow]")
        console.print("PDFs ja gerados foram preservados em pages/")
        console.print("Rode novamente para continuar de onde parou (resume automatico).")
        exit_code = 130
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[bold red]Erro fatal:[/bold red] {exc}")
        _suggest_action_for_error(console, exc)
        console.print_exception(show_locals=False)
        exit_code = 1

    raise typer.Exit(exit_code)


def _suggest_action_for_error(console, exc: BaseException) -> None:
    """Imprime sugestao de acao baseada no tipo de erro."""
    msg = str(exc).lower()
    if any(k in msg for k in ("522", "523", "524", "429", "cloudflare")):
        console.print(
            "\n[bold yellow]Acao sugerida:[/bold yellow] o servidor TDN esta bloqueando "
            "(rate limit/Cloudflare).\n"
            "  1. Espere 15-60 minutos antes de tentar de novo.\n"
            "  2. Aumente --request-delay para 5.0 ou mais (ex: --request-delay 5).\n"
            "  3. Reduza --max-workers para 1.\n"
            "  4. Use modo [Lento e seguro] no iniciar.bat.\n"
        )
    elif "playwright" in msg or "chromium" in msg or "browser" in msg:
        console.print(
            "\n[bold yellow]Acao sugerida:[/bold yellow] problema com o Chromium.\n"
            "  1. Use opcao [9] do iniciar.bat para reinstalar o Chromium.\n"
            "  2. Ou rode: .venv\\Scripts\\python.exe -m playwright install --force chromium\n"
        )
    elif "disk" in msg or "space" in msg or "no space" in msg:
        console.print(
            "\n[bold yellow]Acao sugerida:[/bold yellow] sem espaco em disco.\n"
            "  1. Libere espaco na pasta de saida.\n"
            "  2. Mova PDFs antigos para outro local.\n"
        )
    elif "permission" in msg or "denied" in msg or "winerror 5" in msg:
        console.print(
            "\n[bold yellow]Acao sugerida:[/bold yellow] problema de permissao.\n"
            "  1. Rode o iniciar.bat como administrador.\n"
            "  2. Verifique se a pasta de saida nao esta em uso por outro programa.\n"
        )


@app.command()
def version() -> None:
    """Mostra a versao do extrator."""
    typer.echo(__version__)


@app.command()
def jobs(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta raiz onde procurar jobs incompletos.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Saida em JSON (para scripts/integracao).",
    ),
) -> None:
    """Lista jobs incompletos detectados em pastas com manifest.json."""
    console = get_console()
    pending = find_pending_jobs(output_dir)

    if json_output:
        import json as _json
        payload = [{
            "output_dir": str(j.output_dir),
            "start_url": j.start_url,
            "mapped_count": j.mapped_count,
            "queue_count": j.queue_count,
            "exported_count": j.exported_count,
            "failure_count": j.failure_count,
            "last_updated": j.last_updated,
            "crawl_complete": j.crawl_complete,
        } for j in pending]
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if not pending:
        console.print("[green]Nenhum job incompleto encontrado.[/green]")
        return

    table = Table(
        title=f"Jobs incompletos em {output_dir}/", border_style="yellow",
    )
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Pasta", style="bold")
    table.add_column("URL", overflow="fold")
    table.add_column("Mapeadas", justify="right")
    table.add_column("Fila", justify="right")
    table.add_column("PDFs", justify="right")
    table.add_column("Falhas", justify="right", style="red")
    table.add_column("Atualizado")
    for i, job in enumerate(pending, start=1):
        status = "" if job.crawl_complete else "[yellow](crawl incompleto)[/yellow] "
        table.add_row(
            str(i),
            f"{status}{job.display_name}",
            job.start_url,
            str(job.mapped_count),
            str(job.queue_count),
            str(job.exported_count),
            str(job.failure_count),
            job.last_updated.replace("T", " ").replace("+00:00", "Z"),
        )
    console.print(table)
    console.print(
        "\nPara continuar um job: rode novamente com a MESMA URL e --output-dir "
        "apontando para a subpasta. O resume eh automatico."
    )


@app.command()
def failures(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta do job (com manifest.json) a inspecionar.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Saida em JSON (para scripts/integracao).",
    ),
) -> None:
    """Lista URLs em failures (com tentativas e ultimo erro)."""
    console = get_console()
    manifest_path = output_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        console.print(f"[red]manifest.json nao encontrado em {output_dir}[/red]")
        raise typer.Exit(1)
    # Le manifest sem validacao de start_url (so listagem)
    import json as _json
    try:
        with manifest_path.open("r", encoding="utf-8") as fp:
            data = _json.load(fp)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Erro ao ler manifest: {exc}[/red]")
        raise typer.Exit(1)

    raw = data.get("failures", {}) or {}

    if json_output:
        payload = []
        for url, entry in raw.items():
            if isinstance(entry, str):
                payload.append({"url": url, "error": entry, "attempts": 1, "last_attempt": ""})
            else:
                payload.append({
                    "url": url,
                    "error": str(entry.get("error", "")),
                    "attempts": int(entry.get("attempts", 1)),
                    "last_attempt": str(entry.get("last_attempt", "")),
                })
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if not raw:
        console.print("[green]Nenhuma URL em failures.[/green]")
        return

    table = Table(
        title=f"Failures em {output_dir}/ ({len(raw)} URLs)",
        border_style="red",
    )
    table.add_column("Tentativas", justify="right", style="bold yellow")
    table.add_column("Ultimo erro", overflow="fold")
    table.add_column("Ultima tentativa")
    table.add_column("URL", overflow="fold")
    rows = []
    for url, entry in raw.items():
        if isinstance(entry, str):
            rows.append((1, entry, "", url))
        else:
            rows.append((
                int(entry.get("attempts", 1)),
                str(entry.get("error", "")),
                str(entry.get("last_attempt", "")).replace("T", " ").replace("+00:00", "Z"),
                url,
            ))
    rows.sort(key=lambda r: (-r[0], r[3]))  # mais tentativas primeiro
    for attempts, error, ts, url in rows:
        table.add_row(str(attempts), error, ts, url)
    console.print(table)


def _backup_and_remove_manifest(manifest_path: Path, output_dir: Path, console) -> None:
    """Faz backup do manifest e o remove."""
    if not manifest_path.exists():
        return
    try:
        data_url = ""
        try:
            import json as _json
            with manifest_path.open("r", encoding="utf-8") as fp:
                data_url = _json.load(fp).get("start_url", "")
        except (OSError, ValueError):
            pass
        temp_ck = Checkpoint(output_dir, data_url)
        backup = temp_ck.backup()
        if backup:
            console.print(f"[dim]Backup: {backup}[/dim]")
    except (OSError, ValueError):
        pass
    try:
        manifest_path.unlink()
    except OSError as exc:
        console.print(f"[red]Erro ao remover manifest: {exc}[/red]")


@app.command()
def reset(
    output_dir: Path = typer.Argument(..., help="Pasta do job a resetar."),
    hard: bool = typer.Option(
        False, "--hard",
        help="Reset total: remove manifest E PDFs. Senao so reseta manifest.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Pula confirmacao."),
) -> None:
    """Reseta um job. Soft: mantem PDFs e remove manifest. Hard: remove tudo."""
    console = get_console()

    # Valida que output_dir existe e eh pasta
    if output_dir.exists() and not output_dir.is_dir():
        console.print(f"[red]{output_dir} nao eh pasta[/red]")
        raise typer.Exit(2)

    manifest_path = output_dir / MANIFEST_FILENAME
    pages_dir = output_dir / "pages"

    if not manifest_path.exists() and not pages_dir.exists():
        console.print(f"[yellow]Nada para resetar em {output_dir}[/yellow]")
        return

    mode = "HARD (manifest + PDFs)" if hard else "SOFT (so manifest)"
    console.print(f"[bold yellow]Reset {mode} em {output_dir}[/bold yellow]")
    if not yes and sys.stdin.isatty() and not typer.confirm("Confirma?", default=False):
        console.print("Cancelado.")
        return

    _backup_and_remove_manifest(manifest_path, output_dir, console)

    if hard and pages_dir.exists():
        try:
            shutil.rmtree(pages_dir)
            console.print(f"[yellow]PDFs removidos: {pages_dir}[/yellow]")
        except OSError as exc:
            console.print(f"[red]Erro ao remover PDFs: {exc}[/red]")

    console.print("[green]Reset concluido.[/green]")


@app.command(name="clean-orphans")
def clean_orphans(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta do job.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Pula confirmacao."),
) -> None:
    """Remove PDFs em pages/ que nao tem entrada no manifest (orfaos)."""
    console = get_console()
    manifest_path = output_dir / MANIFEST_FILENAME
    pages_dir = output_dir / "pages"
    if not manifest_path.exists():
        console.print(f"[red]manifest.json nao encontrado em {output_dir}[/red]")
        raise typer.Exit(1)
    if not pages_dir.exists():
        console.print(f"[yellow]Nada em {pages_dir}[/yellow]")
        return

    import json as _json
    try:
        with manifest_path.open("r", encoding="utf-8") as fp:
            data = _json.load(fp)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Erro ao ler manifest: {exc}[/red]")
        raise typer.Exit(1)

    checkpoint = Checkpoint(output_dir, str(data.get("start_url", "")))
    orphans = checkpoint.find_orphan_pdfs(pages_dir)
    if not orphans:
        console.print("[green]Nenhum PDF orfao encontrado.[/green]")
        return

    table = Table(title=f"PDFs orfaos em {pages_dir} ({len(orphans)})", border_style="yellow")
    table.add_column("Arquivo")
    table.add_column("Tamanho", justify="right")
    total_bytes = 0
    for p in orphans:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        total_bytes += sz
        table.add_row(p.name, format_bytes(sz))
    console.print(table)
    console.print(f"\nTotal: {format_bytes(total_bytes)}")

    if not yes and sys.stdin.isatty():
        if not typer.confirm("Remover todos os orfaos?", default=False):
            console.print("Cancelado.")
            return

    removed = 0
    for p in orphans:
        try:
            p.unlink()
            removed += 1
        except OSError as exc:
            console.print(f"[red]Erro ao remover {p.name}: {exc}[/red]")
    console.print(f"[green]{removed} PDFs orfaos removidos.[/green]")


@app.command()
def report(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta do job.",
    ),
    format: str = typer.Option(
        "csv", help="Formato: csv ou json.",
    ),
    output_file: Path | None = typer.Option(
        None, help="Arquivo de saida. Default: report.csv|json no output_dir.",
    ),
) -> None:
    """Gera relatorio de execucao (CSV ou JSON) com stats por URL."""
    console = get_console()
    manifest_path = output_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        console.print(f"[red]manifest.json nao encontrado em {output_dir}[/red]")
        raise typer.Exit(1)

    import json as _json
    try:
        with manifest_path.open("r", encoding="utf-8") as fp:
            data = _json.load(fp)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Erro ao ler manifest: {exc}[/red]")
        raise typer.Exit(1)

    fmt = format.lower()
    if fmt not in ("csv", "json"):
        console.print("[red]Formato deve ser 'csv' ou 'json'[/red]")
        raise typer.Exit(2)

    target = output_file or (output_dir / f"report.{fmt}")
    rows = _build_report_rows(data)

    if fmt == "csv":
        _write_report_csv(target, rows)
    else:
        _write_report_json(target, rows, data)
    console.print(f"[green]Relatorio gerado:[/green] {target} ({len(rows)} linhas)")


def _build_report_rows(data: dict) -> list[dict]:
    """Constroi rows do relatorio a partir do manifest."""
    rows: list[dict] = []
    exported = data.get("exported", {}) or {}
    failures = data.get("failures", {}) or {}
    mapped = data.get("mapped_urls", []) or []
    for url in mapped:
        if url in exported:
            entry = exported[url]
            rows.append({
                "url": url,
                "status": "exported",
                "filename": entry.get("filename", ""),
                "title": entry.get("title", ""),
                "size_bytes": entry.get("size_bytes", 0),
                "elapsed_seconds": round(float(entry.get("elapsed_seconds", 0)), 2),
                "error": "",
                "attempts": 0,
            })
        elif url in failures:
            f = failures[url]
            error = f if isinstance(f, str) else f.get("error", "")
            attempts = 1 if isinstance(f, str) else int(f.get("attempts", 1))
            rows.append({
                "url": url, "status": "failed", "filename": "", "title": "",
                "size_bytes": 0, "elapsed_seconds": 0, "error": error,
                "attempts": attempts,
            })
        else:
            rows.append({
                "url": url, "status": "pending", "filename": "", "title": "",
                "size_bytes": 0, "elapsed_seconds": 0, "error": "", "attempts": 0,
            })
    return rows


def _write_report_csv(target: Path, rows: list[dict]) -> None:
    """Escreve CSV com escape forte (QUOTE_ALL) para URLs/titles com virgulas/aspas/newlines."""
    import csv
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ["url", "status", "filename", "title", "size_bytes",
              "elapsed_seconds", "error", "attempts"]

    def _clean(value):
        """Remove newlines e CR de strings (CSV nao deveria ter)."""
        if isinstance(value, str):
            return value.replace("\r", " ").replace("\n", " ").strip()
        return value

    sanitized = [{k: _clean(v) for k, v in row.items()} for row in rows]
    with target.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(sanitized)


def _write_report_json(target: Path, rows: list[dict], data: dict) -> None:
    import json as _json
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "start_url": data.get("start_url", ""),
        "last_updated": data.get("last_updated", ""),
        "crawl_complete": data.get("crawl_complete", False),
        "summary": {
            "total": len(rows),
            "exported": sum(1 for r in rows if r["status"] == "exported"),
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "pending": sum(1 for r in rows if r["status"] == "pending"),
            "total_bytes": sum(int(r.get("size_bytes", 0)) for r in rows),
        },
        "rows": rows,
    }
    with target.open("w", encoding="utf-8") as fp:
        _json.dump(payload, fp, indent=2, ensure_ascii=False)


def _install_signal_handlers() -> None:
    """Instala handlers para SIGTERM/SIGINT que levantam KeyboardInterrupt.

    Sem isto, task managers (systemd, docker stop) enviam SIGTERM e o processo
    morre sem chance de salvar o manifest. Convertendo para KeyboardInterrupt,
    o pipeline normal de cleanup (try/finally) eh acionado.
    """
    import signal

    def _handler(signum, _frame):
        raise KeyboardInterrupt(f"Sinal {signum} recebido")

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (AttributeError, ValueError):
        pass  # Windows pode nao ter SIGTERM em alguns contextos
    # SIGINT (Ctrl+C) ja levanta KeyboardInterrupt por default — nao reinstala


if __name__ == "__main__":
    _install_signal_handlers()
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
