from __future__ import annotations

from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .utils import build_pdf_file_name, ensure_output_dirs, get_console

_CONTENT_SELECTORS = (
    "#main-content",
    "#content",
    ".wiki-content",
    "article",
    "main",
)


def _content_timeout(timeout_ms: int) -> int:
    return max(5_000, timeout_ms // 6)


class TransientHTTPError(RuntimeError):
    """HTTP 5xx ou erro transiente que merece retry."""


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((PlaywrightError, TransientHTTPError)),
    reraise=True,
)
async def _goto_with_retry(page: Page, url: str, timeout_ms: int) -> None:
    response = await page.goto(url, wait_until="load", timeout=timeout_ms)
    if response and response.status >= 500:
        raise TransientHTTPError(f"HTTP {response.status}")
    if response and response.status >= 400:
        raise RuntimeError(f"HTTP {response.status}")


async def export_pages_to_pdf(
    urls: list[str],
    output_dir: Path,
    logger,
    headless: bool = True,
    timeout_ms: int = 45_000,
) -> tuple[list[Path], list[tuple[str, str]]]:
    pages_dir, _ = ensure_output_dirs(output_dir)
    exported: list[Path] = []
    failures: list[tuple[str, str]] = []
    used_names: set[str] = set()
    total = len(urls)
    content_timeout = _content_timeout(timeout_ms)
    console = get_console()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold green]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            TextColumn("•"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
        )

        with progress:
            task = progress.add_task("Gerando PDFs", total=total)

            for index, url in enumerate(urls, start=1):
                short_url = url if len(url) <= 60 else url[:57] + "..."
                progress.update(task, description=f"PDF {index}/{total}: {short_url}")

                try:
                    await _goto_with_retry(page, url, timeout_ms)

                    content_sel = ",".join(_CONTENT_SELECTORS)
                    try:
                        await page.wait_for_selector(content_sel, timeout=content_timeout)
                    except PlaywrightError:
                        pass

                    await page.emulate_media(media="print")
                    title = (await page.title()) or f"pagina-{index}"
                    file_name = build_pdf_file_name(index, title, used_names)
                    used_names.add(file_name)

                    pdf_path = pages_dir / file_name
                    await page.pdf(
                        path=str(pdf_path),
                        format="A4",
                        print_background=True,
                        prefer_css_page_size=True,
                        margin={
                            "top": "12mm",
                            "right": "10mm",
                            "bottom": "12mm",
                            "left": "10mm",
                        },
                    )
                    exported.append(pdf_path)
                    logger.info("PDF gerado (%s/%s): %s", index, total, file_name)

                except Exception as exc:
                    logger.error("Erro ao gerar PDF de %s: %s", url, exc)
                    failures.append((url, str(exc)))

                progress.advance(task)

        await context.close()
        await browser.close()

    return exported, failures
