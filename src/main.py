from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from .crawler import crawl_confluence_tree
from .markdown_exporter import export_pages_to_markdown
from .markdown_merge import merge_markdown
from .pdf_exporter import export_pages_to_pdf
from .pdf_merge import merge_pdfs
from .proxy import (
    PROXY_FILE_DEFAULT,
    AllProxiesDeadError,
    ProxyConfigError,
    ProxyPool,
    load_proxy_file,
    mask_proxy,
    normalize_proxy_url,
    resolve_exits,
    save_proxy_file,
    to_playwright_proxy,
)
from .proxy_check import DEFAULT_TIMEOUT_SECONDS as PROBE_TIMEOUT_SECONDS
from .proxy_check import estimate_seconds, probe_many, rank_working
from .utils import (
    BlockedTooLongError,
    Checkpoint,
    InvalidStartUrlError,
    JobLock,
    JobLockError,
    MANIFEST_FILENAME,
    RateLimitConfig,
    RateLimiter,
    chrome_user_agent,
    ensure_output_dirs,
    find_jobs,
    format_bytes,
    format_duration,
    generate_correlation_id,
    get_console,
    setup_logger,
    start_url_key,
    storage_state_path,
    validate_safe_path,
    validate_start_url,
    write_slow_pages_log,
)

from .utils import LARGE_BATCH_THRESHOLD  # re-export para retrocompat

__version__ = "0.11.1"

MIN_DISK_FREE_BYTES = 500 * 1024 * 1024  # 500MB minimo razoavel

# Codigo de saida quando o run terminou mas a documentacao ficou incompleta
# (paginas faltando, mapeamento incompleto ou IP bloqueado por tempo demais).
# O progresso esta salvo: basta retomar o job.
EXIT_INCOMPLETE = 3
# `resume` sem nada a fazer (nenhum job encontrado ou escolha cancelada). O
# menu usa este codigo para voltar direto ao menu em vez de encerrar.
EXIT_NOTHING_TO_DO = 4

# Onde o menu procura jobs existentes: a pasta padrao e a pasta atual (um job
# criado fora de output/ nao pode ficar invisivel).
JOB_ROOTS = (Path("output"), Path("."))

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
    debug: bool = False,
    retry_failed_only: bool = False,
    quiet: bool = False,
    proxies: list[str | None] | None = None,
) -> int:
    pages_dir, log_file = ensure_output_dirs(output_dir)
    correlation_id = generate_correlation_id()
    logger = setup_logger(log_file, debug=debug, correlation_id=correlation_id)
    console = get_console()
    if quiet:
        # Suprime output rich + logger (so erros + summary)
        console.quiet = True
        # Logger ainda escreve no arquivo run.log mas remove handler do console
        import logging as _lg
        for handler in list(logger.handlers):
            # RichHandler tem nome de classe contendo "Rich"
            if "Rich" in type(handler).__name__:
                logger.removeHandler(handler)
        # Sobe nivel para WARNING (so warnings/errors visiveis se algum
        # RichHandler escapar)
        logger.setLevel(_lg.WARNING)
    logger.info("Run iniciado: correlation_id=%s", correlation_id)

    # Lock file: impede 2 processos rodando no mesmo output_dir
    job_lock = JobLock(output_dir)
    try:
        job_lock.acquire()
    except JobLockError as exc:
        console.print(f"[bold red]Lock conflict:[/bold red] {exc}")
        # NAO chama release — lock nunca foi adquirido por este processo
        return 2
    try:
        pool = await _build_checked_pool(proxies, proxy, start_url, logger, console)
        return await _run_pipeline_inner(
            start_url, output_dir, consolidated_name, headless, timeout_seconds,
            max_pages, slow_threshold_seconds, auto_confirm, force_recrawl,
            force_reexport, rate_limit, max_workers, proxy, dry_run,
            pages_dir, log_file, logger, console, retry_failed_only, pool,
        )
    except BlockedTooLongError as exc:
        logger.error("Run interrompido: %s", exc)
        _print_blocked_panel(console, exc, output_dir, _resume_hint(force_recrawl, force_reexport))
        return EXIT_INCOMPLETE
    except AllProxiesDeadError as exc:
        logger.error("Run interrompido: %s", exc)
        console.print(f"\n[bold red]Proxies indisponiveis:[/bold red] {exc}")
        console.print("O progresso foi salvo. Corrija a lista de proxies e retome o job.")
        return EXIT_INCOMPLETE
    except ProxyConfigError as exc:
        console.print(f"[bold red]Proxy invalido:[/bold red] {exc}")
        return 2
    finally:
        # release() eh idempotente (verifica _held interno)
        job_lock.release()


def _normalized_exits(proxies: list[str | None] | None, proxy: str | None) -> list[str | None]:
    if proxies:
        return [normalize_proxy_url(item) if item else None for item in proxies]
    return [normalize_proxy_url(proxy)]


def _build_pool(proxies: list[str | None] | None, proxy: str | None) -> ProxyPool:
    """Monta o rodizio de saidas de rede a partir da lista ou do proxy unico."""
    return ProxyPool(_normalized_exits(proxies, proxy))


# A partir de quantos proxies a lista passa pela triagem antes do run. Listas
# pequenas sao proxies escolhidos a dedo; as grandes (publicas) vem cheias de
# enderecos mortos, e descobrir isso pelo browser custa minutos por proxy.
_TRIAGE_MIN_PROXIES = 6


def _with_direct(working: list[str], exits: list[str | None]) -> list[str | None]:
    """Recoloca a conexao direta ('direct' na lista) junto dos proxies aprovados,
    como primeira ou ultima opcao, conforme estava na lista original."""
    if None not in exits:
        return list(working)
    return [None, *working] if exits[0] is None else [*working, None]


def _progress_printer(console, label: str):
    """Callback de progresso da triagem: uma linha a cada ~10%."""
    state = {"next": 10}

    def report(done: int, total: int) -> None:
        percent = done * 100 // max(1, total)
        if percent >= state["next"] and done < total:
            state["next"] = percent - percent % 10 + 10
            console.print(f"[dim]  {label}: {done}/{total}[/dim]")

    return report


async def _build_checked_pool(
    proxies: list[str | None] | None, proxy: str | None, start_url: str, logger, console,
) -> ProxyPool:
    """Monta o rodizio; lista grande passa antes pela triagem.

    So entram os proxies que respondem, do mais rapido ao mais lento (1 request
    leve por proxy, em paralelo). TDN_SKIP_PROXY_CHECK=1 desliga a triagem.
    """
    exits = _normalized_exits(proxies, proxy)
    candidates = [item for item in dict.fromkeys(exits) if item is not None]
    if len(candidates) < _TRIAGE_MIN_PROXIES or os.environ.get("TDN_SKIP_PROXY_CHECK") == "1":
        return ProxyPool(exits)
    from urllib.parse import urlsplit  # noqa: PLC0415

    origin = urlsplit(start_url)
    target = f"{origin.scheme}://{origin.netloc}/cdn-cgi/trace"
    console.print(
        f"[bold]Testando {len(candidates)} proxies[/bold] antes de comecar "
        f"(no maximo ~{format_duration(estimate_seconds(len(candidates)))})..."
    )
    started = time.monotonic()
    results = await probe_many(
        candidates, target, on_progress=_progress_printer(console, "testados"),
    )
    working = rank_working(results)
    logger.info(
        "Triagem de proxies: %d de %d responderam em ate %.0fs (levou %s).",
        len(working), len(candidates), PROBE_TIMEOUT_SECONDS,
        format_duration(time.monotonic() - started),
    )
    if not working:
        raise AllProxiesDeadError(
            f"Nenhum dos {len(candidates)} proxies da lista respondeu. Troque a lista "
            "(opcao [P] do menu) ou remova-a para usar a conexao direta."
        )
    console.print(
        f"[green]{len(working)} de {len(candidates)} proxies responderam[/green]; "
        f"o run usa so esses, do mais rapido ({working[0].seconds:.1f}s) "
        f"ao mais lento ({working[-1].seconds:.1f}s)."
    )
    return ProxyPool(_with_direct([item.proxy for item in working], exits))


def _resume_hint(force_recrawl: bool, force_reexport: bool) -> tuple[str, str]:
    """(opcao do menu, modo do `resume`) que realmente continua o que foi pedido.

    Um re-mapeamento interrompido (--update/--regenerate) mantem o mapa antigo
    marcado como completo: [1] e [F] NAO o refazem. So repetindo a mesma opcao.
    """
    if force_reexport:
        return "[5] Regenerar todos os PDFs", "regenerate"
    if force_recrawl:
        return "[4] Verificar atualizacoes", "update"
    return "[1] Continuar trabalho anterior", "auto"


def _print_blocked_panel(
    console, exc: BlockedTooLongError, output_dir: Path,
    hint: tuple[str, str] = ("[1] Continuar trabalho anterior", "auto"),
) -> None:
    resume_at = time.strftime("%H:%M", time.localtime(time.time() + exc.seconds))
    option, mode = hint
    console.print(Panel.fit(
        f"O servidor bloqueou este IP (Cloudflare erro 1015) por mais "
        f"{format_duration(exc.seconds)} — ate cerca de {resume_at}.\n"
        "Isso passa do limite de espera configurado (--max-block-wait), entao o "
        "run parou.\n\n"
        f"[bold]Nada foi perdido:[/bold] o progresso esta salvo em {output_dir}.\n"
        f"Depois desse horario use a opcao {option} do menu\n"
        f"(CLI: python -m src.main resume --output-dir \"{output_dir}\" --mode {mode}),\n"
        "ou configure proxies na opcao [P] para nao depender de um unico IP.",
        title="IP bloqueado pelo servidor", border_style="red",
    ))


async def _run_pipeline_inner(
    start_url, output_dir, consolidated_name, headless, timeout_seconds,
    max_pages, slow_threshold_seconds, auto_confirm, force_recrawl,
    force_reexport, rate_limit, max_workers, proxy, dry_run,
    pages_dir, log_file, logger, console, retry_failed_only=False, pool=None,
) -> int:
    """Pipeline interno (encapsulado no lock). Mantem assinatura original."""
    timeout_ms = timeout_seconds * 1000
    pipeline_start = time.monotonic()
    if pool is None:
        pool = _build_pool(None, proxy)

    checkpoint = _setup_checkpoint(output_dir, start_url, pages_dir, logger)
    if checkpoint.previous_start_url:
        return _abort_start_url_mismatch(
            console, logger, output_dir, start_url, checkpoint.previous_start_url,
        )
    # Mesma pagina digitada de outra forma (barra final, /display/ vs
    # /display/public/): segue com a URL gravada no manifest, para que as
    # chaves das paginas ja exportadas continuem batendo.
    if checkpoint.manifest.start_url and checkpoint.manifest.start_url != start_url:
        logger.info(
            "URL inicial equivalente a do manifest; usando a forma gravada: %s",
            checkpoint.manifest.start_url,
        )
        start_url = checkpoint.manifest.start_url

    is_new_job = not checkpoint.manifest.mapped_urls and not checkpoint.manifest.exported
    if is_new_job:
        _warn_job_for_same_url_elsewhere(console, logger, start_url, output_dir, force_recrawl)
    # Gravado no manifest para que `resume` reuse o MESMO nome de consolidado
    # (sem adivinhar por arquivos da pasta).
    checkpoint.manifest.consolidated_name = consolidated_name

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
    previous_map_complete = bool(checkpoint.manifest.crawl_complete)

    # Compartilha RateLimiter entre crawl e export: se o servidor bloquear no
    # crawl, o cooldown propaga para o export — evita o pipeline pegar 522
    # logo no inicio do export depois de ter sido bloqueado no crawl.
    shared_limiter = RateLimiter(rate_limit, logger)

    # Storage state: cookies/localStorage persistidos entre runs.
    state_path = storage_state_path(output_dir)
    # Registra por onde os requests saem (credenciais sempre mascaradas).
    if len(pool) > 1:
        logger.info(
            "Saidas de rede (%d, com rodizio em bloqueio): %s",
            len(pool), pool.summary(),
        )
        console.print(
            f"[bold]Saidas de rede:[/bold] {len(pool)} (rodizio automatico quando "
            "um IP for bloqueado)"
        )
    elif pool.current:
        logger.info("Proxy configurado: %s", pool.current_label)
        console.print(f"[bold]Proxy:[/bold] {pool.current_label}")
    else:
        logger.info("Saida de rede: conexao direta (sem proxy).")

    crawl_complete = True
    crawl_note = ""
    if retry_failed_only:
        urls = list(checkpoint.manifest.mapped_urls)
        slow_crawl = []
        crawl_complete = bool(checkpoint.manifest.crawl_complete)
        legacy_giveups = checkpoint.crawl_giveups()
        if crawl_complete and legacy_giveups:
            # Mapa fechado por versao antiga com paginas sem os filhos listados:
            # este modo nao re-mapeia, entao nao pode dizer que esta completo.
            crawl_complete = False
            crawl_note = (
                f"mapa antigo com {len(legacy_giveups)} pagina(s) sem os filhos listados"
            )
        if not urls and not checkpoint.manifest.failures:
            logger.error(
                "Modo --retry-failed-only requer manifest com URLs mapeadas ou "
                "failures, mas manifest esta vazio. Rode sem --retry-failed-only "
                "para iniciar novo crawl."
            )
            console.print(
                "[bold red]Erro:[/bold red] --retry-failed-only sem manifest. "
                "Rode sem essa flag primeiro para mapear URLs."
            )
            return 1
        logger.info(
            "Modo --retry-failed-only: pulando crawl, %d URLs em mapped, %d em failures",
            len(urls), len(checkpoint.manifest.failures),
        )
    else:
        try:
            crawl = await crawl_confluence_tree(
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
                pool=pool,
            )
        except KeyboardInterrupt:
            logger.warning("Crawl interrompido pelo usuario (Ctrl+C).")
            raise
        urls, slow_crawl = crawl.urls, crawl.slow_records
        crawl_complete, crawl_note = crawl.complete, crawl.note

    if not urls:
        logger.error("Nenhuma pagina foi encontrada a partir da URL inicial.")
        return 1

    # Diff de URLs (so relevante em modo update/regenerate). Um re-mapeamento
    # que nao terminou nao prova que alguma pagina saiu da TDN: sem diff.
    # So ha "paginas novas/removidas" quando um mapa COMPLETO foi re-mapeado por
    # inteiro; retomar um mapeamento interrompido nao e novidade na TDN.
    current_set = set(urls)
    diff_valid = (
        force_recrawl and previous_map_complete and bool(previous_mapped) and crawl_complete
    )
    # Compara por chave independente da forma da URL: /display/X/T e
    # /display/public/X/T sao a mesma pagina (mapas antigos misturam as duas).
    def _page_key(url: str) -> str:
        return url.replace("/display/public/", "/display/", 1)

    current_keys = {_page_key(u) for u in current_set}
    previous_keys = {_page_key(u) for u in previous_mapped}
    added_urls = sorted(u for u in current_set if _page_key(u) not in previous_keys) if diff_valid else []
    removed_urls = sorted(u for u in previous_mapped if _page_key(u) not in current_keys) if diff_valid else []
    if force_recrawl and diff_valid:
        logger.info(
            "Diff do re-crawl: %d novas, %d removidas, %d inalteradas.",
            len(added_urls), len(removed_urls),
            len(current_keys & previous_keys),
        )

    if dry_run:
        _print_dry_run_summary(console, logger, urls, output_dir)
        if not crawl_complete:
            logger.warning("Dry-run com mapeamento incompleto: %s", crawl_note)
            console.print(
                f"[bold red]Atencao:[/bold red] o mapeamento NAO terminou ({crawl_note}); "
                "a contagem acima e parcial. Rode de novo para retomar."
            )
            return EXIT_INCOMPLETE
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
            pool=pool,
        )
    except KeyboardInterrupt:
        logger.warning("Exportacao de PDFs interrompida pelo usuario (Ctrl+C).")
        raise

    # Garante que ultimo batch de record_export foi salvo
    try:
        checkpoint.flush()
    except OSError as exc:
        logger.warning("Falha ao flush manifest: %s", exc)

    # O consolidado e montado a partir do MANIFEST, na ordem do mapa: entra toda
    # pagina que tem PDF valido, tenha sido gerada agora ou em run anterior. A
    # lista devolvida pelo export so tem o que ESTE run processou; usa-la faria
    # um run abortado (ou um --regenerate com falhas) encolher o consolidado.
    merge_entries: list[tuple[Path, str]] = []
    missing_urls: list[str] = []
    for url in urls:
        entry = checkpoint.get_exported_entry(url) if checkpoint.is_exported(url, pages_dir) else None
        if entry is None:
            missing_urls.append(url)
        else:
            merge_entries.append((pages_dir / entry["filename"], str(entry.get("title", ""))))

    if not merge_entries:
        logger.error("Nenhum PDF individual foi gerado.")
        return 1

    # --regenerate: pagina que falhou ou nem chegou a ser processada continua
    # com o PDF ANTIGO. Nao esta "faltando", mas tambem nao foi atualizada.
    stale_urls: list[str] = []
    if force_reexport:
        fresh_paths = {path for path, _title in pdf_entries}
        missing_set = set(missing_urls)
        stale_urls = [
            url for url in urls
            if url not in missing_set
            and pages_dir / checkpoint.get_exported_entry(url)["filename"] not in fresh_paths
        ]
    # Havia mapa completo e o mapeamento desta execucao nao fechou: o mapa
    # antigo foi mantido e so um novo re-mapeamento ([4]) resolve.
    remap_pending = previous_map_complete and not crawl_complete
    incomplete = bool(missing_urls) or bool(stale_urls) or not crawl_complete

    consolidated_path: Path | None = output_dir / consolidated_name
    try:
        merge_pdfs(merge_entries, consolidated_path, logger)
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao consolidar PDFs")
        consolidated_path = None
    if consolidated_path and missing_urls:
        logger.warning(
            "PDF consolidado INCOMPLETO: faltam %d de %d paginas.",
            len(missing_urls), len(urls),
        )

    all_slow = slow_crawl + slow_pdf
    slow_log_path = _maybe_write_slow_log(all_slow, output_dir, logger)

    total_elapsed = time.monotonic() - pipeline_start
    logger.info(
        "Resumo: %s URL(s), %s PDF(s), %s falha(s), %s lenta(s), tempo total %s",
        len(urls), len(merge_entries), len(failures), len(all_slow),
        format_duration(total_elapsed),
    )
    logger.info(
        "Rate limit: %d bloqueio(s) no run, %s aguardando, intervalo final %.1fs.",
        shared_limiter.total_blocks,
        format_duration(shared_limiter.total_wait_seconds),
        shared_limiter.current_delay,
    )

    _print_summary_tables(
        console=console,
        urls=urls, pdf_entries=merge_entries, failures=failures, all_slow=all_slow,
        slow_threshold_seconds=slow_threshold_seconds,
        consolidated_path=consolidated_path, log_file=log_file,
        slow_log_path=slow_log_path, manifest_path=checkpoint.path,
        total_elapsed=total_elapsed,
        added_urls=added_urls, removed_urls=removed_urls,
        blocks=shared_limiter.total_blocks,
        missing_count=len(missing_urls),
    )

    if incomplete:
        _print_incomplete_panel(
            console, logger, output_dir, total=len(urls), missing_urls=missing_urls,
            crawl_complete=crawl_complete, crawl_note=crawl_note,
            stale_urls=stale_urls,
            hint=(
                _resume_hint(force_recrawl or remap_pending, force_reexport)
                if (remap_pending or stale_urls) else None
            ),
        )
    if not consolidated_path:
        return 2
    return EXIT_INCOMPLETE if incomplete else 0


def _abort_start_url_mismatch(
    console, logger, output_dir: Path, start_url: str, previous: str,
) -> int:
    """Recusa reutilizar a pasta de um job de OUTRA URL (nunca descarta sozinho)."""
    logger.error(
        "A pasta %s ja contem um job de outra URL inicial (%s); pedido atual: %s. "
        "Nada foi alterado.", output_dir, previous, start_url,
    )
    console.print(Panel.fit(
        f"A pasta [bold]{output_dir}[/bold] ja contem um job de OUTRA URL inicial:\n"
        f"  gravada : {previous}\n"
        f"  pedida  : {start_url}\n\n"
        "Continuar descartaria o registro das paginas ja exportadas, entao o run "
        "foi cancelado e nada foi alterado.\n"
        "  - Para continuar o job existente: opcao [1] do menu (a URL e lida do job).\n"
        "  - Para um job novo: escolha outra pasta de saida.\n"
        "  - Para realmente recomecar nesta pasta: use --fresh.",
        title="Pasta pertence a outro job", border_style="red",
    ))
    return 2


def _warn_job_for_same_url_elsewhere(
    console, logger, start_url: str, output_dir: Path, force_recrawl: bool,
) -> None:
    """Avisa quando ja existe, em outra pasta, um job para a mesma URL inicial.

    Foi assim que uma "verificacao de atualizacoes" virou extracao completa:
    a pasta digitada (CFGTRIB) nao era a do job existente (output\\CFGTRIB).
    """
    try:
        target = output_dir.resolve()
        key = start_url_key(start_url)
        others = [
            job for job in find_jobs(JOB_ROOTS, include_complete=True)
            if start_url_key(job.start_url) == key and job.output_dir.resolve() != target
        ]
    except OSError:
        return
    if not others:
        return
    lines = "\n".join(
        f"  - {job.output_dir}  ({job.exported_count} PDFs, {job.mapped_count} paginas mapeadas)"
        for job in others
    )
    mode = "atualizacao (--update)" if force_recrawl else "execucao"
    logger.warning(
        "Ja existe job para esta URL em outra pasta (%s); esta %s cria um job NOVO em %s.",
        ", ".join(str(job.output_dir) for job in others), mode, output_dir,
    )
    console.print(Panel.fit(
        f"Ja existe um job para esta mesma URL em:\n{lines}\n\n"
        f"Esta {mode} vai criar um job [bold]novo[/bold] em {output_dir} e baixar "
        "tudo de novo.\nSe a intencao era atualizar o job existente, interrompa "
        "(Ctrl+C) e use a opcao [4] do menu, escolhendo-o na lista.",
        title="Atencao: pasta diferente do job existente", border_style="yellow",
    ))


def _print_incomplete_panel(
    console, logger, output_dir: Path, *, total: int, missing_urls: list[str],
    crawl_complete: bool, crawl_note: str,
    stale_urls: list[str] | None = None,
    hint: tuple[str, str] | None = None,
) -> None:
    """Diz claramente que a documentacao ficou incompleta e como terminar.

    hint: (opcao do menu, modo do resume) quando o que ficou pendente e um
    re-mapeamento ou uma regeneracao — nesses casos [F]/[1] nao resolvem.
    """
    stale_urls = stale_urls or []
    lines: list[str] = []
    if missing_urls:
        lines.append(
            f"[bold]{len(missing_urls)} de {total} paginas ficaram sem PDF[/bold] "
            "(o consolidado foi gerado SEM elas):"
        )
        lines.extend(f"  - {url}" for url in missing_urls[:15])
        if len(missing_urls) > 15:
            lines.append(f"  ... e mais {len(missing_urls) - 15}")
    if stale_urls:
        lines.append(
            f"[bold]{len(stale_urls)} de {total} paginas NAO foram regeneradas[/bold] "
            "(o PDF antigo foi mantido no consolidado):"
        )
        lines.extend(f"  - {url}" for url in stale_urls[:15])
        if len(stale_urls) > 15:
            lines.append(f"  ... e mais {len(stale_urls) - 15}")
    if not crawl_complete:
        lines.append(
            "[bold]O mapeamento da arvore nao terminou[/bold]"
            + (f" ({crawl_note})" if crawl_note else "")
            + (
                ": o mapa anterior foi mantido; paginas novas ainda nao foram procuradas."
                if hint else ": pode haver paginas ainda nao descobertas."
            )
        )
    lines.append("")
    if hint:
        option, mode = hint
        lines.append("Para concluir, repita a mesma operacao (o que ja esta pronto e reaproveitado):")
        lines.append(f"  - menu: opcao {option}")
    else:
        mode = "retry-failed"
        lines.append("Para terminar SO o que falta (sem refazer o que ja esta pronto):")
        lines.append("  - menu: opcao [F] Reprocessar paginas que faltaram")
    lines.append(
        f"  - CLI : python -m src.main resume --output-dir \"{output_dir}\" --mode {mode}"
    )
    logger.warning(
        "Documentacao INCOMPLETA em %s: %d pagina(s) sem PDF, %d nao regenerada(s), mapeamento %s.",
        output_dir, len(missing_urls), len(stale_urls),
        "completo" if crawl_complete else "incompleto",
    )
    console.print(Panel.fit(
        "\n".join(lines), title="DOCUMENTACAO INCOMPLETA", border_style="red",
    ))


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
        # Pasta de outro job: o chamador aborta sem gravar nada (o manifest em
        # memoria e novo e vazio; reconciliar/salvar apagaria o do disco).
        return checkpoint
    removed = checkpoint.reconcile_with_disk(pages_dir, logger=logger)
    if removed > 0:
        logger.warning(
            "Reconciliacao manifest x disco: %d PDFs faltando/invalidos. "
            "Serao regenerados.", removed,
        )
    # Valida invariantes do manifest (alerta sobre inconsistencias)
    issues = checkpoint.check_invariants(logger=logger)
    if issues:
        logger.warning(
            "%d inconsistencia(s) detectada(s) no manifest. Veja log para detalhes.",
            len(issues),
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
    blocks: int = 0,
    missing_count: int = 0,
) -> None:
    main_table = _build_main_summary_table(
        urls=urls, pdf_entries=pdf_entries, failures=failures, all_slow=all_slow,
        slow_threshold_seconds=slow_threshold_seconds,
        consolidated_path=consolidated_path, log_file=log_file,
        slow_log_path=slow_log_path, manifest_path=manifest_path,
        total_elapsed=total_elapsed,
        added_urls=added_urls, removed_urls=removed_urls,
        blocks=blocks, missing_count=missing_count,
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
    blocks: int = 0,
    missing_count: int = 0,
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

    border = "red" if missing_count else "green"
    table = Table(title="Resumo da execucao", border_style=border, show_header=False)
    table.add_column("Metrica", style="bold cyan")
    table.add_column("Valor")
    table.add_row("URLs mapeadas", str(len(urls)))
    if added_urls is not None and (added_urls or removed_urls):
        table.add_row("URLs novas", f"[green]+{len(added_urls)}[/green]")
        table.add_row("URLs removidas", f"[bright_black]-{len(removed_urls or [])}[/bright_black]")
    table.add_row("PDFs individuais", str(len(pdf_entries)))
    table.add_row(
        "Paginas sem PDF",
        f"[bold red]{missing_count}[/bold red]" if missing_count else "0",
    )
    table.add_row("Falhas", str(len(failures)))
    table.add_row("Bloqueios do servidor", str(blocks))
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
    """Backup + remocao do manifest E storage_state quando --fresh.

    --fresh deve resetar TUDO: manifest, cookies persistidos (browser_state.json),
    pois usuario pode estar tentando escapar de sessao bloqueada.
    """
    manifest = output_dir / MANIFEST_FILENAME
    state_file = storage_state_path(output_dir)

    if manifest.exists():
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

    # Remove storage_state para forcar nova sessao (importante apos bloqueio)
    if state_file.exists():
        try:
            state_file.unlink()
            console.print("[yellow]Cookies/storage anteriores removidos[/yellow] (--fresh).")
        except OSError as exc:
            console.print(f"[yellow]Aviso: nao foi possivel remover storage_state:[/yellow] {exc}")


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
    proxy: list[str] | None = typer.Option(
        None, "--proxy",
        help="Proxy de saida. Repita a flag (ou separe por virgula) para varios: o "
        "extrator troca de proxy quando um IP e bloqueado. Ex: http://usuario:senha@host:8080, "
        "host:porta, socks5://host:1080. 'direct' = conexao direta.",
    ),
    proxy_file: Path | None = typer.Option(
        None, "--proxy-file",
        help="Arquivo com um proxy por linha (ver 'proxy add'). Usado se --proxy nao for informado.",
    ),
    no_proxy: bool = typer.Option(
        False, "--no-proxy",
        help="Conexao direta: ignora --proxy-file e as variaveis HTTP_PROXY/HTTPS_PROXY.",
    ),
    max_block_wait: float = typer.Option(
        7200.0, "--max-block-wait", min=0.0, max=86400.0,
        help="Espera maxima (s) por UM bloqueio do servidor. O extrator aguarda o ban "
        "acabar e continua sozinho; acima deste limite ele para com o progresso salvo.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Mapeia a arvore mas nao exporta PDFs (estimativa rapida).",
    ),
    debug: bool = typer.Option(
        False, "--debug",
        help="Verbose: ativa DEBUG logging (mais detalhes em run.log).",
    ),
    retry_failed_only: bool = typer.Option(
        False, "--retry-failed-only",
        help="So re-tenta URLs em failures (skip crawl + skip ja exportadas). "
        "Ideal para resume rapido apos bloqueio.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Silencia output (so erros + summary final). Util para CI.",
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

    exits = _resolve_network_exits(console, proxy, proxy_file, no_proxy)

    # --regenerate implica --update (recrawl). --update sozinho so recrawl.
    force_recrawl = update or regenerate
    force_reexport = regenerate

    rate_limit = RateLimitConfig(
        base_delay_seconds=request_delay,
        backoff_initial_seconds=backoff_initial,
        backoff_max_seconds=backoff_max,
        max_block_wait_seconds=max_block_wait,
    )

    exit_code = _launch_pdf_run(
        console,
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
        dry_run=dry_run,
        debug=debug,
        retry_failed_only=retry_failed_only,
        quiet=quiet,
        proxies=exits,
    )
    raise typer.Exit(exit_code)


def _resolve_network_exits(
    console, proxy_args: list[str] | None, proxy_file: Path | None, no_proxy: bool,
) -> list[str | None]:
    """Valida e resolve as saidas de rede da CLI. Sai com codigo 2 se invalido."""
    if no_proxy:
        return [None]
    if proxy_file is not None and not proxy_file.exists():
        console.print(f"[bold red]Arquivo de proxies nao encontrado:[/bold red] {proxy_file}")
        raise typer.Exit(2)
    try:
        exits, origin = resolve_exits(proxy_args, proxy_file, dict(os.environ))
    except ProxyConfigError as exc:
        console.print(f"[bold red]Proxy invalido:[/bold red] {exc}")
        raise typer.Exit(2)
    if origin.startswith("variavel de ambiente"):
        console.print(
            f"[dim]Proxy vindo da {origin}: {mask_proxy(exits[0])}. "
            "Use --no-proxy para ignorar.[/dim]"
        )
    return exits


def _launch_pdf_run(console, **pipeline_kwargs) -> int:
    """Executa o pipeline de PDF tratando Ctrl+C e erros fatais. Retorna o exit code."""
    try:
        return asyncio.run(run_pipeline(**pipeline_kwargs))
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrompido pelo usuario (Ctrl+C).[/bold yellow]")
        console.print("PDFs ja gerados foram preservados em pages/")
        console.print("Rode novamente para continuar de onde parou (resume automatico).")
        return 130
    except Exception as exc:  # noqa: BLE001
        console.print(f"\n[bold red]Erro fatal:[/bold red] {exc}")
        _suggest_action_for_error(console, exc)
        console.print_exception(show_locals=False)
        return 1


_RESUME_MODES = ("auto", "retry-failed", "update", "regenerate")


def _resolve_consolidated_name(console, requested: str | None, data: dict, output_dir: Path) -> str:
    """Nome do consolidado para `resume`: flag > manifest > PDF gerado por nos > padrao."""
    name = (requested or str(data.get("consolidated_name") or "")).strip()
    if not name:
        return _guess_consolidated_name(output_dir)
    if any(c in name for c in '<>:"/\\|?*'):
        console.print(
            f"[bold red]Nome do consolidado contem caracteres invalidos:[/bold red] {name!r}"
        )
        raise typer.Exit(2)
    return name if name.lower().endswith(".pdf") else name + ".pdf"


def _job_label(job) -> str:
    """Caminho do job como o usuario o digitaria (nao so o nome da ultima pasta:
    output\\CFGTRIB e CFGTRIB sao jobs diferentes)."""
    try:
        return str(job.output_dir.resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        return job.display_name


def _jobs_table(found, title: str) -> Table:
    table = Table(title=title, border_style="yellow")
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Pasta", style="bold")
    table.add_column("URL", overflow="fold")
    table.add_column("Mapeadas", justify="right")
    table.add_column("Fila", justify="right")
    table.add_column("PDFs", justify="right")
    table.add_column("Falhas", justify="right", style="red")
    table.add_column("Atualizado")
    for i, job in enumerate(found, start=1):
        status = "" if job.crawl_complete else "[yellow](crawl incompleto)[/yellow] "
        table.add_row(
            str(i),
            f"{status}{_job_label(job)}",
            job.start_url,
            str(job.mapped_count),
            str(job.queue_count),
            str(job.exported_count),
            str(job.failure_count),
            job.last_updated.replace("T", " ").replace("+00:00", "Z"),
        )
    return table


def _pick_job(console, found, title: str) -> Path | None:
    """Mostra os jobs e pergunta qual usar. Devolve a pasta, ou None se cancelado.

    SEMPRE pergunta: escolher sozinho quando ha um unico job dispararia um run
    (requests ao servidor) sem ninguem ter confirmado. Aceita tambem o caminho
    de uma pasta, para jobs fora de output/ e da pasta atual.
    """
    if found:
        console.print(_jobs_table(found, title))
        question = f"Qual job? [1-{len(found)}], ou o caminho de outra pasta (0 = cancelar)"
    else:
        question = "Caminho da pasta do job (0 = cancelar)"
    answer = str(typer.prompt(question, default="0")).strip().strip('"')
    if not answer or answer == "0":
        return None
    if answer.isdigit():
        choice = int(answer)
        return found[choice - 1].output_dir if 1 <= choice <= len(found) else None
    return Path(answer)


_DEFAULT_CONSOLIDATED = "TDN_TOTVS_consolidado.pdf"


def _guess_consolidated_name(output_dir: Path, default: str = _DEFAULT_CONSOLIDATED) -> str:
    """Nome do consolidado de um job antigo (sem o nome gravado no manifest).

    So aceita um PDF da pasta se ele foi GERADO pelo extrator (metadado
    /Producer) e for o unico assim: um PDF qualquer que o usuario guardou na
    pasta nunca pode ser tomado por consolidado e sobrescrito.
    """
    from pypdf import PdfReader  # noqa: PLC0415

    ours: list[str] = []
    try:
        candidates = [p for p in output_dir.glob("*.pdf") if p.is_file()]
    except OSError:
        return default
    for path in candidates:
        try:
            producer = str((PdfReader(str(path)).metadata or {}).get("/Producer", ""))
        except Exception:  # noqa: BLE001 - PDF ilegivel nao e nosso
            continue
        if producer.startswith("Extrator TDN TOTVS"):
            ours.append(path.name)
    return ours[0] if len(ours) == 1 else default


@app.command()
def resume(
    output_dir: Path | None = typer.Option(
        None, "--output-dir",
        help="Pasta do job. Se omitida, lista os jobs encontrados para voce escolher.",
    ),
    mode: str = typer.Option(
        "auto", "--mode",
        help="auto: continua de onde parou | retry-failed: so as paginas que faltaram | "
        "update: re-mapeia e baixa paginas novas | regenerate: refaz todos os PDFs.",
    ),
    headless: bool = typer.Option(True, "--headless/--headed", help="Executa navegador sem interface."),
    timeout_seconds: int = typer.Option(180, min=10, max=1800, help="Limite total por pagina (segundos)."),
    slow_threshold_seconds: float = typer.Option(
        60.0, min=5.0, help="Paginas que demorarem >= este valor vao para slow_pages.log.",
    ),
    request_delay: float = typer.Option(
        2.0, min=0.0, max=60.0, help="Pausa em segundos entre requests (anti-rate-limit).",
    ),
    backoff_initial: float = typer.Option(
        30.0, min=5.0, max=600.0, help="Cooldown inicial apos bloqueio sem Retry-After.",
    ),
    backoff_max: float = typer.Option(
        900.0, min=60.0, max=7200.0, help="Teto do cooldown apos bloqueios sucessivos.",
    ),
    max_workers: int = typer.Option(1, min=1, max=8, help="Workers paralelos para exportar PDFs."),
    consolidated_name: str | None = typer.Option(
        None, "--consolidated-name",
        help="Nome do PDF consolidado. Padrao: o mesmo usado quando o job foi criado.",
    ),
    proxy: list[str] | None = typer.Option(
        None, "--proxy",
        help="Proxy de saida. Repita a flag (ou separe por virgula) para varios: o "
        "extrator troca de proxy quando um IP e bloqueado. Ex: http://usuario:senha@host:8080, "
        "host:porta, socks5://host:1080. 'direct' = conexao direta.",
    ),
    proxy_file: Path | None = typer.Option(
        None, "--proxy-file",
        help="Arquivo com um proxy por linha (ver 'proxy add'). Usado se --proxy nao for informado.",
    ),
    no_proxy: bool = typer.Option(
        False, "--no-proxy",
        help="Conexao direta: ignora --proxy-file e as variaveis HTTP_PROXY/HTTPS_PROXY.",
    ),
    max_block_wait: float = typer.Option(
        7200.0, "--max-block-wait", min=0.0, max=86400.0,
        help="Espera maxima (s) por UM bloqueio do servidor. O extrator aguarda o ban "
        "acabar e continua sozinho; acima deste limite ele para com o progresso salvo.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Verbose: ativa DEBUG logging."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Silencia output (so erros + summary)."),
) -> None:
    """Retoma, completa ou atualiza um job EXISTENTE sem redigitar a URL.

    A URL inicial e lida do manifest.json do job. Sem --output-dir, procura
    jobs em output/ e na pasta atual e pergunta qual usar.
    """
    console = get_console()
    mode = mode.strip().lower()
    if mode not in _RESUME_MODES:
        console.print(
            f"[bold red]Modo invalido:[/bold red] {mode!r}. Use: {', '.join(_RESUME_MODES)}."
        )
        raise typer.Exit(2)

    if output_dir is None:
        include_complete = mode in ("update", "regenerate")
        found = find_jobs(JOB_ROOTS, include_complete=include_complete)
        if not found:
            console.print(
                "[yellow]Nenhum job "
                + ("" if include_complete else "pendente ")
                + "encontrado em output/ nem na pasta atual.[/yellow]\n"
                "Use a opcao [2] do menu para iniciar um trabalho novo, ou informe a "
                "pasta de um job guardado em outro lugar."
            )
        titles = {
            "auto": "Jobs incompletos",
            "retry-failed": "Jobs com paginas faltando",
            "update": "Jobs existentes (verificar atualizacoes)",
            "regenerate": "Jobs existentes (regenerar todos os PDFs)",
        }
        output_dir = _pick_job(console, found, titles[mode])
        if output_dir is None:
            console.print("Nada a fazer.")
            raise typer.Exit(EXIT_NOTHING_TO_DO)

    manifest_path = output_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        console.print(
            f"[bold red]manifest.json nao encontrado em {output_dir}.[/bold red] "
            "Esta pasta nao contem um job; use a opcao [2] para iniciar um novo."
        )
        raise typer.Exit(1)
    try:
        with manifest_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Erro ao ler manifest: {exc}[/red]")
        raise typer.Exit(1)

    try:
        validated_url = validate_start_url(str(data.get("start_url", "")))
    except InvalidStartUrlError as exc:
        console.print(f"[bold red]URL inicial invalida no manifest:[/bold red] {exc}")
        raise typer.Exit(2)

    exits = _resolve_network_exits(console, proxy, proxy_file, no_proxy)
    crawl_complete = bool(data.get("crawl_complete")) and bool(data.get("mapped_urls"))
    # retry-failed pula o mapeamento; se ele nao terminou, retoma normalmente
    # (primeiro completa o mapa, depois exporta o que falta).
    retry_failed_only = mode == "retry-failed" and crawl_complete
    max_pages = data.get("crawl_max_pages")
    max_pages = max_pages if isinstance(max_pages, int) and max_pages > 0 else None

    console.print(
        f"[bold]Job:[/bold] {output_dir}  |  [bold]Modo:[/bold] {mode}\n"
        f"[bold]URL:[/bold] {validated_url}"
    )
    rate_limit = RateLimitConfig(
        base_delay_seconds=request_delay,
        backoff_initial_seconds=backoff_initial,
        backoff_max_seconds=backoff_max,
        max_block_wait_seconds=max_block_wait,
    )
    exit_code = _launch_pdf_run(
        console,
        start_url=validated_url,
        output_dir=output_dir,
        consolidated_name=_resolve_consolidated_name(console, consolidated_name, data, output_dir),
        headless=headless,
        timeout_seconds=timeout_seconds,
        max_pages=max_pages,
        slow_threshold_seconds=slow_threshold_seconds,
        auto_confirm=True,
        force_recrawl=mode in ("update", "regenerate"),
        force_reexport=mode == "regenerate",
        rate_limit=rate_limit,
        max_workers=max_workers,
        dry_run=False,
        debug=debug,
        retry_failed_only=retry_failed_only,
        quiet=quiet,
        proxies=exits,
    )
    raise typer.Exit(exit_code)


async def markdown_pipeline(
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
    proxy: str | None = None,
    dry_run: bool = False,
    debug: bool = False,
    quiet: bool = False,
    proxies: list[str | None] | None = None,
) -> int:
    pages_dir, log_file = ensure_output_dirs(output_dir)
    correlation_id = generate_correlation_id()
    logger = setup_logger(log_file, debug=debug, correlation_id=correlation_id)
    console = get_console()
    if quiet:
        console.quiet = True
        import logging as _lg
        for handler in list(logger.handlers):
            if "Rich" in type(handler).__name__:
                logger.removeHandler(handler)
        logger.setLevel(_lg.WARNING)
    logger.info("Run Markdown iniciado: correlation_id=%s", correlation_id)

    job_lock = JobLock(output_dir)
    try:
        job_lock.acquire()
    except JobLockError as exc:
        console.print(f"[bold red]Lock conflict:[/bold red] {exc}")
        return 2
    try:
        pool = await _build_checked_pool(proxies, proxy, start_url, logger, console)
        return await _markdown_pipeline_inner(
            start_url, output_dir, consolidated_name, headless, timeout_seconds,
            max_pages, slow_threshold_seconds, auto_confirm, force_recrawl,
            force_reexport, rate_limit, proxy, dry_run,
            pages_dir, log_file, logger, console, pool,
        )
    except BlockedTooLongError as exc:
        logger.error("Run interrompido: %s", exc)
        _print_blocked_panel(console, exc, output_dir)
        return EXIT_INCOMPLETE
    except AllProxiesDeadError as exc:
        logger.error("Run interrompido: %s", exc)
        console.print(f"\n[bold red]Proxies indisponiveis:[/bold red] {exc}")
        return EXIT_INCOMPLETE
    except ProxyConfigError as exc:
        console.print(f"[bold red]Proxy invalido:[/bold red] {exc}")
        return 2
    finally:
        job_lock.release()


async def _markdown_pipeline_inner(
    start_url, output_dir, consolidated_name, headless, timeout_seconds,
    max_pages, slow_threshold_seconds, auto_confirm, force_recrawl,
    force_reexport, rate_limit, proxy, dry_run,
    pages_dir, log_file, logger, console, pool=None,
) -> int:
    """Pipeline interno (encapsulado no lock): crawl (reusado) + export Markdown + merge."""
    timeout_ms = timeout_seconds * 1000
    pipeline_start = time.monotonic()
    if pool is None:
        pool = _build_pool(None, proxy)

    checkpoint = _setup_checkpoint(output_dir, start_url, pages_dir, logger)
    if checkpoint.previous_start_url:
        return _abort_start_url_mismatch(
            console, logger, output_dir, start_url, checkpoint.previous_start_url,
        )
    if checkpoint.manifest.start_url and checkpoint.manifest.start_url != start_url:
        start_url = checkpoint.manifest.start_url
    resume_active = bool(
        checkpoint.manifest.crawl_complete and checkpoint.manifest.mapped_urls
    )
    resume_md = len(checkpoint.manifest.exported_md)

    force_recrawl = _maybe_force_recrawl_for_max_pages(
        force_recrawl, checkpoint, max_pages, logger,
    )

    resume_line = (
        f"\n[bold yellow]Resume:[/bold yellow] {resume_md} paginas Markdown ja existem"
        if resume_active else ""
    )
    console.print(
        Panel.fit(
            f"[bold]URL inicial:[/bold] {start_url}\n"
            f"[bold]Saida:[/bold] {output_dir / 'markdown'}\n"
            f"[bold]Timeout:[/bold] {timeout_seconds}s | "
            f"[bold]Slow:[/bold] >={slow_threshold_seconds:.0f}s | "
            f"[bold]Headless:[/bold] {headless} | "
            f"[bold]Max:[/bold] {max_pages or 'sem limite'}"
            f"{resume_line}",
            title="Extrator TDN TOTVS - Markdown",
            border_style="green",
        )
    )

    logger.info("URL inicial: %s", start_url)
    logger.info("Diretorio de saida: %s", output_dir)
    if resume_active:
        logger.info(
            "Resume detectado: %d URLs mapeadas, %d paginas Markdown ja exportadas",
            len(checkpoint.manifest.mapped_urls), resume_md,
        )

    # Compartilha RateLimiter entre crawl e export (cooldown propaga entre fases).
    shared_limiter = RateLimiter(rate_limit, logger)
    state_path = storage_state_path(output_dir)
    if len(pool) > 1:
        logger.info(
            "Saidas de rede (%d, com rodizio em bloqueio): %s",
            len(pool), pool.summary(),
        )
    elif pool.current:
        logger.info("Proxy configurado: %s", pool.current_label)

    try:
        crawl = await crawl_confluence_tree(
            start_url=start_url,
            checkpoint=checkpoint,
            logger=logger,
            headless=headless,
            timeout_ms=timeout_ms,
            max_pages=max_pages,
            slow_threshold_seconds=slow_threshold_seconds,
            force_recrawl=force_recrawl,
            rate_limit=rate_limit,
            max_workers=1,
            limiter=shared_limiter,
            state_path=state_path,
            proxy=proxy,
            pool=pool,
        )
    except KeyboardInterrupt:
        logger.warning("Crawl interrompido pelo usuario (Ctrl+C).")
        raise
    urls, slow_crawl = crawl.urls, crawl.slow_records

    if not urls:
        logger.error("Nenhuma pagina foi encontrada a partir da URL inicial.")
        return 1

    if dry_run:
        _print_dry_run_summary(console, logger, urls, output_dir)
        return 0

    if not _confirm_large_batch(urls, auto_confirm, logger, console):
        logger.info("Usuario cancelou antes da exportacao.")
        return 0

    try:
        md_entries, failures, slow_md = await export_pages_to_markdown(
            urls=urls,
            output_dir=output_dir,
            checkpoint=checkpoint,
            logger=logger,
            headless=headless,
            timeout_ms=timeout_ms,
            slow_threshold_seconds=slow_threshold_seconds,
            force_reexport=force_reexport,
            rate_limit=rate_limit,
            limiter=shared_limiter,
            state_path=state_path,
            proxy=proxy,
            pool=pool,
        )
    except KeyboardInterrupt:
        logger.warning("Exportacao Markdown interrompida pelo usuario (Ctrl+C).")
        raise

    try:
        checkpoint.flush()
    except OSError as exc:
        logger.warning("Falha ao flush manifest: %s", exc)

    if not md_entries:
        logger.error("Nenhuma pagina Markdown foi gerada.")
        return 1

    consolidated_path: Path | None = output_dir / "markdown" / consolidated_name
    try:
        merge_markdown(md_entries, consolidated_path, logger)
    except Exception:  # noqa: BLE001
        logger.exception("Falha ao consolidar Markdown")
        consolidated_path = None

    all_slow = slow_crawl + slow_md
    slow_log_path = _maybe_write_slow_log(all_slow, output_dir, logger)

    total_elapsed = time.monotonic() - pipeline_start
    logger.info(
        "Resumo Markdown: %s URL(s), %s pagina(s) MD, %s falha(s), %s lenta(s), tempo total %s",
        len(urls), len(md_entries), len(failures), len(all_slow),
        format_duration(total_elapsed),
    )

    incomplete = bool(failures) or not crawl.complete
    failures_line = (
        f"\n[bold red]Falhas:[/bold red] {len(failures)} pagina(s) ficaram de fora "
        "(veja run.log). Rode o mesmo comando de novo para gerar so as que faltam."
        if failures else ""
    )
    if not crawl.complete:
        failures_line += (
            f"\n[bold red]Mapeamento incompleto[/bold red] ({crawl.note}): "
            "rode de novo para retomar."
        )
    slow_line = f"\n[bold]Slow log:[/bold] {slow_log_path}" if slow_log_path else ""
    console.print(
        f"\n[bold green]Concluido:[/bold green] {len(md_entries)}/{len(urls)} paginas "
        f"Markdown em {format_duration(total_elapsed)}.\n"
        f"[bold]Consolidado:[/bold] {consolidated_path if consolidated_path else '[red]falhou[/red]'}\n"
        f"[bold]Log:[/bold] {log_file}"
        f"{slow_line}{failures_line}"
    )

    if not consolidated_path:
        return 2
    return EXIT_INCOMPLETE if incomplete else 0


@app.command(name="markdown")
def markdown_cmd(
    start_url: str = typer.Argument(..., help="URL inicial da documentacao Confluence/TDN."),
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta de saida (compartilha manifest.json com `run`).",
    ),
    consolidated_name: str = typer.Option(
        "TDN_TOTVS_consolidado.md",
        help="Nome do Markdown final consolidado (salvo em <output_dir>/markdown/).",
    ),
    headless: bool = typer.Option(True, "--headless/--headed", help="Executa navegador sem interface."),
    timeout_seconds: int = typer.Option(
        180, min=10, max=1800, help="Limite total por pagina (segundos).",
    ),
    slow_threshold_seconds: float = typer.Option(
        60.0, min=5.0,
        help="Paginas que demorarem >= este valor sao registradas em slow_pages.log.",
    ),
    max_pages: int | None = typer.Option(None, min=1, max=100_000, help="Limite de paginas."),
    yes: bool = typer.Option(False, "--yes/--no-yes", "-y", help="Pula confirmacoes interativas."),
    fresh: bool = typer.Option(False, "--fresh", help="Ignora manifest.json e comeca do zero."),
    update: bool = typer.Option(
        False, "--update", help="Re-mapeia a arvore para detectar paginas novas.",
    ),
    regenerate: bool = typer.Option(
        False, "--regenerate", help="Re-mapeia + regenera todos os arquivos Markdown.",
    ),
    request_delay: float = typer.Option(
        2.0, min=0.0, max=60.0, help="Pausa em segundos entre requests (anti-rate-limit).",
    ),
    backoff_initial: float = typer.Option(
        30.0, min=5.0, max=600.0,
        help="Cooldown inicial em segundos apos detectar bloqueio (5xx/429/timeout).",
    ),
    backoff_max: float = typer.Option(
        900.0, min=60.0, max=7200.0, help="Teto do cooldown apos bloqueios sucessivos.",
    ),
    proxy: list[str] | None = typer.Option(
        None, "--proxy",
        help="Proxy de saida. Repita a flag (ou separe por virgula) para varios.",
    ),
    proxy_file: Path | None = typer.Option(
        None, "--proxy-file", help="Arquivo com um proxy por linha.",
    ),
    no_proxy: bool = typer.Option(
        False, "--no-proxy", help="Conexao direta: ignora arquivo e variaveis de ambiente.",
    ),
    max_block_wait: float = typer.Option(
        7200.0, "--max-block-wait", min=0.0, max=86400.0,
        help="Espera maxima (s) por um bloqueio do servidor antes de parar.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Mapeia a arvore mas nao exporta Markdown.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Verbose: ativa DEBUG logging."),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Silencia output (so erros + summary final).",
    ),
) -> None:
    """Mapeia a arvore do Confluence e exporta para Markdown consolidado com indice.

    Baixa imagens/anexos referenciados em <output_dir>/markdown/attachments/ e
    reescreve os links para caminhos locais relativos. Compartilha o crawl e o
    manifest.json com o comando `run` (PDF); o resume do Markdown e independente
    (namespace `exported_md`), entao rodar os dois formatos no mesmo output_dir
    e seguro — nenhum interfere no checkpoint do outro.
    """
    console = get_console()

    try:
        validated_url = validate_start_url(start_url)
    except InvalidStartUrlError as exc:
        console.print(f"[bold red]URL invalida:[/bold red] {exc}")
        raise typer.Exit(2)

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
    if not consolidated_name.lower().endswith(".md"):
        consolidated_name = consolidated_name + ".md"

    if fresh:
        _handle_fresh_flag(output_dir, validated_url, console)

    if not _check_disk_space(output_dir, console):
        console.print("[red]Cancelado por falta de espaco.[/red]")
        raise typer.Exit(2)

    exits = _resolve_network_exits(console, proxy, proxy_file, no_proxy)

    force_recrawl = update or regenerate
    force_reexport = regenerate

    rate_limit = RateLimitConfig(
        base_delay_seconds=request_delay,
        backoff_initial_seconds=backoff_initial,
        backoff_max_seconds=backoff_max,
        max_block_wait_seconds=max_block_wait,
    )

    try:
        exit_code = asyncio.run(
            markdown_pipeline(
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
                proxies=exits,
                dry_run=dry_run,
                debug=debug,
                quiet=quiet,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[bold yellow]Interrompido pelo usuario (Ctrl+C).[/bold yellow]")
        console.print("Paginas Markdown ja geradas foram preservadas em markdown/pages/")
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
            "este IP (rate limit do Cloudflare, erro 1015).\n"
            "  1. Retome o job pela opcao [1] do menu: o extrator espera o bloqueio "
            "acabar e continua de onde parou.\n"
            "  2. Configure proxies na opcao [P]: com mais de uma saida de rede o "
            "extrator troca de IP quando um e bloqueado.\n"
            "  3. Se o bloqueio for frequente, aumente --request-delay (ex: 5).\n"
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


proxy_app = typer.Typer(
    add_completion=False, no_args_is_help=True,
    help="Gerencia a lista de proxies usada com --proxy-file (padrao: proxies.txt).",
)
app.add_typer(proxy_app, name="proxy")

_PROXY_FILE_OPTION = typer.Option(
    Path(PROXY_FILE_DEFAULT), "--file", help="Arquivo da lista de proxies.",
)


def _load_proxy_list_or_exit(console, file: Path) -> list[str | None]:
    if not file.exists():
        return []
    try:
        return load_proxy_file(file)
    except ProxyConfigError as exc:
        console.print(f"[bold red]Lista de proxies invalida:[/bold red] {exc}")
        raise typer.Exit(2)


@proxy_app.command("list")
def proxy_list(file: Path = _PROXY_FILE_OPTION) -> None:
    """Mostra os proxies configurados (credenciais mascaradas)."""
    console = get_console()
    exits = _load_proxy_list_or_exit(console, file)
    if not exits:
        console.print(
            f"[yellow]Nenhum proxy configurado[/yellow] ({file} nao existe ou esta vazio). "
            "O extrator usa a conexao direta."
        )
        return
    table = Table(title=f"Proxies em {file}", border_style="cyan")
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Saida de rede")
    shown = exits[:20]
    for i, item in enumerate(shown, start=1):
        table.add_row(str(i), mask_proxy(item))
    if len(exits) > len(shown):
        table.add_row("...", f"(+{len(exits) - len(shown)} proxies)")
    console.print(table)
    if len(exits) > 1:
        console.print("Com mais de uma saida, o extrator troca de IP quando uma e bloqueada.")


@proxy_app.command("add")
def proxy_add(
    url: str | None = typer.Argument(
        None, help="Proxy a adicionar. Se omitido, pergunta (evita problemas com caracteres especiais no terminal).",
    ),
    file: Path = _PROXY_FILE_OPTION,
) -> None:
    """Adiciona um proxy a lista. Aceita http://usuario:senha@host:porta, host:porta,
    host:porta:usuario:senha, socks5://host:porta ou 'direct' (conexao direta)."""
    console = get_console()
    if url is None:
        url = typer.prompt("Proxy (ex: http://usuario:senha@host:8080 ou host:porta)")
    try:
        normalized = normalize_proxy_url(url)
    except ProxyConfigError as exc:
        console.print(f"[bold red]Proxy invalido:[/bold red] {exc}")
        raise typer.Exit(2)
    if normalized is None and url.strip().lower() not in ("direct", "direto"):
        console.print("[yellow]Nada informado.[/yellow]")
        raise typer.Exit(0)
    exits = _load_proxy_list_or_exit(console, file)
    if normalized in exits:
        console.print(f"[yellow]Ja estava na lista:[/yellow] {mask_proxy(normalized)}")
        return
    exits.append(normalized)
    try:
        save_proxy_file(file, exits)
    except OSError as exc:
        console.print(f"[red]Erro ao gravar {file}: {exc}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]Adicionado:[/green] {mask_proxy(normalized)}  "
        f"({len(exits)} saida(s) em {file})"
    )


@proxy_app.command("clear")
def proxy_clear(
    file: Path = _PROXY_FILE_OPTION,
    yes: bool = typer.Option(False, "--yes", "-y", help="Pula confirmacao."),
) -> None:
    """Remove a lista de proxies (o extrator volta a usar a conexao direta)."""
    console = get_console()
    if not file.exists():
        console.print("[green]Nenhum proxy configurado.[/green]")
        return
    if not yes and sys.stdin.isatty() and not typer.confirm(f"Apagar {file}?", default=False):
        console.print("Cancelado.")
        return
    try:
        file.unlink()
    except OSError as exc:
        console.print(f"[red]Erro ao remover {file}: {exc}[/red]")
        raise typer.Exit(1)
    console.print("[green]Lista de proxies removida.[/green] Conexao direta em uso.")


async def _probe_exits(exits: list[str | None], url: str, timeout_ms: int) -> list[dict]:
    """Abre `url` por cada saida de rede e reporta status/IP visto pelo servidor."""
    from playwright.async_api import Error as PlaywrightError  # noqa: PLC0415
    from playwright.async_api import async_playwright  # noqa: PLC0415

    from .blocking import classify_response, short_error  # noqa: PLC0415

    results: list[dict] = []
    async with async_playwright() as playwright:
        for item in exits:
            entry = {"exit": mask_proxy(item), "status": None, "ip": "", "detail": "", "ok": False}
            started = time.monotonic()
            browser = None
            try:
                launch_kwargs: dict = {"headless": True, "channel": "chromium"}
                settings = to_playwright_proxy(item)
                if settings:
                    launch_kwargs["proxy"] = settings
                browser = await playwright.chromium.launch(**launch_kwargs)
                context = await browser.new_context(
                    user_agent=chrome_user_agent(browser.version),
                )
                page = await context.new_page()
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = response.status if response is not None else None
                try:
                    text = await response.text() if response is not None else ""
                except PlaywrightError:
                    text = ""
                match = re.search(r"^ip=(.+)$", text, re.MULTILINE)
                block = classify_response(status, response.headers if response else {}, body=text[:4000])
                entry.update(
                    status=status,
                    ip=match.group(1).strip() if match else "",
                    ok=status is not None and status < 400 and block is None,
                    detail=block.describe() if block else "",
                )
            except PlaywrightError as exc:
                entry["detail"] = short_error(exc)
            finally:
                if browser is not None:
                    try:
                        await browser.close()
                    except PlaywrightError:
                        pass
            entry["seconds"] = round(time.monotonic() - started, 1)
            results.append(entry)
    return results


def _proxy_test_bulk(console, file: Path, exits: list[str | None], url: str,
                     prune: bool | None) -> None:
    """Teste em massa (lista grande): resumo, os mais rapidos e limpeza opcional."""
    candidates = [item for item in exits if item is not None]
    console.print(
        f"Testando {len(candidates)} proxies em {url} "
        f"(no maximo ~{format_duration(estimate_seconds(len(candidates)))})..."
    )
    results = asyncio.run(
        probe_many(candidates, url, on_progress=_progress_printer(console, "testados"))
    )
    working = rank_working(results)
    reasons: dict[str, int] = {}
    for item in results:
        if not item.ok:
            reasons[item.detail] = reasons.get(item.detail, 0) + 1
    console.print(
        f"\n[bold]{len(working)} de {len(candidates)} proxies responderam[/bold] "
        f"em ate {PROBE_TIMEOUT_SECONDS:.0f}s."
    )
    for detail, count in sorted(reasons.items(), key=lambda pair: -pair[1])[:6]:
        console.print(f"[dim]  {count:>5} x {detail}[/dim]")
    if not working:
        console.print("[red]Nenhum proxy utilizavel nesta lista.[/red]")
        raise typer.Exit(1)
    table = Table(title="Proxies mais rapidos", border_style="cyan")
    table.add_column("Saida")
    table.add_column("Tempo", justify="right")
    table.add_column("IP visto pelo servidor")
    for item in working[:15]:
        table.add_row(item.label, f"{item.seconds:.1f}s", item.ip or "-")
    console.print(table)
    console.print(
        "[dim]Responder aqui nao garante que o IP esteja livre de bloqueio (erro 1015) "
        "nas paginas da documentacao.[/dim]"
    )
    if prune is None:
        prune = sys.stdin.isatty() and typer.confirm(
            f"Regravar {file} so com os {len(working)} que responderam "
            f"(original salvo em {file}.bak)?", default=True,
        )
    if not prune:
        return
    backup = file.with_name(file.name + ".bak")
    kept = _with_direct([item.proxy for item in working], exits)
    try:
        shutil.copyfile(file, backup)
        save_proxy_file(file, kept)
    except OSError as exc:
        console.print(f"[red]Erro ao regravar {file}: {exc}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]{file} agora tem {len(working)} proxies[/green] (do mais rapido ao "
        f"mais lento). Lista original: {backup}"
    )


@proxy_app.command("test")
def proxy_test(
    file: Path = _PROXY_FILE_OPTION,
    url: str = typer.Option(
        "https://tdn.totvs.com/cdn-cgi/trace", "--url",
        help="Endereco aberto no teste (1 request por saida de rede).",
    ),
    timeout_seconds: int = typer.Option(30, min=5, max=120, help="Timeout por saida."),
    prune: bool | None = typer.Option(
        None, "--prune/--no-prune",
        help="Lista grande: regrava o arquivo so com os proxies que responderam "
             "(original salvo em <arquivo>.bak). Sem a flag, pergunta.",
    ),
) -> None:
    """Testa cada proxy da lista (e a conexao direta, se nao houver lista):
    faz UM request por saida e mostra o status e o IP visto pelo servidor.

    E um teste de CONECTIVIDADE: o endereco padrao e servido pela borda do
    Cloudflare e pode responder 200 mesmo para um IP que esteja banido (erro
    1015) nas paginas da documentacao.

    Listas grandes sao testadas em paralelo, sem browser, e podem ser
    reduzidas aos proxies que respondem (--prune).
    """
    console = get_console()
    exits = _load_proxy_list_or_exit(console, file) or [None]
    if sum(1 for item in exits if item is not None) >= _TRIAGE_MIN_PROXIES:
        _proxy_test_bulk(console, file, exits, url, prune)
        return
    console.print(f"Testando {len(exits)} saida(s) de rede em {url} ...")
    try:
        results = asyncio.run(_probe_exits(exits, url, timeout_seconds * 1000))
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Falha no teste:[/bold red] {exc}")
        _suggest_action_for_error(console, exc)
        raise typer.Exit(1)
    table = Table(title="Teste das saidas de rede", border_style="cyan")
    table.add_column("Saida")
    table.add_column("Resultado")
    table.add_column("HTTP", justify="right")
    table.add_column("IP visto pelo servidor")
    table.add_column("Tempo", justify="right")
    table.add_column("Detalhe", overflow="fold")
    for item in results:
        table.add_row(
            item["exit"],
            "[green]OK[/green]" if item["ok"] else "[red]FALHOU[/red]",
            str(item["status"] or "-"),
            item["ip"] or "-",
            f"{item['seconds']}s",
            item["detail"],
        )
    console.print(table)
    console.print(
        "[dim]OK = a saida conecta e chega ao servidor. Este teste nao garante que o IP "
        "esteja livre de bloqueio (erro 1015) nas paginas da documentacao.[/dim]"
    )
    if not all(item["ok"] for item in results):
        raise typer.Exit(1)


@app.command()
def jobs(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta raiz onde procurar jobs incompletos.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Saida em JSON (para scripts/integracao).",
    ),
    all_jobs: bool = typer.Option(
        False, "--all", help="Lista tambem os jobs ja concluidos.",
    ),
) -> None:
    """Lista jobs incompletos detectados em pastas com manifest.json."""
    console = get_console()
    # Com a pasta padrao, procura tambem na pasta atual: um job criado fora de
    # output/ (pasta digitada sem o prefixo) tambem precisa aparecer.
    roots = list(JOB_ROOTS) if output_dir == Path("output") else [output_dir]
    pending = find_jobs(roots, include_complete=all_jobs)

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

    where = "output/ e na pasta atual" if len(roots) > 1 else f"{output_dir}/"
    title = ("Jobs" if all_jobs else "Jobs incompletos") + f" em {where}"
    console.print(_jobs_table(pending, title))
    console.print(
        "\nPara continuar um job: opcao [1] do menu, ou "
        "`python -m src.main resume --output-dir <pasta>` (a URL e lida do job)."
    )


@app.command()
def status(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta do job para inspecionar.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Saida em JSON.",
    ),
) -> None:
    """Mostra snapshot detalhado de um job (mapeadas, exportadas, falhas, disco)."""
    console = get_console()
    manifest_path = output_dir / MANIFEST_FILENAME
    pages_dir = output_dir / "pages"

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

    mapped = data.get("mapped_urls", []) or []
    exported = data.get("exported", {}) or {}
    failures_data = data.get("failures", {}) or {}
    queue = data.get("crawl_queue", []) or []
    crawl_complete = bool(data.get("crawl_complete", False))

    # Conta PDFs reais no disco
    pdf_count = 0
    total_bytes = 0
    if pages_dir.exists():
        for p in pages_dir.glob("*.pdf"):
            if not p.name.endswith(".tmp"):
                pdf_count += 1
                try:
                    total_bytes += p.stat().st_size
                except OSError:
                    pass

    progress_pct = (len(exported) / len(mapped) * 100) if mapped else 0.0

    payload = {
        "output_dir": str(output_dir),
        "start_url": data.get("start_url", ""),
        "crawl_complete": crawl_complete,
        "mapped_count": len(mapped),
        "queue_remaining": len(queue),
        "exported_count": len(exported),
        "failures_count": len(failures_data),
        "pending_count": max(0, len(mapped) - len(exported) - len(failures_data)),
        "pdfs_on_disk": pdf_count,
        "total_bytes_on_disk": total_bytes,
        "progress_pct": round(progress_pct, 1),
        "last_updated": data.get("last_updated", ""),
    }

    if json_output:
        typer.echo(_json.dumps(payload, indent=2, ensure_ascii=False))
        return

    table = Table(title=f"Status: {output_dir.name}", border_style="cyan")
    table.add_column("Metrica", style="bold cyan")
    table.add_column("Valor")
    table.add_row("URL", payload["start_url"])
    table.add_row(
        "Crawl",
        "[green]completo[/green]" if crawl_complete else "[yellow]incompleto[/yellow]",
    )
    table.add_row("URLs mapeadas", str(payload["mapped_count"]))
    table.add_row("Na fila", str(payload["queue_remaining"]))
    table.add_row("PDFs exportados", str(payload["exported_count"]))
    table.add_row("Falhas", str(payload["failures_count"]))
    table.add_row("Pendentes", str(payload["pending_count"]))
    table.add_row("PDFs no disco", f"{pdf_count} ({format_bytes(total_bytes)})")
    table.add_row("Progresso", f"{progress_pct:.1f}%")
    table.add_row(
        "Ultimo update",
        payload["last_updated"].replace("T", " ").replace("+00:00", "Z"),
    )
    console.print(table)


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


@app.command()
def reexport(
    pages: list[int] = typer.Argument(
        ..., help="Numeros das paginas a refazer: o prefixo NNNN do arquivo em pages/ "
        "(ex: 110 para 0110-titulo.pdf).",
    ),
    output_dir: Path = typer.Option(
        Path("output"), "--output-dir", help="Pasta do job (com manifest.json).",
    ),
) -> None:
    """Marca paginas ja exportadas para serem geradas de novo.

    Para quando um PDF saiu incompleto (ex: sem imagens por causa de um
    bloqueio em runs antigos). So o manifest e alterado, com backup: o PDF
    atual fica no disco ate ser substituido. Depois use a opcao [F] do menu
    (ou `resume --mode retry-failed`) para gera-las.
    """
    console = get_console()
    manifest_path = output_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        console.print(f"[red]manifest.json nao encontrado em {output_dir}[/red]")
        raise typer.Exit(1)
    try:
        with manifest_path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Erro ao ler manifest: {exc}[/red]")
        raise typer.Exit(1)

    numbers = list(dict.fromkeys(pages))
    invalid = [n for n in numbers if n < 1 or n > 9999]
    if invalid:
        console.print(
            "[bold red]Numero(s) invalido(s):[/bold red] "
            + ", ".join(str(n) for n in invalid)
        )
        raise typer.Exit(2)

    job_lock = JobLock(output_dir)
    try:
        job_lock.acquire()
    except JobLockError as exc:
        console.print(f"[bold red]Lock conflict:[/bold red] {exc}")
        raise typer.Exit(2)
    marked: list[tuple[str, str]] = []
    not_found: list[int] = []
    try:
        checkpoint = Checkpoint(output_dir, str(data.get("start_url", "")))
        exported = checkpoint.manifest.exported
        for number in numbers:
            # O numero e o prefixo do ARQUIVO: apos um re-mapeamento a posicao
            # da pagina no mapa pode ser outra, mas o nome do arquivo nao muda.
            prefix = f"{number:04d}-"
            keys = [
                url for url, entry in exported.items()
                if str((entry or {}).get("filename", "")).startswith(prefix)
            ]
            if not keys:
                not_found.append(number)
                continue
            for key in keys:
                marked.append((str(exported.pop(key).get("filename", "")), key))
        if marked:
            backup = checkpoint.backup()
            checkpoint.save()
            if backup:
                console.print(f"[dim]Backup do manifest: {backup}[/dim]")
    finally:
        job_lock.release()

    for number in not_found:
        console.print(
            f"[yellow]{number:04d}: nenhum PDF registrado com esse prefixo "
            f"em {output_dir / 'pages'}.[/yellow]"
        )
    if not marked:
        console.print("Nada foi alterado.")
        raise typer.Exit(2)
    for filename, url in marked:
        console.print(f"  [cyan]{filename}[/cyan] sera gerado de novo\n      {url}")
    console.print(
        f"\n[green]{len(marked)} pagina(s) marcada(s).[/green] Agora use a opcao [F] do menu "
        f"ou: python -m src.main resume --output-dir \"{output_dir}\" --mode retry-failed"
    )


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


@app.command()
def verify(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta do job para verificar.",
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Saida em JSON.",
    ),
    limit: int = typer.Option(
        0, help="Limite de PDFs a verificar (0 = todos). Util para amostragem rapida.",
    ),
) -> None:
    """Verifica integridade dos PDFs comparando SHA-256 com o manifest."""
    console = get_console()
    manifest_path = output_dir / MANIFEST_FILENAME
    pages_dir = output_dir / "pages"

    if not manifest_path.exists():
        console.print(f"[red]manifest.json nao encontrado em {output_dir}[/red]")
        raise typer.Exit(1)

    import json as _json
    from .utils import sha256_file
    try:
        with manifest_path.open("r", encoding="utf-8") as fp:
            data = _json.load(fp)
    except (OSError, ValueError) as exc:
        console.print(f"[red]Erro ao ler manifest: {exc}[/red]")
        raise typer.Exit(1)

    exported = data.get("exported", {}) or {}
    if not exported:
        if json_output:
            typer.echo(_json.dumps({"summary": {"total": 0}, "message": "manifest sem exports"}))
        else:
            console.print("[yellow]Manifest sem PDFs exportados. Nada para verificar.[/yellow]")
        return

    # Limit (amostragem)
    if limit > 0:
        exported = dict(list(exported.items())[:limit])
    results = {"ok": [], "mismatch": [], "missing": [], "skipped": [], "io_error": []}

    for url, entry in exported.items():
        filename = entry.get("filename", "")
        expected_hash = entry.get("sha256", "")
        pdf_path = pages_dir / filename

        if not pdf_path.exists():
            results["missing"].append({"url": url, "filename": filename})
            continue
        if not expected_hash:
            results["skipped"].append({"url": url, "filename": filename})
            continue
        try:
            actual = sha256_file(pdf_path)
        except OSError as exc:
            results["io_error"].append({"url": url, "filename": filename, "error": str(exc)})
            continue
        if actual == expected_hash:
            results["ok"].append(filename)
        else:
            results["mismatch"].append({
                "url": url, "filename": filename,
                "expected": expected_hash, "actual": actual,
            })

    summary = {
        "total": len(exported),
        "ok": len(results["ok"]),
        "mismatch": len(results["mismatch"]),
        "missing": len(results["missing"]),
        "skipped_no_hash": len(results["skipped"]),
        "io_error": len(results["io_error"]),
    }

    if json_output:
        typer.echo(_json.dumps({"summary": summary, **results}, indent=2, ensure_ascii=False))
        return

    table = Table(title=f"Integridade: {output_dir.name}", border_style="cyan")
    table.add_column("Categoria", style="bold cyan")
    table.add_column("Contagem", justify="right")
    table.add_row("[green]OK[/green]", str(summary["ok"]))
    table.add_row("[red]Corrompido (hash mismatch)[/red]", str(summary["mismatch"]))
    table.add_row("[yellow]Arquivo faltando[/yellow]", str(summary["missing"]))
    table.add_row("[dim]Sem hash no manifest[/dim]", str(summary["skipped_no_hash"]))
    table.add_row("[yellow]Erro de IO[/yellow]", str(summary["io_error"]))
    table.add_row("[bold]Total verificado[/bold]", str(summary["total"]))
    console.print(table)

    if results["mismatch"]:
        console.print("\n[bold red]PDFs corrompidos (hash mismatch):[/bold red]")
        for item in results["mismatch"][:20]:
            console.print(f"  {item['filename']}")
        if len(results["mismatch"]) > 20:
            console.print(f"  ... e mais {len(results['mismatch']) - 20}")
        console.print("\n[yellow]Acao sugerida:[/yellow] re-exporte com --regenerate")
        raise typer.Exit(2)

    if results["missing"]:
        console.print("\n[bold yellow]PDFs faltando (vai re-gerar no proximo run):[/bold yellow]")
        for item in results["missing"][:20]:
            console.print(f"  {item['filename']}")


@app.command(name="clean-tmp")
def clean_tmp(
    output_dir: Path = typer.Option(
        Path("output"), help="Pasta do job (com pages/).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Pula confirmacao."),
) -> None:
    """Remove arquivos .pdf.tmp orfaos (de runs interrompidos)."""
    console = get_console()
    pages_dir = output_dir / "pages"
    if not pages_dir.exists():
        console.print(f"[yellow]pages/ nao existe em {output_dir}[/yellow]")
        return
    if not pages_dir.is_dir():
        console.print(f"[red]pages/ existe mas nao eh diretorio: {pages_dir}[/red]")
        raise typer.Exit(1)

    # Captura tanto *.pdf.tmp na raiz quanto em subdiretorios (recursivo)
    tmp_files = list(pages_dir.glob("*.pdf.tmp")) + list(pages_dir.glob("**/*.pdf.tmp"))
    # Dedup (glob com ** pode pegar mesmo arquivo)
    tmp_files = list(dict.fromkeys(tmp_files))
    if not tmp_files:
        console.print("[green]Nenhum arquivo .pdf.tmp orfao encontrado.[/green]")
        return

    table = Table(title=f"Arquivos .tmp orfaos em {pages_dir}", border_style="yellow")
    table.add_column("Arquivo")
    table.add_column("Tamanho", justify="right")
    total = 0
    for p in tmp_files:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        total += sz
        table.add_row(p.name, format_bytes(sz))
    console.print(table)
    console.print(f"\nTotal: {format_bytes(total)} em {len(tmp_files)} arquivo(s)")

    if not yes and sys.stdin.isatty() and not typer.confirm("Remover?", default=False):
        console.print("Cancelado.")
        return

    removed = 0
    for p in tmp_files:
        try:
            p.unlink()
            removed += 1
        except OSError as exc:
            console.print(f"[red]Erro ao remover {p.name}: {exc}[/red]")
    console.print(f"[green]{removed} .tmp removidos.[/green]")


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


def _percentiles(values: list[float]) -> dict[str, float]:
    """Calcula p50/p95/p99 sem dependencia externa (numpy etc).

    Filtra valores negativos (sentinelas de erro) e zero (sem dado).
    Retorna 0.0 para todos se lista vazia/invalida.
    """
    # Filtra apenas valores positivos validos
    clean = [v for v in values if v > 0]
    if not clean:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0}
    sorted_v = sorted(clean)
    n = len(sorted_v)

    def _p(percent: float) -> float:
        # Implementacao nearest-rank (simples, sem interpolacao)
        idx = int(percent * (n - 1) + 0.5)
        return float(sorted_v[min(idx, n - 1)])

    return {
        "p50": _p(0.50),
        "p95": _p(0.95),
        "p99": _p(0.99),
        "min": float(sorted_v[0]),
        "max": float(sorted_v[-1]),
    }


def _write_report_json(target: Path, rows: list[dict], data: dict) -> None:
    import json as _json
    target.parent.mkdir(parents=True, exist_ok=True)
    # Calcula estatisticas de tempo (apenas PDFs exportados)
    elapsed_times = [
        float(r.get("elapsed_seconds", 0)) for r in rows
        if r["status"] == "exported" and r.get("elapsed_seconds", 0) > 0
    ]
    total = len(rows)
    exported_count = sum(1 for r in rows if r["status"] == "exported")
    payload = {
        "start_url": data.get("start_url", ""),
        "last_updated": data.get("last_updated", ""),
        "crawl_complete": data.get("crawl_complete", False),
        "summary": {
            "total": total,
            "exported": exported_count,
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "pending": sum(1 for r in rows if r["status"] == "pending"),
            "total_bytes": sum(int(r.get("size_bytes", 0)) for r in rows),
            "conversion_rate": (exported_count / total) if total else 0.0,
            "timing_seconds": _percentiles(elapsed_times),
        },
        "rows": rows,
    }
    with target.open("w", encoding="utf-8") as fp:
        _json.dump(payload, fp, indent=2, ensure_ascii=False)


def _install_signal_handlers() -> None:
    """Instala handlers para SIGTERM que levantam KeyboardInterrupt.

    Sem isto, task managers (systemd, docker stop) enviam SIGTERM e o processo
    morre sem chance de salvar o manifest. Convertendo para KeyboardInterrupt,
    o pipeline normal de cleanup (try/finally) eh acionado.

    Note: signal.signal() so funciona na main thread; em Windows pode nao
    receber SIGTERM mas SIGBREAK eh usado.
    """
    import signal

    def _handler(signum, _frame):
        raise KeyboardInterrupt(f"Sinal {signum} recebido")

    # SIGTERM (Unix) e SIGBREAK (Windows Ctrl+Break) — tenta ambos
    for sig_name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (AttributeError, ValueError, OSError):
                pass
    # SIGINT (Ctrl+C) ja levanta KeyboardInterrupt por default — nao reinstala


if __name__ == "__main__":
    _install_signal_handlers()
    try:
        app()
    except KeyboardInterrupt:
        sys.exit(130)
