"""Testes de ponta a ponta contra um Confluence/TDN falso local.

Nenhum request sai da maquina: o servidor (tests/fake_tdn.py) roda em loopback
e simula a arvore de paginas, a REST API e o rate limit do Cloudflare
(HTTP 429 + Retry-After + pagina de erro 1015).

Precisam do Chromium do Playwright e levam alguns minutos, entao sao opt-in:

    pytest -m e2e
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.fake_tdn import FakeTDN, LocalProxy, sample_tree

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module", autouse=True)
def _chromium_or_skip():
    """Pula o modulo inteiro se o Chromium do Playwright nao estiver instalado."""
    async def _launch() -> None:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
            await browser.close()

    try:
        asyncio.run(_launch())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Chromium do Playwright indisponivel: {str(exc).splitlines()[0]}")


@pytest.fixture(autouse=True)
def _restore_console():
    """run_pipeline(quiet=True) silencia o console global; devolve ao normal."""
    from src.utils import get_console

    yield
    get_console().quiet = False


def _server(**kwargs) -> FakeTDN:
    tree_kwargs = {
        key: kwargs.pop(key) for key in ("leaves_per_branch", "branches", "deep")
        if key in kwargs
    }
    tree_kwargs.setdefault("leaves_per_branch", 4)
    tree_kwargs.setdefault("branches", 2)
    tree, root = sample_tree(**tree_kwargs)
    return FakeTDN(tree, root, **kwargs)


def _run(
    srv: FakeTDN, out: Path, *, delay: float = 0.05, workers: int = 1,
    proxies: list[str] | None = None, force_recrawl: bool = False,
    retry_failed_only: bool = False, max_block_wait: float = 600.0,
    start_url: str | None = None,
    quiet: bool = True,
) -> int:
    from src.main import run_pipeline
    from src.utils import RateLimitConfig

    return asyncio.run(run_pipeline(
        start_url=start_url or srv.url_for(srv.root_id),
        output_dir=out,
        consolidated_name="consolidado.pdf",
        headless=True,
        timeout_seconds=60,
        max_pages=None,
        slow_threshold_seconds=60.0,
        auto_confirm=True,
        force_recrawl=force_recrawl,
        force_reexport=False,
        rate_limit=RateLimitConfig(
            base_delay_seconds=delay, backoff_initial_seconds=1.0,
            backoff_max_seconds=30.0, max_block_wait_seconds=max_block_wait,
        ),
        max_workers=workers,
        quiet=quiet,
        retry_failed_only=retry_failed_only,
        proxies=proxies,
    ))


def _manifest(out: Path) -> dict:
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def _expected_urls(srv: FakeTDN) -> list[str]:
    return [srv.url_for(pid) for pid in srv.bfs_order()]


def _non_leaf_count(srv: FakeTDN) -> int:
    return sum(1 for _title, children, _style in srv.tree.values() if children)


# =============================================================================
# Mapeamento
# =============================================================================

def test_crawl_navega_uma_vez_e_lista_filhos_pela_rest_api(tmp_path):
    """Uma navegacao (pagina inicial) + um request de API por no COM filhos.

    Antes: uma navegacao completa + um request de API por pagina, so para
    descobrir o id. Folhas agora nao custam request nenhum.
    """
    srv = _server()
    try:
        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        total = len(srv.tree)

        assert manifest["crawl_complete"] is True
        assert manifest["mapped_urls"] == _expected_urls(srv)
        assert len(manifest["exported"]) == total
        assert manifest["failures"] == {}
        assert (tmp_path / "consolidado.pdf").exists()

        # 1 navegacao do crawl + 1 por pagina exportada
        assert sum(srv.page_hits.values()) == total + 1
        assert sum(srv.api_hits.values()) == _non_leaf_count(srv)
        # Telemetria/polling bloqueados: nao chegam ao servidor
        assert srv.hits["junk"] == 0
        assert not srv.bans
    finally:
        srv.stop()


def test_dom_fallback_quando_a_rest_api_e_negada(tmp_path):
    """REST API 404: o mapa sai pelo DOM da arvore lateral e a API e desligada."""
    srv = _server(leaves_per_branch=3, branches=1, deep=False, api_enabled=False)
    try:
        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert manifest["crawl_complete"] is True
        assert manifest["mapped_urls"] == _expected_urls(srv)
        assert len(manifest["exported"]) == len(srv.tree)
        # Desiste da API apos poucas negativas em vez de tentar em toda pagina.
        assert sum(srv.api_hits.values()) <= 4
    finally:
        srv.stop()


def test_update_remapeia_sem_rebaixar_paginas_existentes(tmp_path):
    """--update: novo mapeamento custa poucos requests e nao refaz PDFs."""
    srv = _server()
    try:
        assert _run(srv, tmp_path) == 0
        pages_before = sum(srv.page_hits.values())
        api_before = sum(srv.api_hits.values())

        assert _run(srv, tmp_path, force_recrawl=True) == 0
        manifest = _manifest(tmp_path)
        assert manifest["crawl_complete"] is True
        assert manifest["mapped_urls"] == _expected_urls(srv)
        # So a navegacao inicial do re-mapeamento; nenhuma pagina re-exportada.
        assert sum(srv.page_hits.values()) == pages_before + 1
        assert sum(srv.api_hits.values()) == api_before * 2
    finally:
        srv.stop()


# =============================================================================
# Rate limit
# =============================================================================

def test_rate_limit_nao_perde_pagina_nem_cutuca_o_ban(tmp_path):
    """Com o servidor bloqueando o IP, todas as paginas saem e, depois de ver o
    429, o extrator fica quieto ate o ban acabar (espera o Retry-After)."""
    srv = _server(limit=10, window=3.0, ban_seconds=2.0)
    try:
        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert srv.bans, "o cenario deveria provocar pelo menos um bloqueio"
        assert not srv.late_pokes, f"requests feitos durante o ban: {dict(srv.late_pokes)}"
        assert manifest["crawl_complete"] is True
        assert len(manifest["exported"]) == len(srv.tree)
        assert manifest["failures"] == {}
    finally:
        srv.stop()


def test_imagens_respondidas_com_429_fazem_a_pagina_ser_refeita(tmp_path):
    """HTML 200 + imagens 429: o PDF nao pode ser gravado sem as imagens."""
    tree, root = sample_tree(leaves_per_branch=3, branches=1, deep=False)
    victim = [pid for pid, (_t, children, _s) in tree.items() if not children][0]
    srv = FakeTDN(tree, root, image_429_once_ids={victim})
    try:
        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert srv.image_429_served[victim] >= 1
        assert srv.page_hits[victim] == 2, "a pagina deveria ter sido carregada de novo"
        assert srv.url_for(victim) in manifest["exported"]
        assert manifest["failures"] == {}
    finally:
        srv.stop()


def test_erro_5xx_transitorio_e_repetido_no_mesmo_run(tmp_path):
    tree, root = sample_tree(leaves_per_branch=3, branches=1, deep=False)
    victim = [pid for pid, (_t, children, _s) in tree.items() if not children][-1]
    srv = FakeTDN(tree, root, fail_once_ids={victim})
    try:
        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert srv.page_hits[victim] == 2
        assert srv.url_for(victim) in manifest["exported"]
        assert manifest["failures"] == {}
    finally:
        srv.stop()


def test_bloqueio_acima_do_limite_de_espera_para_e_depois_retoma(tmp_path):
    """Ban maior que --max-block-wait: para com codigo 3 e o proximo run conclui."""
    srv = _server(leaves_per_branch=3, branches=1, deep=False,
                  limit=3, window=60.0, ban_seconds=30.0)
    try:
        assert _run(srv, tmp_path, max_block_wait=2.0) == 3
        assert not srv.late_pokes

        # O ban "expira": o run seguinte completa o trabalho.
        srv.limit = 10**6
        with srv.lock:
            srv._banned_until.clear()
        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert manifest["crawl_complete"] is True
        assert len(manifest["exported"]) == len(srv.tree)
    finally:
        srv.stop()


# =============================================================================
# Falhas permanentes, exit code e retomada
# =============================================================================

def test_pagina_que_nao_exporta_resulta_em_exit_code_3_e_retry_so_do_que_falta(tmp_path):
    tree, root = sample_tree(leaves_per_branch=3, branches=1, deep=False)
    victim = [pid for pid, (_t, children, _s) in tree.items() if not children][0]
    srv = FakeTDN(tree, root, login_ids={victim})
    try:
        # Documentacao incompleta NAO pode sair como sucesso.
        assert _run(srv, tmp_path) == 3
        manifest = _manifest(tmp_path)
        assert srv.url_for(victim) in manifest["failures"]
        assert len(manifest["exported"]) == len(tree) - 1
        hits_before = dict(srv.page_hits)

        # Pagina liberada: reprocessar so o que falta nao toca nas outras.
        srv.login_ids.clear()
        assert _run(srv, tmp_path, retry_failed_only=True) == 0
        manifest = _manifest(tmp_path)
        assert manifest["failures"] == {}
        assert len(manifest["exported"]) == len(tree)
        for pid, count in hits_before.items():
            if pid != victim:
                assert srv.page_hits[pid] == count, f"pagina {pid} foi baixada de novo"
    finally:
        srv.stop()


def test_url_inicial_de_outro_job_nao_descarta_o_manifest(tmp_path):
    srv = _server(leaves_per_branch=3, branches=1, deep=False)
    try:
        assert _run(srv, tmp_path) == 0
        before = (tmp_path / "manifest.json").read_text(encoding="utf-8")
        other = srv.url_for(srv.bfs_order()[1])

        assert _run(srv, tmp_path, start_url=other) == 2
        assert (tmp_path / "manifest.json").read_text(encoding="utf-8") == before
    finally:
        srv.stop()


def test_url_inicial_equivalente_reusa_o_job(tmp_path):
    """/display/X/Titulo e /display/public/X/Titulo sao a mesma pagina."""
    srv = _server(leaves_per_branch=3, branches=1, deep=False)
    try:
        assert _run(srv, tmp_path) == 0
        pages_before = sum(srv.page_hits.values())
        assert _run(srv, tmp_path, start_url=srv.url_for(srv.root_id, public=False)) == 0
        # Crawl completo reaproveitado e todos os PDFs existentes: zero navegacoes.
        assert sum(srv.page_hits.values()) == pages_before
    finally:
        srv.stop()


# =============================================================================
# Ordem e proxies
# =============================================================================

def test_consolidado_segue_a_ordem_do_mapa_mesmo_com_workers(tmp_path):
    from pypdf import PdfReader

    srv = _server()
    try:
        assert _run(srv, tmp_path, workers=3) == 0
        reader = PdfReader(str(tmp_path / "consolidado.pdf"))
        titles = [item.title for item in reader.outline if hasattr(item, "title")]
        expected = [f"{srv.tree[pid][0]} - Fake TDN" for pid in srv.bfs_order()]
        assert titles == expected
    finally:
        srv.stop()


def test_rodizio_de_proxy_quando_o_ip_e_bloqueado(tmp_path):
    """Com dois proxies, o bloqueio de um IP faz o trabalho seguir pelo outro."""
    srv = _server(limit=10, window=3.0, ban_seconds=20.0)
    proxies = [LocalProxy("127.0.0.2"), LocalProxy("127.0.0.3")]
    try:
        assert _run(srv, tmp_path, proxies=[p.url for p in proxies]) == 0
        manifest = _manifest(tmp_path)
        assert len(manifest["exported"]) == len(srv.tree)
        assert manifest["failures"] == {}
        assert srv.bans, "o cenario deveria provocar pelo menos um bloqueio"
        used = {ip for ip, count in srv.hits_by_ip.items() if count}
        assert {"127.0.0.2", "127.0.0.3"} <= used, f"so usou {used}"
        assert "127.0.0.1" not in used, "request saiu sem passar pelo proxy"
        assert not srv.late_pokes
    finally:
        for proxy in proxies:
            proxy.stop()
        srv.stop()


def test_rodizio_de_proxy_com_workers_paralelos(tmp_path):
    """Regressao: a troca de proxy relanca o browser enquanto outros workers
    estao no meio de uma pagina. Antes isso derrubava o run inteiro com
    "context has been closed"; agora os workers pegam uma page nova e seguem."""
    from pypdf import PdfReader

    srv = _server(limit=10, window=3.0, ban_seconds=20.0)
    proxies = [LocalProxy("127.0.0.2"), LocalProxy("127.0.0.3")]
    try:
        assert _run(srv, tmp_path, workers=3, proxies=[p.url for p in proxies]) == 0
        manifest = _manifest(tmp_path)
        assert len(manifest["exported"]) == len(srv.tree)
        assert manifest["failures"] == {}
        assert srv.bans, "o cenario deveria provocar pelo menos um bloqueio"
        used = {ip for ip, count in srv.hits_by_ip.items() if count}
        assert {"127.0.0.2", "127.0.0.3"} <= used, f"so usou {used}"
        reader = PdfReader(str(tmp_path / "consolidado.pdf"))
        titles = [item.title for item in reader.outline if hasattr(item, "title")]
        assert titles == [f"{srv.tree[pid][0]} - Fake TDN" for pid in srv.bfs_order()]
    finally:
        for proxy in proxies:
            proxy.stop()
        srv.stop()


def test_proxies_trabalham_simultaneamente_e_troca_nao_fecha_outras_paginas(tmp_path, monkeypatch):
    from src import pdf_exporter as exporter

    srv = _server(leaves_per_branch=5, branches=1, deep=False)
    proxies = [LocalProxy(f"127.0.0.{i}") for i in range(2, 6)]
    original = exporter._export_one
    active = set()
    simultaneous = set()
    contexts = {}

    async def observe(page, session, *args, **kwargs):
        active.add(session.proxy)
        contexts[session.proxy] = session.context
        simultaneous.update(active if len(active) > 1 else ())
        try:
            await asyncio.sleep(0.1)
            return await original(page, session, *args, **kwargs)
        finally:
            active.discard(session.proxy)

    monkeypatch.setattr(exporter, "_export_one", observe)
    try:
        assert _run(srv, tmp_path, workers=2, proxies=[p.url for p in proxies], quiet=False) == 0
        assert len(simultaneous) >= 2
        assert len({id(context) for context in contexts.values()}) >= 2
        assert len(_manifest(tmp_path)["exported"]) == len(srv.tree)
        log = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert "context or browser has been closed" not in log
    finally:
        for proxy in proxies:
            proxy.stop()
        srv.stop()


def test_pagina_com_ip_bloqueado_muda_de_worker_sem_aguardar_uma_hora(tmp_path, monkeypatch):
    import time
    from src import pdf_exporter as exporter

    srv = _server(leaves_per_branch=3, branches=1, deep=False)
    proxies = [LocalProxy("127.0.0.2"), LocalProxy("127.0.0.3")]
    original = exporter._export_one
    injected = []

    async def block_once(page, session, *args, **kwargs):
        if session.proxy == proxies[0].url and not injected:
            injected.append(True)
            return page, exporter.ExportOutcome(exporter._BLOCKED, "ban de teste", retry_after=3600)
        return await original(page, session, *args, **kwargs)

    monkeypatch.setattr(exporter, "_export_one", block_once)
    try:
        started = time.monotonic()
        assert _run(srv, tmp_path, workers=2, proxies=[p.url for p in proxies], quiet=False) == 0
        assert time.monotonic() - started < 30
        assert injected
        assert len(_manifest(tmp_path)["exported"]) == len(srv.tree)
        assert _manifest(tmp_path)["failures"] == {}
        assert "devolvida a fila" in (tmp_path / "run.log").read_text(encoding="utf-8")
    finally:
        for proxy in proxies:
            proxy.stop()
        srv.stop()


def test_preparacao_espera_imagem_lazy_mas_nao_script_secundario(tmp_path):
    import logging
    import time
    from playwright.async_api import async_playwright
    from src.pdf_exporter import _prepare_page_for_pdf
    from tests.fake_tdn import PNG

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, channel="chromium")
            try:
                page = await browser.new_page()
                image_started = asyncio.Event()
                script_release = asyncio.Event()

                async def route(request):
                    if request.request.url.endswith("image.png"):
                        image_started.set()
                        await asyncio.sleep(0.2)
                        await request.fulfill(body=PNG, content_type="image/png")
                    elif request.request.url.endswith("slow.js"):
                        await script_release.wait()
                        await request.fulfill(body="window.ready = true", content_type="application/javascript")
                    else:
                        await request.fulfill(body='''<html><body><div id="main-content">
                        <h1>Conteudo</h1><img loading="lazy" src="/image.png" style="margin-top:4000px">
                        </div><script async src="/slow.js"></script></body></html>''', content_type="text/html")

                await page.route("http://assets.test/**", route)
                await page.goto("http://assets.test/", wait_until="domcontentloaded")
                started = time.monotonic()
                await asyncio.wait_for(_prepare_page_for_pdf(
                    page, started, 30_000, logging.getLogger("assets"), page.url,
                ), timeout=5)
                assert image_started.is_set()
                assert await page.evaluate("document.images[0].complete && document.images[0].naturalWidth > 0")
                assert await page.evaluate("document.readyState") != "complete"
                script_release.set()
                await page.wait_for_load_state("load")
            finally:
                await browser.close()

    asyncio.run(run())


def test_ctrl_c_no_meio_do_mapeamento_retoma_do_ponto_exato(tmp_path):
    """Run interrompido por excecao durante o crawl grava a fila; o seguinte
    conclui sem perder nenhuma pagina."""
    from src import crawler

    srv = _server()
    original = crawler._api_list_children
    calls = {"n": 0}

    async def interrupting(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("simulado")
        return await original(*args, **kwargs)

    crawler._api_list_children = interrupting
    try:
        with pytest.raises(KeyboardInterrupt):
            _run(srv, tmp_path)
    finally:
        crawler._api_list_children = original
    try:
        partial = _manifest(tmp_path)
        assert partial["crawl_complete"] is False
        assert partial["crawl_queue"], "a fila deveria ter sido gravada"

        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert manifest["crawl_complete"] is True
        assert sorted(manifest["mapped_urls"]) == sorted(_expected_urls(srv))
        assert len(manifest["exported"]) == len(srv.tree)
    finally:
        srv.stop()


# =============================================================================
# Markdown e teste de proxies
# =============================================================================

def _run_markdown(srv: FakeTDN, out: Path) -> int:
    from src.main import markdown_pipeline
    from src.utils import RateLimitConfig

    return asyncio.run(markdown_pipeline(
        start_url=srv.url_for(srv.root_id),
        output_dir=out,
        consolidated_name="consolidado.md",
        headless=True,
        timeout_seconds=60,
        max_pages=None,
        slow_threshold_seconds=60.0,
        auto_confirm=True,
        force_recrawl=False,
        force_reexport=False,
        rate_limit=RateLimitConfig(
            base_delay_seconds=0.05, backoff_initial_seconds=1.0, backoff_max_seconds=30.0,
        ),
        quiet=True,
    ))


def test_markdown_exporta_todas_as_paginas_e_refaz_a_bloqueada(tmp_path):
    """Pipeline Markdown ponta a ponta, com uma pagina cujas imagens levam 429."""
    tree, root = sample_tree(leaves_per_branch=3, branches=1, deep=False)
    victim = [pid for pid, (_t, children, _s) in tree.items() if not children][0]
    srv = FakeTDN(tree, root, image_429_once_ids={victim})
    try:
        assert _run_markdown(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert len(manifest["exported_md"]) == len(tree)
        assert srv.page_hits[victim] == 2, "a pagina bloqueada deveria ter sido refeita"
        pages = sorted((tmp_path / "markdown" / "pages").glob("*.md"))
        assert len(pages) == len(tree)
        consolidated = (tmp_path / "markdown" / "consolidado.md").read_text(encoding="utf-8")
        for title, _children, _style in tree.values():
            assert title in consolidated
        # Imagens baixadas e apontando para o arquivo local
        assert list((tmp_path / "markdown" / "attachments").rglob("*.png"))
        assert "../attachments/" in pages[-1].read_text(encoding="utf-8")
    finally:
        srv.stop()


def test_proxy_test_reporta_saida_boa_e_proxy_morto():
    """`proxy test`: 1 request por saida; proxy fora do ar aparece como falha."""
    from src.main import _probe_exits

    srv = _server(leaves_per_branch=1, branches=1, deep=False)
    good = LocalProxy("127.0.0.2")
    dead = LocalProxy("127.0.0.3", fail=True)
    try:
        url = srv.url_for(srv.root_id)
        results = asyncio.run(_probe_exits([good.url, None, dead.url], url, 15_000))
        assert [item["ok"] for item in results] == [True, True, False]
        assert results[0]["status"] == 200
        assert results[2]["detail"] or results[2]["status"] != 200
        assert srv.hits_by_ip["127.0.0.2"] >= 1 and srv.hits_by_ip["127.0.0.1"] >= 1
        assert srv.hits_by_ip["127.0.0.3"] == 0
    finally:
        good.stop()
        dead.stop()
        srv.stop()


# =============================================================================
# Achados da revisao: regenerate, mapas antigos, max-pages, pagina inicial 404
# =============================================================================

def _consolidated_titles(out: Path) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(str(out / "consolidado.pdf"))
    return [item.title for item in reader.outline if hasattr(item, "title")]


def test_regenerate_com_pagina_que_falha_sai_com_3_e_mantem_o_consolidado_inteiro(tmp_path):
    """--regenerate em que uma pagina cai numa tela de login: antes saia com
    codigo 0 e o consolidado era regravado SEM a pagina. Agora o PDF antigo
    dela continua no consolidado e o run avisa que ela nao foi atualizada."""
    from src.main import run_pipeline
    from src.utils import RateLimitConfig

    tree, root = sample_tree(leaves_per_branch=3, branches=1, deep=False)
    victim = [pid for pid, (_t, children, _s) in tree.items() if not children][0]
    srv = FakeTDN(tree, root)
    try:
        assert _run(srv, tmp_path) == 0
        assert len(_consolidated_titles(tmp_path)) == len(tree)

        srv.login_ids.add(victim)
        code = asyncio.run(run_pipeline(
            start_url=srv.url_for(root), output_dir=tmp_path, consolidated_name="consolidado.pdf",
            headless=True, timeout_seconds=60, max_pages=None, slow_threshold_seconds=60.0,
            auto_confirm=True, force_recrawl=True, force_reexport=True,
            rate_limit=RateLimitConfig(base_delay_seconds=0.05, backoff_initial_seconds=1.0),
            max_workers=1, quiet=True,
        ))
        assert code == 3
        assert len(_consolidated_titles(tmp_path)) == len(tree)
    finally:
        srv.stop()


def test_mapa_antigo_com_desistencias_e_remapeado_no_resume(tmp_path):
    """Job de versao antiga: crawl_complete=true mas com paginas cujos filhos
    nunca foram listados (subarvore faltando). Um resume simples re-mapeia,
    acha as paginas que faltavam e limpa o registro."""
    srv = _server()
    try:
        assert _run(srv, tmp_path) == 0
        manifest_path = tmp_path / "manifest.json"
        data = _manifest(tmp_path)
        full = list(data["mapped_urls"])
        # Simula o estado deixado pela versao antiga: mapa "completo" truncado.
        section = srv.url_for(srv.tree[srv.root_id][1][0])
        lost = [srv.url_for(c) for c in srv.tree[srv.tree[srv.root_id][1][0]][1]]
        data["mapped_urls"] = [u for u in full if u not in lost]
        for url in lost:
            data["exported"].pop(url, None)
        data["failures"] = {section: {
            "error": "crawl timeout apos 2 tentativas intra-run", "attempts": 1, "last_attempt": "",
        }}
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        assert _run(srv, tmp_path) == 0
        healed = _manifest(tmp_path)
        assert healed["crawl_complete"] is True
        assert sorted(healed["mapped_urls"]) == sorted(full)
        assert healed["failures"] == {}
        assert len(healed["exported"]) == len(srv.tree)
    finally:
        srv.stop()


def test_max_pages_e_depois_sem_limite_mapeia_a_arvore_inteira(tmp_path):
    """A pagina que bate no limite nao pode ficar marcada como resolvida:
    o run seguinte, sem limite, tem de listar os filhos dela."""
    from src.main import run_pipeline
    from src.utils import RateLimitConfig

    srv = _server()
    try:
        limited = asyncio.run(run_pipeline(
            start_url=srv.url_for(srv.root_id), output_dir=tmp_path,
            consolidated_name="consolidado.pdf", headless=True, timeout_seconds=60,
            max_pages=3, slow_threshold_seconds=60.0, auto_confirm=True,
            force_recrawl=False, force_reexport=False,
            rate_limit=RateLimitConfig(base_delay_seconds=0.05, backoff_initial_seconds=1.0),
            max_workers=1, quiet=True,
        ))
        assert limited == 0
        assert len(_manifest(tmp_path)["mapped_urls"]) == 3

        assert _run(srv, tmp_path) == 0
        manifest = _manifest(tmp_path)
        assert manifest["crawl_complete"] is True
        assert sorted(manifest["mapped_urls"]) == sorted(_expected_urls(srv))
        assert len(manifest["exported"]) == len(srv.tree)
    finally:
        srv.stop()


def test_update_com_pagina_inicial_fora_do_ar_mantem_o_mapa(tmp_path):
    """Pagina inicial respondendo 404 durante um --update: o mapa completo e o
    consolidado anteriores nao podem ser trocados por um mapa de uma pagina."""
    srv = _server(leaves_per_branch=3, branches=1, deep=False)
    try:
        assert _run(srv, tmp_path) == 0
        before = _manifest(tmp_path)["mapped_urls"]
        root_title = srv.tree[srv.root_id][0]
        del srv.by_title[root_title]  # a pagina inicial passa a responder 404

        assert _run(srv, tmp_path, force_recrawl=True) == 3
        after = _manifest(tmp_path)
        assert after["crawl_complete"] is True
        assert after["mapped_urls"] == before
        assert len(_consolidated_titles(tmp_path)) == len(srv.tree)
    finally:
        srv.stop()


# =============================================================================
# Lista grande de proxies publicos: mortos, um que emperra e um bom
# =============================================================================

class _StallingProxy(LocalProxy):
    """Proxy publico tipico: responde ao teste de conectividade e a pagina
    inicial, mas emperra nas demais paginas (segura a conexao sem responder)."""

    def __init__(self, source_ip: str, keep_url: str, stall: float = 15.0):
        self.keep_url = keep_url
        self.stall = stall
        self.stalled = 0
        super().__init__(source_ip)

    def _serve(self, conn):
        import socket
        import time

        try:
            first_line = conn.recv(4096, socket.MSG_PEEK).split(b"\r\n", 1)[0].decode("latin-1")
        except OSError:
            first_line = ""
        target = first_line.split(" ")[1] if first_line.count(" ") >= 2 else ""
        is_page = "/display/" in target or "/pages/" in target
        if is_page and target != self.keep_url:
            self.stalled += 1
            time.sleep(self.stall)
            conn.close()
            return
        super()._serve(conn)


class _SlowFirstProxy(LocalProxy):
    """Proxy bom, mas mais lento no primeiro contato: fica atras na triagem."""

    def __init__(self, source_ip: str):
        self.first = True
        super().__init__(source_ip)

    def _serve(self, conn):
        import time

        if self.first:
            self.first = False
            time.sleep(0.6)
        super()._serve(conn)


def _closed_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_lista_grande_com_proxies_mortos_e_um_que_emperra(tmp_path):
    """Cenario do run real com 2000 proxies publicos: a triagem tira os mortos;
    o proxy que passa no teste mas emperra nas paginas sai do rodizio depois de
    3 timeouts, SEM virar "bloqueio do servidor" (nada de cooldown nem de
    intervalo dobrado), e as paginas saem pelo proxy bom."""
    from src.main import run_pipeline
    from src.utils import RateLimitConfig

    srv = _server()
    root_url = srv.url_for(srv.root_id)
    stalling = _StallingProxy("127.0.0.2", keep_url=root_url)
    good = _SlowFirstProxy("127.0.0.3")
    dead = [f"http://127.0.0.1:{_closed_port()}" for _ in range(4)]
    try:
        code = asyncio.run(run_pipeline(
            start_url=root_url, output_dir=tmp_path, consolidated_name="consolidado.pdf",
            headless=True, timeout_seconds=10, max_pages=None, slow_threshold_seconds=60.0,
            auto_confirm=True, force_recrawl=False, force_reexport=False,
            rate_limit=RateLimitConfig(base_delay_seconds=0.05, backoff_initial_seconds=1.0),
            max_workers=3, quiet=True, proxies=[*dead, stalling.url, good.url],
        ))
        log = (tmp_path / "run.log").read_text(encoding="utf-8")
        assert code == 0, log[-3000:]
        manifest = _manifest(tmp_path)
        assert len(manifest["exported"]) == len(srv.tree)
        assert manifest["failures"] == {}
        # Os 4 mortos ficaram na triagem: o browser nunca tentou sair por eles.
        assert "ERR_PROXY_CONNECTION_FAILED" not in log
        assert stalling.stalled >= 3, "o cenario deveria fazer o proxy emperrar"
        assert "fora do rodizio apos falhas seguidas" in log or "falhou seguidamente" in log
        assert "Bloqueio detectado" not in log
        assert srv.hits_by_ip["127.0.0.3"] > 0
        assert srv.hits_by_ip["127.0.0.1"] == 0, "request saiu sem passar por proxy"
    finally:
        stalling.stop()
        good.stop()
        srv.stop()
