from __future__ import annotations

import sys
from pathlib import Path

from playwright.async_api import async_playwright

from .utils import build_pdf_file_name, ensure_output_dirs


def _status(msg: str) -> None:
    sys.stdout.write(f"\r\033[2K  {msg}")
    sys.stdout.flush()


_CONTENT_SELECTORS = (
    "#main-content",
    "#content",
    ".wiki-content",
    "article",
    "main",
)


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

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        for index, url in enumerate(urls, start=1):
            try:
                short_url = url if len(url) <= 80 else url[:77] + "..."
                _status(f"[{index}/{total}] Carregando: {short_url}")
                response = await page.goto(url, wait_until="load", timeout=timeout_ms)
                if response and response.status >= 400:
                    raise RuntimeError(f"HTTP {response.status}")

                # Aguarda o conteúdo principal aparecer
                content_sel = ",".join(_CONTENT_SELECTORS)
                try:
                    await page.wait_for_selector(content_sel, timeout=5_000)
                except Exception:
                    pass

                _status(f"[{index}/{total}] Gerando PDF...")
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

                sys.stdout.write("\n")
                sys.stdout.flush()
                logger.info("PDF gerado (%s/%s): %s", index, total, file_name)

            except Exception as exc:
                sys.stdout.write("\n")
                sys.stdout.flush()
                logger.error("Erro ao gerar PDF de %s: %s", url, exc)
                failures.append((url, str(exc)))

        await context.close()
        await browser.close()

    return exported, failures
