"""Consolidacao de paginas Markdown individuais em um unico documento navegavel."""
from __future__ import annotations

from pathlib import Path

from .utils import atomic_replace_with_retry


def merge_markdown(
    entries: list[tuple[Path, str, str]],
    output_file: Path,
    logger,
    consolidated_title: str = "TDN TOTVS - Consolidado",
) -> None:
    """Concatena paginas Markdown em um unico arquivo com indice clicavel.

    entries: lista de (path, titulo, url_origem) na ordem do crawl (hierarquia).
    Cada pagina recebe uma ancora unica `#page-N`; o indice linka para elas.
    Escreve em arquivo temporario e faz rename atomico no fim.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if not entries:
        raise ValueError("Nenhuma pagina Markdown disponivel para consolidar.")

    skipped: list[tuple[Path, str]] = []
    toc_lines: list[str] = []
    body_parts: list[str] = []

    for i, (path, title, _url) in enumerate(entries, start=1):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Pulando Markdown ilegivel %s: %s", path.name, exc)
            skipped.append((path, str(exc)))
            continue
        anchor = f"page-{i}"
        display_title = title.strip() or path.stem
        toc_lines.append(f"{i}. [{display_title}](#{anchor})")
        body_parts.append(f'<a id="{anchor}"></a>\n\n{text.strip()}\n\n---\n')

    if not body_parts:
        raise ValueError("Nenhuma pagina Markdown valida para consolidar (todas ilegiveis).")

    header = (
        f"# {consolidated_title}\n\n## Indice\n\n" + "\n".join(toc_lines) + "\n\n---\n\n"
    )
    full_text = header + "\n".join(body_parts)

    tmp_file = output_file.with_suffix(output_file.suffix + ".tmp")
    try:
        tmp_file.write_text(full_text, encoding="utf-8")
        atomic_replace_with_retry(str(tmp_file), str(output_file))
    except OSError as exc:
        logger.error("Falha ao gravar Markdown consolidado: %s", exc)
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass
        raise

    if skipped:
        logger.warning("Paginas Markdown ignoradas na consolidacao: %s", len(skipped))
    logger.info(
        "Markdown consolidado gerado: %s (%d entradas, %d ignoradas)",
        output_file, len(entries) - len(skipped), len(skipped),
    )
