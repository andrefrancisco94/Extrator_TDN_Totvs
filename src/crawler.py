from __future__ import annotations

import sys
from collections import deque
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from .utils import CrawlScope, canonicalize_url, is_url_in_scope, parse_scope


def _status(msg: str) -> None:
    """Sobrescreve a linha de status no terminal."""
    sys.stdout.write(f"\r\033[2K  {msg}")
    sys.stdout.flush()


# Containers da sidebar/árvore de navegação (em ordem de prioridade)
_TREE_ROOTS = (
    ".plugin_pagetree",
    ".ia-splitter-left",
    "#page-tree",
    "#sidebar",
    "#children-section",
    "#main-content .childpages-macro",
)

# Seletores para aguardar carregamento
_LOAD_SELECTORS = (
    ".plugin_pagetree a[href]",
    ".ia-splitter-left a[href]",
    "#sidebar a[href]",
    "#page-tree a[href]",
    "#children-section a[href]",
    "#main-content",
)


async def _extract_child_links(page, current_url: str) -> list[str]:
    """Extrai links filhos da página atual na sidebar.

    Localiza o <li> da página atual na árvore e retorna os hrefs contidos
    dentro dele (filhos/descendentes diretos), sem vazar para outras seções.
    Fallback para #children-section quando a página não é encontrada na árvore.
    """
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

          // Gera variante de URL para lidar com /display/public/X vs /display/X
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

          // Estratégia 1: meta ajs-page-id → data-page-id no toggle da árvore
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

          // Estratégia 2: busca por URL nos anchors da sidebar
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
            // Retorna todos os hrefs dentro do <li> da página atual,
            // excluindo a própria página (candidatos).
            const childHrefs = new Set();
            li.querySelectorAll('li a[href], ul a[href]').forEach(a => {
              const href = a.getAttribute('href');
              if (!href) return;
              const n = normalize(href);
              if (n && !candidates.has(n)) childHrefs.add(href);
            });
            if (childHrefs.size > 0) return Array.from(childHrefs);
          }

          // Estratégia 3: #children-section ou .childpages-macro
          // (macros do Confluence que listam filhos explicitamente)
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


async def crawl_confluence_tree(
    start_url: str,
    logger,
    headless: bool = True,
    timeout_ms: int = 45_000,
    max_pages: int | None = None,
) -> list[str]:
    """BFS pela árvore de páginas do Confluence.

    Visita cada página e extrai apenas os links filhos da página atual
    na sidebar (não todos os links visíveis). Usa wait_until='load' para
    evitar travamento por requests de analytics/tracking contínuos.
    """
    start_canonical = canonicalize_url(start_url)
    scope: CrawlScope = parse_scope(start_canonical)

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

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        while queue:
            current = queue.popleft()
            if current in seen:
                continue

            seen.add(current)
            ordered_urls.append(current)

            if len(ordered_urls) > 1:
                sys.stdout.write("\n")
                sys.stdout.flush()

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
                _status("Carregando página...")
                # "load" = aguarda evento load (recursos iniciais carregados).
                # "networkidle" trava em sites com analytics/tracking contínuos.
                await page.goto(current, wait_until="load", timeout=timeout_ms)

                _status("Aguardando sidebar...")
                load_sel = ",".join(_LOAD_SELECTORS)
                try:
                    await page.wait_for_selector(load_sel, timeout=8_000)
                except Exception:
                    pass

                # Aguarda o AJAX de carregamento dos filhos da árvore terminar.
                # O plugin_pagetree do Confluence carrega filhos assincronamente
                # e exibe um spinner (.plugin_pagetree_loading) enquanto processa.
                _status("Aguardando AJAX da árvore...")
                try:
                    await page.wait_for_selector(
                        ".plugin_pagetree_loading", state="detached", timeout=5_000
                    )
                except Exception:
                    pass
                # Margem extra para o DOM estabilizar após o AJAX
                await page.wait_for_timeout(800)

                _status("Extraindo links filhos...")
                raw_hrefs = await _extract_child_links(page, current)

                new_links = 0
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
                    new_links += 1

                _status(
                    f"Concluído — {new_links} novo(s) | "
                    f"fila: {len(queue)} | visitado: {len(seen)}"
                )

            except Exception as exc:
                sys.stdout.write("\n")
                sys.stdout.flush()
                logger.error("Falha em %s: %s", current, exc)

        sys.stdout.write("\n")
        sys.stdout.flush()
        await context.close()
        await browser.close()

    logger.info("Crawl finalizado: %s página(s).", len(ordered_urls))
    return ordered_urls
