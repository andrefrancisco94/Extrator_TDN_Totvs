from __future__ import annotations

from collections import deque
from urllib.parse import urljoin

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .utils import CrawlScope, canonicalize_url, get_console, is_url_in_scope, parse_scope

_TREE_ROOTS = (
    ".plugin_pagetree",
    ".ia-splitter-left",
    "#page-tree",
    "#sidebar",
    "#children-section",
    "#main-content .childpages-macro",
)

_LOAD_SELECTORS = (
    ".plugin_pagetree a[href]",
    ".ia-splitter-left a[href]",
    "#sidebar a[href]",
    "#page-tree a[href]",
    "#children-section a[href]",
    "#main-content",
)


def _selector_timeouts(timeout_ms: int) -> tuple[int, int]:
    """Calcula timeouts dos seletores escalando com o timeout principal."""
    sidebar_load = max(8_000, timeout_ms // 4)
    tree_loading = max(5_000, timeout_ms // 6)
    return sidebar_load, tree_loading


async def _extract_child_links(page: Page, current_url: str) -> list[str]:
    return await page.evaluate(
        """
        ([currentUrl, treeRoots]) => {
          const normalize = (href) => {
            try {
              const u = new URL(href, window.location.origin);
              u.hash = '';
              return u.toString();
            } catch { return null; }
          };

          const altUrl = (() => {
            try {
              const u = new URL(currentUrl);
              if (u.pathname.startsWith('/display/public/')) {
                const a = new URL(u);
                a.pathname = u.pathname.replace('/display/public/', '/display/');
                return a.toString();
              }
              if (u.pathname.startsWith('/display/') && !u.pathname.startsWith('/display/public/')) {
                const parts = u.pathname.split('/').filter(Boolean);
                if (parts.length >= 2 && parts[0] === 'display') {
                  const a = new URL(u);
                  a.pathname = '/display/public/' + parts.slice(1).join('/');
                  return a.toString();
                }
              }
            } catch {}
            return null;
          })();

          const candidates = new Set([normalize(currentUrl)].filter(Boolean));
          if (altUrl) {
            const n = normalize(altUrl);
            if (n) candidates.add(n);
          }

          let li = null;

          const pageId = document.querySelector('meta[name="ajs-page-id"]')?.getAttribute('content');
          if (pageId) {
            for (const sel of treeRoots) {
              const container = document.querySelector(sel);
              if (!container) continue;
              const toggle = container.querySelector(`[data-page-id="${pageId}"]`);
              if (toggle) {
                li = toggle.closest('li');
                break;
              }
            }
          }

          if (!li) {
            for (const sel of treeRoots) {
              const container = document.querySelector(sel);
              if (!container) continue;
              for (const a of container.querySelectorAll('a[href]')) {
                const n = normalize(a.getAttribute('href'));
                if (n && candidates.has(n)) {
                  li = a.closest('li');
                  break;
                }
              }
              if (li) break;
            }
          }

          if (li) {
            const childHrefs = new Set();
            li.querySelectorAll('li a[href], ul a[href]').forEach(a => {
              const href = a.getAttribute('href');
              if (!href) return;
              const n = normalize(href);
              if (n && !candidates.has(n)) childHrefs.add(href);
            });
            if (childHrefs.size > 0) return Array.from(childHrefs);
          }

          const childSection = document.querySelector(
            '#children-section a[href], .childpages-macro a[href], .children-show-hide a[href]'
          );
          if (childSection) {
            const hrefs = new Set();
            document.querySelectorAll(
              '#children-section a[href], .childpages-macro a[href]'
            ).forEach(a => {
              const href = a.getAttribute('href');
              if (href) hrefs.add(href);
            });
            return Array.from(hrefs);
          }

          return [];
        }
        """,
        [current_url, list(_TREE_ROOTS)],
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(PlaywrightError),
    reraise=True,
)
async def _goto_with_retry(page: Page, url: str, timeout_ms: int) -> None:
    await page.goto(url, wait_until="load", timeout=timeout_ms)


async def crawl_confluence_tree(
    start_url: str,
    logger,
    headless: bool = True,
    timeout_ms: int = 45_000,
    max_pages: int | None = None,
) -> list[str]:
    """BFS pela árvore de páginas do Confluence."""
    start_canonical = canonicalize_url(start_url)
    scope: CrawlScope = parse_scope(start_canonical)
    sidebar_timeout, tree_timeout = _selector_timeouts(timeout_ms)

    logger.info("Iniciando crawl em %s", start_canonical)
    logger.info(
        "Escopo: dominio=%s | path_prefix=%s | space=%s",
        scope.domain,
        scope.path_prefix,
        scope.space_key,
    )

    ordered_urls: list[str] = []
    seen: set[str] = set()
    queued: set[str] = {start_canonical}
    queue: deque[str] = deque([start_canonical])
    console = get_console()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=None),
            TextColumn("{task.completed} mapeada(s) | fila: {task.fields[queue]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
        )

        with progress:
            task = progress.add_task("Mapeando árvore...", total=None, queue=0)

            while queue:
                current = queue.popleft()
                if current in seen:
                    continue

                seen.add(current)
                ordered_urls.append(current)
                short = current if len(current) <= 60 else current[:57] + "..."
                progress.update(
                    task,
                    advance=1,
                    queue=len(queue),
                    description=f"Mapeando: {short}",
                )

                logger.info(
                    "Mapeando (%s/%s, fila=%s): %s",
                    len(ordered_urls),
                    max_pages or "∞",
                    len(queue),
                    current,
                )

                if max_pages and len(ordered_urls) >= max_pages:
                    logger.warning("Limite max_pages=%s atingido.", max_pages)
                    break

                try:
                    await _goto_with_retry(page, current, timeout_ms)

                    load_sel = ",".join(_LOAD_SELECTORS)
                    try:
                        await page.wait_for_selector(load_sel, timeout=sidebar_timeout)
                    except PlaywrightError:
                        pass

                    try:
                        await page.wait_for_selector(
                            ".plugin_pagetree_loading",
                            state="detached",
                            timeout=tree_timeout,
                        )
                    except PlaywrightError:
                        pass
                    await page.wait_for_timeout(800)

                    raw_hrefs = await _extract_child_links(page, current)

                    for href in raw_hrefs:
                        if not href or href == "#" or href.lower().startswith("javascript:"):
                            continue
                        absolute = canonicalize_url(urljoin(current, href))
                        if absolute in seen or absolute in queued:
                            continue
                        if not is_url_in_scope(absolute, scope):
                            continue
                        queued.add(absolute)
                        queue.append(absolute)

                    progress.update(task, queue=len(queue))

                except Exception as exc:
                    logger.error("Falha em %s: %s", current, exc)

        await context.close()
        await browser.close()

    logger.info("Crawl finalizado: %s página(s).", len(ordered_urls))
    return ordered_urls
