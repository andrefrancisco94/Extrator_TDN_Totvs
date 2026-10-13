"""Testes para os fixes da v0.11.0: bloqueio 1015/429, retry, resume e mapeamento.

Cobre a logica pura (sem Playwright real). O fluxo completo contra um servidor
falso esta em test_e2e_fake_tdn.py (`pytest -m e2e`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from src.blocking import (
    ACCESS_DENIED,
    CHALLENGE,
    RATE_LIMIT,
    SERVER_ERROR,
    classify_response,
    error_headline,
    is_proxy_error,
    is_transient_network_error,
    parse_retry_after,
    short_error,
)
from src.browser import PageNetStats, get_page_stats
from src.crawler import (
    ChildRef,
    CrawlState,
    _child_refs_from_results,
    _enqueue_children,
    _finalize_crawl,
    _page_id_from_url,
    _restore_state_from_checkpoint,
)
from src.main import app
from src.utils import (
    MANIFEST_FILENAME,
    BlockedTooLongError,
    Checkpoint,
    RateLimitConfig,
    RateLimiter,
    chrome_user_agent,
    display_url_variant,
    find_jobs,
    is_blocking_error,
    parse_scope,
    start_url_key,
)

runner = CliRunner()

BLOCK_1015 = (
    "<title>Access denied | tdn.totvs.com used Cloudflare to restrict access</title>"
    "Error 1015 Ray ID: abc You are being rate limited Cloudflare"
)


@pytest.fixture
def logger():
    return logging.getLogger("test_v11")


def _valid_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 4000 + b"\n%%EOF")


# =============================================================================
# RateLimiter: Retry-After, ritmo adaptativo, workers
# =============================================================================


def test_troca_de_ip_restaura_ritmo_proprio_e_preserva_respostas_antigas():
    async def run():
        lim = RateLimiter(RateLimitConfig(base_delay_seconds=2))
        started = time.monotonic()
        await lim.report_block(retry_after=600)
        await lim.report_block(retry_after=600)
        assert lim.current_delay == 8
        await lim.switch_exit("ip-a", "ip-b")
        await lim.set_cooldown(0)
        assert lim.current_delay == 2 and lim.consecutive_blocks == 0
        # Um 429 do IP antigo nao altera o ritmo nem bloqueia o novo.
        assert await lim.report_block(retry_after=600, started_at=started) == 0
        assert lim.current_delay == 2 and lim.total_blocks == 2
        await lim.report_block(retry_after=1)
        assert lim.current_delay == 4
        await lim.switch_exit("ip-b", "ip-a")
        assert lim.current_delay == 8 and lim.consecutive_blocks == 2
        await lim.switch_exit("ip-a", "ip-b")
        assert lim.current_delay == 4 and lim.consecutive_blocks == 1

    asyncio.run(run())

def test_limiter_usa_retry_after_do_servidor():
    """O cooldown e o tempo informado pelo servidor (+ folga), nao um chute."""
    cfg = RateLimitConfig(base_delay_seconds=0.01, retry_after_margin_seconds=2.0)
    lim = RateLimiter(cfg)

    async def run():
        first = await lim.report_block(reason="429", retry_after=600)
        second = await lim.report_block(reason="429", retry_after=600)
        return first, second

    first, second = asyncio.run(run())
    assert first == 602.0
    assert second == 602.0  # nao escala exponencialmente quando o servidor informa


def test_limiter_sem_retry_after_mantem_backoff_exponencial():
    cfg = RateLimitConfig(base_delay_seconds=0.01, backoff_initial_seconds=10.0)
    lim = RateLimiter(cfg)

    async def run():
        return [await lim.report_block(reason="x") for _ in range(3)]

    first, second, third = asyncio.run(run())
    assert 10.0 <= first <= 12.5
    assert 20.0 <= second <= 25.0
    assert 40.0 <= third <= 50.0


def test_limiter_desacelera_apos_bloqueio_e_recupera_aos_poucos():
    """Um bloqueio dobra o intervalo; so uma sequencia de sucessos o reduz."""
    cfg = RateLimitConfig(
        base_delay_seconds=2.0, adaptive_recover_after=3, adaptive_recover_factor=0.5,
        adaptive_max_delay_seconds=20.0,
    )
    lim = RateLimiter(cfg)

    async def run():
        assert lim.current_delay == 2.0
        await lim.report_block(reason="x", retry_after=1)
        assert lim.current_delay == 4.0
        await lim.report_block(reason="x", retry_after=1)
        assert lim.current_delay == 8.0
        # Um sucesso isolado nao devolve o ritmo que acabou de ser bloqueado.
        await lim.report_success()
        assert lim.current_delay == 8.0
        await lim.report_success()
        await lim.report_success()
        assert lim.current_delay == 4.0
        for _ in range(3):
            await lim.report_success()
        assert lim.current_delay == 2.0
        for _ in range(6):
            await lim.report_success()
        assert lim.current_delay == 2.0  # nunca abaixo do base

    asyncio.run(run())


def test_limiter_intervalo_adaptativo_respeita_teto():
    cfg = RateLimitConfig(base_delay_seconds=2.0, adaptive_max_delay_seconds=5.0)
    lim = RateLimiter(cfg)

    async def run():
        for _ in range(6):
            await lim.report_block(reason="x", retry_after=1)
        return lim.current_delay

    assert asyncio.run(run()) == 5.0


def test_limiter_workers_nao_multiplicam_o_cooldown():
    """3 workers esperando o MESMO cooldown saem em fila, sem somar o cooldown
    uma vez por worker (antes: 100s / 202s / 304s)."""
    cfg = RateLimitConfig(base_delay_seconds=0.05, jitter_ratio=0.0)
    lim = RateLimiter(cfg)

    async def run():
        await lim.report_block(reason="x", retry_after=0.4)
        await lim.set_cooldown(0.4)
        start = time.monotonic()
        await asyncio.gather(lim.wait(), lim.wait(), lim.wait())
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    assert 0.35 <= elapsed < 1.2, elapsed


def test_limiter_wait_respeita_bloqueio_reportado_durante_o_sono():
    """Quem ja estava dormindo o intervalo base nao pode sair dentro de um ban
    reportado por outro worker nesse meio tempo."""
    cfg = RateLimitConfig(base_delay_seconds=0.2, jitter_ratio=0.0)
    lim = RateLimiter(cfg)

    async def run():
        await lim.wait()  # primeiro request: sem espera
        start = time.monotonic()
        waiter = asyncio.create_task(lim.wait())  # dorme ~0.2s (intervalo base)
        await asyncio.sleep(0.05)
        await lim.report_block(reason="x", retry_after=0.6)
        await lim.set_cooldown(0.6)
        await waiter
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.6


def test_limiter_mesmo_bloqueio_visto_por_outro_worker_nao_escala():
    cfg = RateLimitConfig(base_delay_seconds=0.01, backoff_initial_seconds=10.0)
    lim = RateLimiter(cfg)

    async def run():
        in_flight_since = time.monotonic()
        await asyncio.sleep(0.01)
        first = await lim.report_block(reason="worker 1")
        # worker 2 tinha um request em voo desde ANTES do bloqueio ser registrado
        second = await lim.report_block(reason="worker 2", started_at=in_flight_since)
        return first, second, lim.consecutive_blocks, lim.total_blocks

    first, second, consecutive, total = asyncio.run(run())
    assert consecutive == 1 and total == 1
    assert second <= first


def test_limiter_bloqueio_do_ip_antigo_nao_trava_o_proxy_novo():
    """Worker A foi bloqueado e trocou de proxy (cooldown zerado). O request do
    worker B, que ainda saiu pelo IP antigo, volta com 429 depois disso: nao
    pode colocar o IP novo em cooldown nem desacelerar o ritmo de novo."""
    cfg = RateLimitConfig(base_delay_seconds=1.0, backoff_initial_seconds=30.0)
    lim = RateLimiter(cfg)

    async def run():
        b_started = time.monotonic()
        await asyncio.sleep(0.01)
        await lim.report_block(reason="worker A", retry_after=600)
        await lim.set_cooldown(0.0)  # rodizio: novo IP esta livre
        delay_after_a = lim.current_delay
        stale = await lim.report_block(reason="worker B", retry_after=600, started_at=b_started)
        return stale, lim.cooldown_remaining(), lim.total_blocks, delay_after_a, lim.current_delay

    stale, remaining, total, delay_a, delay_b = asyncio.run(run())
    assert stale == 0.0 and remaining == 0.0
    assert total == 1 and delay_b == delay_a


def test_limiter_bloqueio_acima_do_limite_levanta_erro():
    cfg = RateLimitConfig(base_delay_seconds=0.01, max_block_wait_seconds=60.0)
    lim = RateLimiter(cfg)

    async def run():
        await lim.report_block(reason="1015", retry_after=3583)
        await lim.wait()

    with pytest.raises(BlockedTooLongError) as info:
        asyncio.run(run())
    assert info.value.seconds > 3000


def test_limiter_set_cooldown_substitui_espera_apos_troca_de_proxy():
    cfg = RateLimitConfig(base_delay_seconds=0.01, max_block_wait_seconds=60.0)
    lim = RateLimiter(cfg)

    async def run():
        await lim.report_block(reason="1015", retry_after=3583)
        await lim.set_cooldown(0.0)  # novo IP: a espera do IP antigo nao vale mais
        start = time.monotonic()
        await lim.wait()
        return time.monotonic() - start

    assert asyncio.run(run()) < 2.0


def test_limiter_mark_done_conta_intervalo_a_partir_do_fim_da_pagina():
    cfg = RateLimitConfig(base_delay_seconds=0.3, jitter_ratio=0.0)
    lim = RateLimiter(cfg)

    async def run():
        await lim.wait()
        await asyncio.sleep(0.4)  # "pagina" demorou mais que o intervalo
        lim.mark_done()
        start = time.monotonic()
        await lim.wait()
        return time.monotonic() - start

    assert asyncio.run(run()) >= 0.25


# =============================================================================
# Classificacao de bloqueios
# =============================================================================

def test_classifica_429_como_rate_limit_com_retry_after():
    block = classify_response(429, {"Retry-After": "3583"}, body=BLOCK_1015)
    assert block.kind == RATE_LIMIT
    assert block.retry_after == 3583.0


def test_classifica_pagina_1015_mesmo_sem_status_429():
    block = classify_response(
        200, {}, title="Access denied | x used Cloudflare to restrict access", body=BLOCK_1015,
    )
    assert block is not None and block.kind == RATE_LIMIT


def test_pagina_normal_do_confluence_nunca_e_bloqueio():
    """Texto 'Ray ID:' ou script challenge-platform numa pagina 200 legitima."""
    body = "informe o Ray ID: abc /cdn-cgi/challenge-platform/scripts/jsd/main.js cf-error"
    assert classify_response(
        200, {}, title="CFGTRIB - TDN", body=body, is_confluence_page=True,
    ) is None


def test_classifica_challenge_bloqueio_waf_e_5xx():
    assert classify_response(503, {"cf-mitigated": "challenge"}).kind == CHALLENGE
    assert classify_response(
        403, {}, title="Just a moment...", body="checking your browser",
    ).kind == CHALLENGE
    assert classify_response(
        403, {}, title="Attention Required! | Cloudflare",
        body="Sorry, you have been blocked Cloudflare Ray ID",
    ).kind == ACCESS_DENIED
    assert classify_response(522, {}, title="522", body="cloudflare").kind == SERVER_ERROR


def test_403_e_404_da_propria_aplicacao_nao_sao_bloqueio():
    assert classify_response(
        403, {}, title="Access Denied - TDN", body="access denied", is_confluence_page=True,
    ) is None
    assert classify_response(404, {}, title="Page Not Found", body="not found") is None


@pytest.mark.parametrize("value,expected", [
    ("42", 42.0), (" 3583 ", 3583.0), (0, 0.0),
    ("abc", None), ("-1", None), ("999999", None), (None, None), ("", None),
])
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_data_http():
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480.0) == 10.0


def test_erros_de_rede_transitorios_sao_reconhecidos():
    denied = Exception(
        "Page.goto: net::ERR_NETWORK_ACCESS_DENIED at https://tdn.totvs.com/x\nCall log:\n - nav"
    )
    assert is_transient_network_error(denied)
    assert not is_proxy_error(denied)
    assert is_transient_network_error(Exception("APIRequestContext.get: read ECONNRESET"))
    assert is_proxy_error(Exception("Page.goto: net::ERR_PROXY_CONNECTION_FAILED at http://x"))
    assert is_proxy_error(Exception("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at http://x"))
    assert not is_transient_network_error(Exception("Page.goto: Timeout 90000ms exceeded."))
    assert not is_transient_network_error(Exception("File not found"))


def test_error_headline_remove_urls_e_call_log():
    exc = Exception("Page.goto: net::ERR_X at https://t/display/PROT/Registro+C503\nCall log:\n 429")
    headline = error_headline(exc)
    assert "c503" not in headline and "429" not in headline
    assert short_error(exc) == "Page.goto: net::ERR_X at https://t/display/PROT/Registro+C503"


def test_is_blocking_error_ignora_codigo_dentro_da_url():
    """Titulo de pagina como 'Registro C503' nao e um HTTP 503."""
    assert not is_blocking_error(
        Exception("Page.pdf: Protocol error at https://t/display/PROT/Registro+C503")
    )
    assert not is_blocking_error(Exception("falha em https://t/display/PROT/FINA+429+teste"))
    assert is_blocking_error(Exception("HTTP 503 at https://t/display/PROT/Pagina"))


# =============================================================================
# Contabilidade de rede por pagina (sub-recursos bloqueados)
# =============================================================================

def _response(url, status, resource_type="image", navigation=False, headers=None):
    request = MagicMock()
    request.is_navigation_request.return_value = navigation
    request.resource_type = resource_type
    response = MagicMock()
    response.url = url
    response.status = status
    response.request = request
    response.headers = headers or {}
    return response


def test_page_stats_registra_imagem_com_429():
    stats = PageNetStats()
    stats.begin("https://tdn.totvs.com/display/PROT/Pagina")
    stats.on_response(_response("https://tdn.totvs.com/download/a.png", 200))
    stats.on_response(_response(
        "https://tdn.totvs.com/download/b.png", 429, headers={"retry-after": "30"},
    ))
    assert [(status, retry) for _u, status, retry in stats.blocked] == [(429, 30.0)]
    assert stats.retry_after() == 30.0


def test_page_stats_ignora_navegacao_terceiros_e_5xx_de_xhr():
    stats = PageNetStats()
    stats.begin("https://tdn.totvs.com/display/PROT/Pagina")
    stats.on_response(_response("https://tdn.totvs.com/x", 429, "document", navigation=True))
    stats.on_response(_response("https://www.youtube.com/embed/x", 429))
    stats.on_response(_response("https://tdn.totvs.com/rest/likes", 503, "xhr"))
    assert stats.blocked == []
    stats.on_response(_response("https://tdn.totvs.com/download/c.png", 503))
    assert len(stats.blocked) == 1


def test_page_stats_conta_requests_do_host_e_zera_a_cada_navegacao():
    stats = PageNetStats()
    stats.begin("https://tdn.totvs.com/a")
    for url in ("https://tdn.totvs.com/a", "https://tdn.totvs.com/img.png", "https://cdn.x/y.js"):
        request = MagicMock()
        request.url = url
        stats.on_request(request)
    assert stats.requests == 2
    stats.begin("https://tdn.totvs.com/b")
    assert stats.requests == 0 and stats.blocked == []


def test_page_stats_falha_de_imagem_por_rede():
    stats = PageNetStats()
    stats.begin("https://tdn.totvs.com/a")
    failed = MagicMock()
    failed.url = "https://tdn.totvs.com/img.png"
    failed.resource_type = "image"
    failed.failure = "net::ERR_CONNECTION_RESET"
    stats.on_request_failed(failed)
    blocked_by_us = MagicMock()
    blocked_by_us.url = "https://tdn.totvs.com/font.woff"
    blocked_by_us.resource_type = "font"
    blocked_by_us.failure = "net::ERR_BLOCKED_BY_CLIENT"
    stats.on_request_failed(blocked_by_us)
    assert len(stats.failed) == 1


def test_get_page_stats_de_page_desconhecida_nao_quebra():
    assert get_page_stats(object()).requests == 0


# =============================================================================
# URLs: mesma pagina, uma chave
# =============================================================================

def test_display_url_variant():
    public = "https://tdn.totvs.com/display/public/PROT/A+B"
    plain = "https://tdn.totvs.com/display/PROT/A+B"
    assert display_url_variant(public) == plain
    assert display_url_variant(plain) == public
    assert display_url_variant("https://tdn.totvs.com/pages/viewpage.action?pageId=1") is None


def test_start_url_key_ignora_diferencas_cosmeticas():
    base = "https://tdn.totvs.com/display/public/PROT/CFGTRIB+-+P12"
    same = [
        base + "/", base.replace("/public", ""), base.replace("https", "http"),
        base.replace("tdn.", "TDN."), base + "?src=sidebar",
    ]
    assert all(start_url_key(url) == start_url_key(base) for url in same)
    assert start_url_key(base + "2") != start_url_key(base)


def test_checkpoint_mantem_job_quando_url_inicial_e_equivalente(tmp_path):
    stored = "https://tdn.totvs.com/display/public/PROT/Raiz"
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps({
        "version": 2, "start_url": stored, "crawl_complete": True,
        "mapped_urls": [stored], "exported": {}, "failures": {},
    }), encoding="utf-8")

    same = Checkpoint(tmp_path, "https://tdn.totvs.com/display/PROT/Raiz/")
    assert same.previous_start_url is None
    assert same.manifest.mapped_urls == [stored]
    assert same.manifest.start_url == stored

    other = Checkpoint(tmp_path, "https://tdn.totvs.com/display/public/PROT/Outra")
    assert other.previous_start_url == stored


def test_checkpoint_reaproveita_pdf_gravado_na_outra_forma_da_url(tmp_path):
    """Manifests antigos tem /display/PROT/X; o crawl novo produz /display/public/PROT/X."""
    pages = tmp_path / "pages"
    pages.mkdir()
    _valid_pdf(pages / "0002-x.pdf")
    old_key = "https://tdn.totvs.com/display/PROT/X"
    new_key = "https://tdn.totvs.com/display/public/PROT/X"
    ck = Checkpoint(tmp_path, "https://tdn.totvs.com/display/public/PROT/Raiz")
    ck.manifest.exported[old_key] = {"filename": "0002-x.pdf", "title": "X"}

    assert ck.is_exported(new_key, pages)
    assert ck.get_exported_entry(new_key)["filename"] == "0002-x.pdf"

    # Re-exportar pela forma nova nao deixa a pagina com duas chaves.
    ck.record_export(new_key, "0002-x.pdf", "X", 4000, 1.0)
    assert new_key in ck.manifest.exported and old_key not in ck.manifest.exported


def test_parse_scope_display_sem_public():
    """Antes o TITULO virava o espaco e os filhos saiam todos do escopo."""
    scope = parse_scope("https://tdn.totvs.com/display/PROT/Pagina+Inicial")
    assert scope.space_key == "PROT"
    assert scope.path_prefix == "/display/PROT/"


def test_record_failure_guarda_so_o_cabecalho_do_erro(tmp_path):
    ck = Checkpoint(tmp_path, "https://x.com/a")
    ck.record_failure("https://x.com/a", "Page.goto: net::ERR_X\nCall log:\n  cookie: segredo=1")
    assert ck.manifest.failures["https://x.com/a"]["error"] == "Page.goto: net::ERR_X"


def test_chrome_user_agent_acompanha_versao_do_browser():
    ua = chrome_user_agent("148.0.7778.96")
    assert "Chrome/148.0.0.0" in ua and "Headless" not in ua and "Firefox" not in ua
    assert "Chrome/" in chrome_user_agent(None)


# =============================================================================
# Crawl: REST API, folhas e invariantes de completude
# =============================================================================

def test_child_refs_guardam_id_e_identificam_folhas():
    scope = parse_scope("https://tdn.totvs.com/display/public/PROT/Raiz")
    results = [
        {"id": "10", "_links": {"webui": "/display/PROT/Com+Filhos"},
         "children": {"page": {"results": [{"id": "11"}], "size": 1}}},
        {"id": "20", "_links": {"webui": "/pages/viewpage.action?pageId=20"},
         "children": {"page": {"results": [], "size": 0}}},
        {"id": "30", "_links": {"webui": "/display/PROT/Sem+Expand"}},
        {"id": "40", "_links": {}},
    ]
    refs = _child_refs_from_results(results, "https://tdn.totvs.com", scope)
    assert [(r.page_id, r.is_leaf) for r in refs] == [
        ("10", False), ("20", True), ("30", None),
    ]
    # webui vem como /display/PROT/...; o job usa /display/public/PROT/...
    assert refs[0].url == "https://tdn.totvs.com/display/public/PROT/Com+Filhos"


def test_enqueue_children_registra_ids_folhas_e_normaliza_forma_da_url():
    scope = parse_scope("https://tdn.totvs.com/display/public/PROT/Raiz")
    state = CrawlState(queue=deque(), queued=set())
    _enqueue_children(
        [
            ChildRef("https://tdn.totvs.com/display/PROT/A", page_id="1", is_leaf=True),
            ChildRef("/display/public/PROT/A"),  # mesma pagina vinda do DOM
            ChildRef("/pages/viewpage.action?pageId=2", page_id="2", is_leaf=False),
            ChildRef("https://outro.site/x"),
        ],
        "https://tdn.totvs.com/display/public/PROT/Raiz", state, scope,
    )
    a = "https://tdn.totvs.com/display/public/PROT/A"
    b = "https://tdn.totvs.com/pages/viewpage.action?pageId=2"
    assert list(state.queue) == [a, b]
    assert state.page_ids == {a: "1", b: "2"}
    assert state.known_leaves == {a}


def test_page_id_from_url():
    assert _page_id_from_url("https://t/pages/viewpage.action?pageId=123") == "123"
    assert _page_id_from_url("https://t/display/PROT/X") is None


def test_restore_nao_descarta_no_que_aguardava_nova_tentativa(tmp_path, logger):
    """Regressao: no bloqueado fica em mapped_urls E na fila. Ao retomar ele era
    tratado como ja visto e a subarvore sumia em silencio."""
    ck = Checkpoint(tmp_path, "https://x.com/S")
    ck.manifest.mapped_urls = ["https://x.com/S", "https://x.com/A", "https://x.com/X"]
    ck.manifest.crawl_queue = ["https://x.com/Q", "https://x.com/X"]
    ck.manifest.crawl_seen = ["https://x.com/S", "https://x.com/A"]
    ck.manifest.page_ids = {"https://x.com/X": "77"}
    state = CrawlState(queue=deque(), queued=set())

    assert _restore_state_from_checkpoint(state, ck, "https://x.com/S", logger)
    assert list(state.queue) == ["https://x.com/Q", "https://x.com/X"]
    assert "https://x.com/X" not in state.seen
    assert state.ordered_urls == ["https://x.com/S", "https://x.com/A", "https://x.com/X"]
    assert state.page_ids["https://x.com/X"] == "77"


def test_finalize_so_marca_completo_sem_pendencias(tmp_path, logger):
    ck = Checkpoint(tmp_path, "https://x.com/S")
    state = CrawlState(queue=deque(), queued=set())
    state.ordered_urls = ["https://x.com/S", "https://x.com/A"]
    state.seen = set(state.ordered_urls)
    state.page_ids = {"https://x.com/A": "5"}

    urls, complete, note = _finalize_crawl(state, ck, None, logger)
    assert complete and note == ""
    assert ck.manifest.crawl_complete is True
    assert ck.manifest.page_ids == {"https://x.com/A": "5"}


def test_finalize_com_no_pendente_nao_marca_completo(tmp_path, logger):
    """No cujos filhos nao foram listados: o crawl NAO e completo e o no fica
    na fila para o proximo run (antes era marcado completo e a subarvore sumia)."""
    ck = Checkpoint(tmp_path, "https://x.com/S")
    state = CrawlState(queue=deque(), queued=set())
    state.ordered_urls = ["https://x.com/S", "https://x.com/A"]
    state.seen = {"https://x.com/S"}
    state.unresolved = ["https://x.com/A"]

    urls, complete, note = _finalize_crawl(state, ck, None, logger)
    assert not complete and note
    assert ck.manifest.crawl_complete is False
    assert ck.manifest.crawl_queue == ["https://x.com/A"]
    assert "https://x.com/A" not in ck.manifest.crawl_seen


def test_finalize_remapeamento_incompleto_preserva_mapa_anterior(tmp_path, logger):
    """--update que nao termina nao pode trocar um mapa completo por um parcial."""
    ck = Checkpoint(tmp_path, "https://x.com/S")
    previous = ["https://x.com/S", "https://x.com/A", "https://x.com/B"]
    ck.record_crawl_complete(previous)
    state = CrawlState(queue=deque(["https://x.com/A"]), queued={"https://x.com/A"})
    state.ordered_urls = ["https://x.com/S"]
    state.stop_reason = "bloqueio persistente"

    urls, complete, note = _finalize_crawl(state, ck, None, logger, staged_previous=previous)
    assert urls == previous and not complete
    assert ck.manifest.crawl_complete is True
    assert ck.manifest.mapped_urls == previous


def test_finalize_limitado_por_max_pages_nao_e_tratado_como_erro(tmp_path, logger):
    ck = Checkpoint(tmp_path, "https://x.com/S")
    state = CrawlState(queue=deque(["https://x.com/B"]), queued={"https://x.com/B"})
    state.ordered_urls = ["https://x.com/S", "https://x.com/A"]
    state.stop_reason = "max_pages"
    urls, complete, note = _finalize_crawl(state, ck, 2, logger)
    assert urls == state.ordered_urls and complete and note == ""
    assert ck.manifest.crawl_complete is False  # manifest segue retomavel


# =============================================================================
# Descoberta de jobs e CLI
# =============================================================================

def _write_job(folder: Path, *, complete: bool = True, exported: int = 1, mapped: int = 1) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    urls = [f"https://x.com/p{i}" for i in range(mapped)]
    (folder / MANIFEST_FILENAME).write_text(json.dumps({
        "version": 2, "start_url": "https://x.com/p0", "crawl_complete": complete,
        "mapped_urls": urls, "crawl_queue": [], "crawl_seen": [],
        "exported": {u: {"filename": f"{i}.pdf"} for i, u in enumerate(urls[:exported])},
        "failures": {}, "last_updated": "2026-01-01",
    }), encoding="utf-8")


def test_find_jobs_varre_varias_raizes_e_ignora_pastas_de_infra(tmp_path):
    """Job criado fora de output/ (ex: pasta CFGTRIB na raiz) tambem aparece."""
    _write_job(tmp_path / "output" / "A", exported=0)
    _write_job(tmp_path / "CFGTRIB", exported=0)
    _write_job(tmp_path / ".venv" / "lib", exported=0)
    (tmp_path / "ext").mkdir()
    (tmp_path / "ext" / MANIFEST_FILENAME).write_text('{"name": "extensao"}', encoding="utf-8")

    found = find_jobs([tmp_path / "output", tmp_path])
    assert {job.output_dir.name for job in found} == {"A", "CFGTRIB"}


def test_find_jobs_include_complete(tmp_path):
    _write_job(tmp_path / "pronto", exported=1, mapped=1)
    _write_job(tmp_path / "faltando", exported=1, mapped=2)
    assert [j.output_dir.name for j in find_jobs([tmp_path])] == ["faltando"]
    assert {j.output_dir.name for j in find_jobs([tmp_path], include_complete=True)} == {
        "pronto", "faltando",
    }


def test_cli_resume_e_proxy_documentados():
    result = runner.invoke(app, ["resume", "--help"])
    assert result.exit_code == 0
    for flag in ["--output-dir", "--mode", "--proxy-file", "--max-block-wait"]:
        assert flag in result.stdout
    result = runner.invoke(app, ["run", "--help"])
    for flag in ["--proxy", "--proxy-file", "--no-proxy", "--max-block-wait"]:
        assert flag in result.stdout
    assert runner.invoke(app, ["proxy", "--help"]).exit_code == 0


def test_cli_resume_sem_manifest_falha_com_mensagem(tmp_path):
    result = runner.invoke(app, ["resume", "--output-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "manifest.json" in result.stdout


def test_cli_resume_modo_invalido(tmp_path):
    result = runner.invoke(app, ["resume", "--output-dir", str(tmp_path), "--mode", "xyz"])
    assert result.exit_code == 2


def test_cli_run_rejeita_proxy_invalido_em_vez_de_ignorar(tmp_path):
    """Proxy digitado errado aborta: nunca roda 'sem proxy' em silencio."""
    result = runner.invoke(app, [
        "run", "https://tdn.totvs.com/display/public/PROT/X",
        "--output-dir", str(tmp_path / "job"), "--proxy", "ftp://host:21",
    ])
    assert result.exit_code == 2
    assert "Proxy invalido" in result.stdout


def test_cli_proxy_add_list_clear(tmp_path):
    file = tmp_path / "proxies.txt"
    result = runner.invoke(app, ["proxy", "add", "user:segredo@host:8080", "--file", str(file)])
    assert result.exit_code == 0, result.stdout
    assert "segredo" not in result.stdout
    assert runner.invoke(app, ["proxy", "add", "direct", "--file", str(file)]).exit_code == 0
    assert runner.invoke(app, ["proxy", "add", "ftp://x:1", "--file", str(file)]).exit_code == 2

    listed = runner.invoke(app, ["proxy", "list", "--file", str(file)])
    assert listed.exit_code == 0
    assert "host:8080" in listed.stdout and "segredo" not in listed.stdout
    assert "conexao direta" in listed.stdout

    assert runner.invoke(app, ["proxy", "clear", "--file", str(file), "--yes"]).exit_code == 0
    assert not file.exists()


# =============================================================================
# Export: conferencia de sub-recursos antes do PDF
# =============================================================================

def _page_with_stats(blocked=(), failed=(), broken_images=0):
    """Page falsa registrada na contabilidade de rede de src.browser."""
    from unittest.mock import AsyncMock

    from src import browser

    page = MagicMock()
    page.evaluate = AsyncMock(return_value=broken_images)
    stats = PageNetStats(host="tdn.totvs.com")
    stats.blocked = list(blocked)
    stats.failed = list(failed)
    browser._page_stats[page] = stats
    return page


def test_verify_assets_imagem_com_429_e_sempre_rate_limit(logger):
    """Mesmo em modo tolerante: PDF sem imagens por rate limit nunca e aceito."""
    from src.pdf_exporter import RateLimitedError, _verify_page_assets

    page = _page_with_stats(blocked=[("https://tdn.totvs.com/a.png", 429, 30.0)])
    for lenient in (False, True):
        with pytest.raises(RateLimitedError) as info:
            asyncio.run(_verify_page_assets(page, logger, "u", lenient=lenient))
        assert info.value.retry_after == 30.0


def test_verify_assets_5xx_repete_sem_virar_bloqueio_e_depois_tolera(logger):
    """5xx numa imagem nao e ban do IP: repete rapido; se persistir, gera o PDF."""
    from src.pdf_exporter import (
        RateLimitedError,
        TransientNetworkError,
        _verify_page_assets,
    )

    page = _page_with_stats(blocked=[("https://tdn.totvs.com/a.png", 503, None)])
    with pytest.raises(TransientNetworkError) as info:
        asyncio.run(_verify_page_assets(page, logger, "u", lenient=False))
    assert not isinstance(info.value, RateLimitedError)
    asyncio.run(_verify_page_assets(page, logger, "u", lenient=True))  # so avisa


def test_verify_assets_so_queda_de_rede_real_conta_como_falha(logger):
    from src.pdf_exporter import TransientNetworkError, _verify_page_assets

    generic = _page_with_stats(failed=[("https://tdn.totvs.com/f.woff", "net::ERR_FAILED")])
    asyncio.run(_verify_page_assets(generic, logger, "u"))  # nao levanta

    dropped = _page_with_stats(
        failed=[("https://tdn.totvs.com/a.png", "net::ERR_CONNECTION_RESET")],
    )
    with pytest.raises(TransientNetworkError):
        asyncio.run(_verify_page_assets(dropped, logger, "u"))


def test_verify_assets_pagina_limpa_passa(logger):
    from src.pdf_exporter import _verify_page_assets

    asyncio.run(_verify_page_assets(_page_with_stats(broken_images=2), logger, "u"))


# =============================================================================
# Export: page do worker x relancamento do browser (rodizio de proxy)
# =============================================================================

def _fake_session(generation=0):
    from unittest.mock import AsyncMock

    from src.browser import BrowserSession

    session = BrowserSession()
    session.generation = generation
    session.context = MagicMock()
    created = []

    async def new_page():
        page = MagicMock()
        page.is_closed.return_value = False
        created.append(page)
        return page

    session.context.new_page = AsyncMock(side_effect=new_page)
    return session, created


def test_slot_page_reutiliza_page_valida():
    from src.pdf_exporter import _PageSlot, _slot_page

    session, created = _fake_session()
    page = MagicMock()
    page.is_closed.return_value = False
    slot = _PageSlot(page=page, generation=0)
    cfg = MagicMock(timeout_ms=1000)
    assert asyncio.run(_slot_page(slot, session, cfg)) is page
    assert created == []


def test_slot_page_cria_outra_apos_relancamento_do_browser():
    """Geracao mudou (troca de proxy por outro worker): a page antiga nao vale."""
    from src.pdf_exporter import _PageSlot, _slot_page

    session, created = _fake_session(generation=1)
    old = MagicMock()
    old.is_closed.return_value = False
    slot = _PageSlot(page=old, generation=0)
    cfg = MagicMock(timeout_ms=1000)
    page = asyncio.run(_slot_page(slot, session, cfg))
    assert page is created[0] and page is not old
    assert slot.generation == 1


def test_slot_page_sequencial_adota_a_page_da_nova_sessao():
    from src.pdf_exporter import _PageSlot, _slot_page

    session, created = _fake_session(generation=1)
    session.page = MagicMock()
    session.page.is_closed.return_value = False
    slot = _PageSlot(page=MagicMock(), generation=0)
    cfg = MagicMock(timeout_ms=1000)
    assert asyncio.run(_slot_page(slot, session, cfg, adopt=True)) is session.page
    assert created == []


# =============================================================================
# Crawl: reservas da REST API e interrupcao
# =============================================================================

def test_http_get_usa_apirequestcontext_quando_o_fetch_falha():
    from unittest.mock import AsyncMock

    from src.crawler import _http_get

    response = MagicMock()
    response.status = 200
    response.headers = {"content-type": "application/json"}
    response.text = AsyncMock(return_value='{"results": []}')
    page = MagicMock()
    page.url = "https://tdn.totvs.com/display/PROT/Raiz"
    page.evaluate = AsyncMock(return_value={"error": "Failed to fetch"})
    page.request.get = AsyncMock(return_value=response)

    status, _headers, body, error = asyncio.run(
        _http_get(page, "https://tdn.totvs.com/rest/api/content/1/child/page")
    )
    assert (status, body, error) == (200, '{"results": []}', "")
    page.request.get.assert_awaited_once()


def test_http_get_resposta_429_do_fetch_nao_e_repetida_pela_reserva():
    """Uma RESPOSTA de bloqueio nao pode gerar um segundo request dentro do ban."""
    from unittest.mock import AsyncMock

    from src.crawler import _http_get

    page = MagicMock()
    page.url = "https://tdn.totvs.com/display/PROT/Raiz"
    page.evaluate = AsyncMock(return_value={
        "status": 429, "retryAfter": "600", "mitigated": None, "body": "Error 1015",
    })
    page.request.get = AsyncMock()

    status, headers, _body, _error = asyncio.run(
        _http_get(page, "https://tdn.totvs.com/rest/api/content/1/child/page")
    )
    assert status == 429 and headers == {"retry-after": "600"}
    page.request.get.assert_not_awaited()


def test_api_sem_suporte_a_expand_continua_sem_ele(monkeypatch, logger):
    """HTTP 400 no expand=children.page: segue sem expand em vez de falhar."""
    from src import crawler

    calls = []

    async def fake_get(page, url):
        calls.append(url)
        if "expand=" in url:
            return 400, {}, '{"message": "bad expand"}', ""
        return 200, {}, json.dumps({"results": [
            {"id": "2", "_links": {"webui": "/display/PROT/Filho"}},
        ]}), ""

    monkeypatch.setattr(crawler, "_http_get", fake_get)
    scope = parse_scope("https://tdn.totvs.com/display/public/PROT/Raiz")
    state = CrawlState(queue=deque(), queued=set())

    async def run():
        first = await crawler._api_list_children(
            MagicMock(), "1", "https://tdn.totvs.com/display/public/PROT/Raiz",
            scope, logger, state,
        )
        second = await crawler._api_list_children(
            MagicMock(), "9", "https://tdn.totvs.com/display/public/PROT/Raiz",
            scope, logger, state,
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.status == "ok" and len(first.children) == 1
    assert first.children[0].is_leaf is None  # sem expand nao da para saber
    assert state.api_expand is False
    assert second.status == "ok"
    assert ["expand=" in url for url in calls] == [True, False, False]


def test_requeue_in_flight_devolve_no_interrompido_para_a_frente_da_fila():
    from src.crawler import _requeue_in_flight

    state = CrawlState(queue=deque(["https://x.com/B"]), queued={"https://x.com/B"})
    state.seen = {"https://x.com/S", "https://x.com/A"}
    state.in_flight = "https://x.com/A"
    _requeue_in_flight(state)
    assert list(state.queue) == ["https://x.com/A", "https://x.com/B"]
    assert "https://x.com/A" not in state.seen and state.in_flight is None


# =============================================================================
# Desistencias antigas do crawl e comando reexport
# =============================================================================

def test_mapa_completo_remove_desistencias_antigas_do_crawl(tmp_path):
    ck = Checkpoint(tmp_path, "https://x.com/S")
    ck.manifest.failures = {
        "https://x.com/A": {"error": "crawl timeout apos 2 tentativas intra-run", "attempts": 1},
        "https://x.com/B": {"error": "HTTP 429", "attempts": 1},
    }
    assert ck.crawl_giveups() == ["https://x.com/A"]
    ck.record_crawl_complete(["https://x.com/S", "https://x.com/A", "https://x.com/B"])
    assert list(ck.manifest.failures) == ["https://x.com/B"]  # falha de export fica


def test_cli_reexport_marca_pagina_para_ser_refeita(tmp_path):
    urls = [f"https://x.com/p{i}" for i in range(1, 4)]
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps({
        "version": 2, "start_url": urls[0], "crawl_complete": True,
        "mapped_urls": urls,
        "exported": {u: {"filename": f"000{i}-p.pdf", "title": "p"} for i, u in enumerate(urls, 1)},
        "failures": {},
    }), encoding="utf-8")

    result = runner.invoke(app, ["reexport", "2", "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    data = json.loads((tmp_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert urls[1] not in data["exported"]
    assert urls[0] in data["exported"] and urls[2] in data["exported"]
    assert list(tmp_path.glob("manifest.json.bak.*")), "deveria ter criado backup"
    assert not (tmp_path / ".extrator.lock").exists()

    assert runner.invoke(app, ["reexport", "9", "--output-dir", str(tmp_path)]).exit_code == 2


# =============================================================================
# CLI: resume/run repassam os parametros certos ao pipeline (pipeline falso)
# =============================================================================

@pytest.fixture
def fake_pipeline(monkeypatch):
    """Substitui run_pipeline por um stub que so registra os argumentos."""
    import src.main as main_module

    calls: list[dict] = []

    async def stub(**kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(main_module, "run_pipeline", stub)
    return calls


def _job_manifest(folder: Path, *, complete: bool = True, max_pages=None, missing: int = 1) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    start = "https://tdn.totvs.com/display/public/PROT/Raiz"
    urls = [start] + [f"https://tdn.totvs.com/pages/viewpage.action?pageId={i}" for i in range(1, 4)]
    (folder / MANIFEST_FILENAME).write_text(json.dumps({
        "version": 2, "start_url": start, "crawl_complete": complete,
        "crawl_max_pages": max_pages, "mapped_urls": urls,
        "crawl_queue": [] if complete else [urls[-1]], "crawl_seen": [],
        "exported": {u: {"filename": f"{i:04d}-p.pdf"} for i, u in enumerate(urls[:len(urls) - missing], 1)},
        "failures": {}, "last_updated": "2026-10-13T14:00:00+00:00",
    }), encoding="utf-8")
    return start


def test_cli_resume_le_url_limite_e_nome_do_consolidado_do_manifest(tmp_path, fake_pipeline):
    start = _job_manifest(tmp_path / "job", max_pages=50)
    manifest_path = tmp_path / "job" / MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["consolidated_name"] = "MeuConsolidado.pdf"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(app, [
        "resume", "--output-dir", str(tmp_path / "job"), "--mode", "retry-failed", "--no-proxy",
    ])
    assert result.exit_code == 0, result.stdout
    (call,) = fake_pipeline
    assert call["start_url"] == start
    assert call["retry_failed_only"] is True
    assert call["force_recrawl"] is False and call["force_reexport"] is False
    assert call["max_pages"] == 50  # mesmo limite do job: nao forca re-crawl
    assert call["consolidated_name"] == "MeuConsolidado.pdf"
    assert call["proxies"] == [None]
    assert call["auto_confirm"] is True


def test_cli_resume_nao_adota_pdf_alheio_como_consolidado(tmp_path, fake_pipeline):
    """Um PDF qualquer guardado na pasta do job nao pode ser sobrescrito pelo
    consolidado; so um PDF gerado pelo proprio extrator e reconhecido."""
    from pypdf import PdfWriter

    _job_manifest(tmp_path / "job")
    stray = PdfWriter()
    stray.add_blank_page(width=200, height=200)
    with (tmp_path / "job" / "Parecer_fiscal.pdf").open("wb") as fp:
        stray.write(fp)

    args = ["resume", "--output-dir", str(tmp_path / "job"), "--no-proxy"]
    assert runner.invoke(app, args).exit_code == 0
    assert fake_pipeline[-1]["consolidated_name"] == "TDN_TOTVS_consolidado.pdf"

    ours = PdfWriter()
    ours.add_blank_page(width=200, height=200)
    ours.add_metadata({"/Producer": "Extrator TDN TOTVS (pypdf)"})
    with (tmp_path / "job" / "CFGTRIB.pdf").open("wb") as fp:
        ours.write(fp)
    assert runner.invoke(app, args).exit_code == 0
    assert fake_pipeline[-1]["consolidated_name"] == "CFGTRIB.pdf"

    assert runner.invoke(app, args + ["--consolidated-name", "Outro"]).exit_code == 0
    assert fake_pipeline[-1]["consolidated_name"] == "Outro.pdf"


@pytest.mark.parametrize("mode,recrawl,reexport", [
    ("auto", False, False), ("update", True, False), ("regenerate", True, True),
])
def test_cli_resume_modos(tmp_path, fake_pipeline, mode, recrawl, reexport):
    _job_manifest(tmp_path / "job")
    result = runner.invoke(app, [
        "resume", "--output-dir", str(tmp_path / "job"), "--mode", mode, "--no-proxy",
    ])
    assert result.exit_code == 0, result.stdout
    (call,) = fake_pipeline
    assert (call["force_recrawl"], call["force_reexport"]) == (recrawl, reexport)
    assert call["retry_failed_only"] is False
    assert call["consolidated_name"] == "TDN_TOTVS_consolidado.pdf"


def test_cli_resume_retry_failed_com_mapa_incompleto_retoma_o_crawl(tmp_path, fake_pipeline):
    """Sem mapa completo nao da para pular o mapeamento: retoma normalmente."""
    _job_manifest(tmp_path / "job", complete=False)
    result = runner.invoke(app, [
        "resume", "--output-dir", str(tmp_path / "job"), "--mode", "retry-failed", "--no-proxy",
    ])
    assert result.exit_code == 0, result.stdout
    assert fake_pipeline[0]["retry_failed_only"] is False


def test_cli_resume_sem_pasta_encontra_job_fora_de_output(tmp_path, fake_pipeline, monkeypatch):
    """Job criado na pasta atual (fora de output/) e encontrado e usado."""
    monkeypatch.chdir(tmp_path)
    start = _job_manifest(tmp_path / "CFGTRIB")
    result = runner.invoke(app, ["resume", "--mode", "retry-failed", "--no-proxy"], input="1\n")
    assert result.exit_code == 0, result.stdout
    (call,) = fake_pipeline
    assert call["start_url"] == start
    assert Path(call["output_dir"]).name == "CFGTRIB"


def test_cli_resume_nunca_inicia_um_run_sem_escolha_explicita(tmp_path, fake_pipeline, monkeypatch):
    """Mesmo com um unico job, o picker pergunta: sem resposta nada roda
    (um run automatico faria requests ao servidor sem ninguem confirmar)."""
    monkeypatch.chdir(tmp_path)
    _job_manifest(tmp_path / "CFGTRIB")

    no_answer = runner.invoke(app, ["resume", "--no-proxy"])
    assert no_answer.exit_code != 0
    cancelled = runner.invoke(app, ["resume", "--no-proxy"], input="0\n")
    assert cancelled.exit_code == 4
    assert fake_pipeline == []


def test_cli_resume_aceita_caminho_de_job_fora_das_pastas_varridas(tmp_path, fake_pipeline, monkeypatch):
    elsewhere = tmp_path / "longe" / "MeuJob"
    start = _job_manifest(elsewhere)
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    result = runner.invoke(app, ["resume", "--no-proxy"], input=f"{elsewhere}\n")
    assert result.exit_code == 0, result.stdout
    assert fake_pipeline[0]["start_url"] == start


def test_cli_resume_sem_jobs_avisa_e_nao_roda(tmp_path, fake_pipeline, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["resume", "--no-proxy"], input="0\n")
    assert result.exit_code == 4  # "nada a fazer": o menu volta sem encerrar
    assert "Nenhum job" in result.stdout
    assert fake_pipeline == []


def test_cli_run_repassa_lista_de_proxies_normalizada(tmp_path, fake_pipeline):
    result = runner.invoke(app, [
        "run", "https://tdn.totvs.com/display/public/PROT/X",
        "--output-dir", str(tmp_path / "job"),
        "--proxy", "user:pw@a.example:8080", "--proxy", "b.example:3128,direct",
        "--max-block-wait", "120",
    ])
    assert result.exit_code == 0, result.stdout
    (call,) = fake_pipeline
    assert call["proxies"] == ["http://user:pw@a.example:8080", "http://b.example:3128", None]
    assert call["rate_limit"].max_block_wait_seconds == 120.0
    assert "pw" not in result.stdout


def test_cli_run_usa_arquivo_de_proxies(tmp_path, fake_pipeline):
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text("# meus proxies\nhost1:8080\ndirect\n", encoding="utf-8")
    result = runner.invoke(app, [
        "run", "https://tdn.totvs.com/display/public/PROT/X",
        "--output-dir", str(tmp_path / "job"), "--proxy-file", str(proxy_file),
    ])
    assert result.exit_code == 0, result.stdout
    assert fake_pipeline[0]["proxies"] == ["http://host1:8080", None]

    missing = runner.invoke(app, [
        "run", "https://tdn.totvs.com/display/public/PROT/X",
        "--output-dir", str(tmp_path / "job"), "--proxy-file", str(tmp_path / "nao-existe.txt"),
    ])
    assert missing.exit_code == 2



# =============================================================================
# Pipeline: consolidado e completude (crawl/export substituidos por stubs)
# =============================================================================

def _blank_pdf(path: Path, pages: int = 40) -> None:
    """PDF real e valido (>3KB) para o merge."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=300, height=300)
    writer.add_metadata({"/Title": "x" * 3000})
    with path.open("wb") as fp:
        writer.write(fp)


def _pipeline_job(tmp_path: Path, count: int = 3):
    """Job completo em disco: `count` paginas com PDF valido."""
    job = tmp_path / "job"
    pages = job / "pages"
    pages.mkdir(parents=True)
    start = "https://tdn.totvs.com/display/public/PROT/Raiz"
    urls = [start] + [f"https://tdn.totvs.com/pages/viewpage.action?pageId={i}" for i in range(1, count)]
    exported = {}
    for i, url in enumerate(urls, start=1):
        name = f"{i:04d}-pagina.pdf"
        _blank_pdf(pages / name)
        exported[url] = {"filename": name, "title": f"Pagina {i}"}
    (job / MANIFEST_FILENAME).write_text(json.dumps({
        "version": 2, "start_url": start, "crawl_complete": True, "mapped_urls": urls,
        "crawl_queue": [], "crawl_seen": [], "exported": exported, "failures": {},
    }), encoding="utf-8")
    return job, start, urls


def _run_stubbed_pipeline(monkeypatch, job, start, *, crawl_result, export_result, **kwargs):
    """Roda run_pipeline com crawl e export falsos (sem browser, sem rede)."""
    import src.main as main_module
    from src.utils import get_console

    async def fake_crawl(**_kwargs):
        return crawl_result

    async def fake_export(**_kwargs):
        return export_result

    monkeypatch.setattr(main_module, "crawl_confluence_tree", fake_crawl)
    monkeypatch.setattr(main_module, "export_pages_to_pdf", fake_export)
    params = dict(
        start_url=start, output_dir=job, consolidated_name="consolidado.pdf", headless=True,
        timeout_seconds=30, max_pages=None, slow_threshold_seconds=60.0, auto_confirm=True,
        force_recrawl=False, force_reexport=False, rate_limit=RateLimitConfig(),
        max_workers=1, quiet=True,
    )
    params.update(kwargs)
    try:
        return asyncio.run(main_module.run_pipeline(**params))
    finally:
        get_console().quiet = False


def _outline_titles(path: Path) -> list[str]:
    from pypdf import PdfReader

    return [item.title for item in PdfReader(str(path)).outline if hasattr(item, "title")]


def test_regenerate_com_falha_nao_encolhe_o_consolidado_e_sai_com_3(tmp_path, monkeypatch):
    """--regenerate em que uma pagina falha: o PDF antigo dela continua no
    consolidado e o run NAO termina como sucesso (antes: exit 0 e a pagina
    sumia do consolidado)."""
    from src.crawler import CrawlResult

    job, start, urls = _pipeline_job(tmp_path)
    pages = job / "pages"
    regenerated = [(pages / "0001-pagina.pdf", "Pagina 1"), (pages / "0002-pagina.pdf", "Pagina 2")]
    code = _run_stubbed_pipeline(
        monkeypatch, job, start,
        crawl_result=CrawlResult(urls, [], True, ""),
        export_result=(regenerated, [(urls[2], "Pagina de login")], []),
        force_recrawl=True, force_reexport=True,
    )
    assert code == 3
    assert _outline_titles(job / "consolidado.pdf") == ["Pagina 1", "Pagina 2", "Pagina 3"]


def test_run_abortado_no_meio_mantem_no_consolidado_o_que_ja_existia(tmp_path, monkeypatch):
    """Export que para cedo (circuit breaker) devolve so parte das entradas; o
    consolidado continua com todas as paginas que tem PDF valido."""
    from src.crawler import CrawlResult

    job, start, urls = _pipeline_job(tmp_path)
    code = _run_stubbed_pipeline(
        monkeypatch, job, start,
        crawl_result=CrawlResult(urls, [], True, ""),
        export_result=([(job / "pages" / "0001-pagina.pdf", "Pagina 1")], [], []),
    )
    assert code == 0  # nada faltando: todas tem PDF valido
    assert len(_outline_titles(job / "consolidado.pdf")) == 3


def test_update_interrompido_orienta_a_repetir_a_atualizacao(tmp_path, monkeypatch, capsys):
    """Re-mapeamento que nao termina mantem o mapa antigo; [F]/[1] nao o
    refazem, entao a orientacao tem de ser repetir a opcao [4]."""
    from src.crawler import CrawlResult
    from src.utils import get_console

    job, start, urls = _pipeline_job(tmp_path)
    entries = [(job / "pages" / f"{i:04d}-pagina.pdf", f"Pagina {i}") for i in range(1, 4)]
    import src.main as main_module

    printed: list[str] = []
    console = get_console()
    monkeypatch.setattr(console, "print", lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
    panels: list = []
    monkeypatch.setattr(
        main_module, "_print_incomplete_panel",
        lambda *a, **k: panels.append(k),
    )
    code = _run_stubbed_pipeline(
        monkeypatch, job, start,
        crawl_result=CrawlResult(urls, [], False, "bloqueio persistente do servidor"),
        export_result=(entries, [], []),
        force_recrawl=True, quiet=False,
    )
    assert code == 3
    assert panels and panels[0]["hint"] == ("[4] Verificar atualizacoes", "update")
    assert panels[0]["missing_urls"] == []


def test_dry_run_com_mapeamento_incompleto_sai_com_3(tmp_path, monkeypatch):
    from src.crawler import CrawlResult

    job, start, urls = _pipeline_job(tmp_path)
    code = _run_stubbed_pipeline(
        monkeypatch, job, start,
        crawl_result=CrawlResult(urls[:2], [], False, "bloqueio persistente"),
        export_result=([], [], []),
        dry_run=True,
    )
    assert code == 3


def test_retomar_mapeamento_interrompido_nao_e_reportado_como_paginas_novas(tmp_path, monkeypatch):
    """So um re-crawl de um mapa COMPLETO gera diff de paginas novas/removidas."""
    from src.crawler import CrawlResult
    import src.main as main_module

    job, start, urls = _pipeline_job(tmp_path)
    manifest_path = job / MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["crawl_complete"] = False
    data["mapped_urls"] = urls[:1]
    data["crawl_queue"] = urls[1:]
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    captured: dict = {}
    monkeypatch.setattr(main_module, "_print_summary_tables", lambda **k: captured.update(k))
    entries = [(job / "pages" / f"{i:04d}-pagina.pdf", f"Pagina {i}") for i in range(1, 4)]
    code = _run_stubbed_pipeline(
        monkeypatch, job, start,
        crawl_result=CrawlResult(urls, [], True, ""),
        export_result=(entries, [], []),
    )
    assert code == 0
    assert captured["added_urls"] == [] and captured["removed_urls"] == []


def test_job_com_falha_so_de_pagina_ja_exportada_nao_fica_pendente(tmp_path):
    """Registro antigo de falha de uma pagina que TEM PDF nao e trabalho pendente."""
    folder = tmp_path / "antigo"
    folder.mkdir()
    url = "https://x.com/display/PROT/A"
    (folder / MANIFEST_FILENAME).write_text(json.dumps({
        "version": 2, "start_url": url, "crawl_complete": True, "mapped_urls": [url],
        "exported": {"https://x.com/display/public/PROT/A": {"filename": "0001-a.pdf"}},
        "failures": {url: {"error": "crawl timeout apos 2 tentativas intra-run", "attempts": 1}},
        "last_updated": "2026-08-05",
    }), encoding="utf-8")
    assert find_jobs([tmp_path]) == []
    (job,) = find_jobs([tmp_path], include_complete=True)
    assert job.failure_count == 0 and not job.is_pending


def test_proxy_host_porta_usuario_senha_com_arroba_ou_dois_pontos():
    from src.proxy import ProxyConfigError, normalize_proxy_url, to_playwright_proxy

    assert to_playwright_proxy("10.0.0.5:3128:bob:p@ss") == {
        "server": "http://10.0.0.5:3128", "username": "bob", "password": "p@ss",
    }
    assert to_playwright_proxy("10.0.0.5:3128:bob:pa:ss")["password"] == "pa:ss"
    with pytest.raises(ProxyConfigError):
        normalize_proxy_url("http://host:0")


# =============================================================================
# Crawl: achados da revisao (mapa parcial, max_pages, folhas, paginacao, origem)
# =============================================================================

def test_restore_de_mapa_parcial_sem_fila_recomeca_da_pagina_inicial(tmp_path, logger):
    """Mapa parcial + fila vazia (estado deixado por um --update interrompido em
    versao anterior): paginas mapeadas NAO sao dadas por resolvidas — senao o
    proximo resume fecharia o mapa incompleto como completo."""
    ck = Checkpoint(tmp_path, "https://x.com/S")
    ck.manifest.mapped_urls = ["https://x.com/S", "https://x.com/A"]
    ck.manifest.crawl_queue = []
    ck.manifest.crawl_seen = []
    state = CrawlState(queue=deque(["https://x.com/S"]), queued={"https://x.com/S"})

    _restore_state_from_checkpoint(state, ck, "https://x.com/S", logger)
    assert state.seen == set()
    assert list(state.queue) == ["https://x.com/S"]
    assert state.ordered_urls == ["https://x.com/S", "https://x.com/A"]  # ordem preservada


def test_reset_de_recrawl_forcado_grava_estado_coerente(tmp_path, logger):
    from src.crawler import _reset_for_force_recrawl

    ck = Checkpoint(tmp_path, "https://x.com/S")
    ck.save_crawl_progress(
        ["https://x.com/S", "https://x.com/A"], queue=["https://x.com/B"], seen=["https://x.com/S"],
    )
    _reset_for_force_recrawl(ck, logger, "https://x.com/S")
    reloaded = Checkpoint(tmp_path, "https://x.com/S").manifest
    assert reloaded.crawl_complete is False
    assert reloaded.mapped_urls == [] and reloaded.crawl_queue == ["https://x.com/S"]


def test_folhas_conhecidas_sobrevivem_a_interrupcao(tmp_path, logger):
    """Sem persistir as folhas, retomar custaria um request por folha da fila."""
    ck = Checkpoint(tmp_path, "https://x.com/S")
    state = CrawlState(queue=deque(["https://x.com/L1", "https://x.com/N"]),
                       queued={"https://x.com/L1", "https://x.com/N"})
    state.ordered_urls = ["https://x.com/S"]
    state.seen = {"https://x.com/S"}
    state.known_leaves = {"https://x.com/L1", "https://x.com/ja-resolvida"}
    _finalize_crawl(state, ck, None, logger)

    reloaded = Checkpoint(tmp_path, "https://x.com/S")
    assert reloaded.manifest.crawl_leaves == ["https://x.com/L1"]
    restored = CrawlState(queue=deque(), queued=set())
    _restore_state_from_checkpoint(restored, reloaded, "https://x.com/S", logger)
    assert restored.known_leaves == {"https://x.com/L1"}


def test_no_que_atinge_max_pages_volta_para_a_fila(tmp_path, logger, monkeypatch):
    """A pagina que bate no limite entra no mapa mas NAO e dada por resolvida:
    um run posterior sem limite ainda lista os filhos dela."""
    from src import crawler

    async def resolved(*_args, **_kwargs):
        return "resolved"

    monkeypatch.setattr(crawler, "_resolve_node", resolved)
    ck = Checkpoint(tmp_path, "https://x.com/S")
    state = CrawlState(
        queue=deque(["https://x.com/S", "https://x.com/A", "https://x.com/B"]),
        queued={"https://x.com/S", "https://x.com/A", "https://x.com/B"},
    )
    cfg = MagicMock(max_pages=2)
    console = MagicMock()
    console.get_time = time.monotonic
    asyncio.run(crawler._run_crawl_loop(
        MagicMock(), state, MagicMock(), None, cfg, logger,
        __import__("rich.console").console.Console(quiet=True), ck, MagicMock(),
    ))
    assert state.ordered_urls == ["https://x.com/S", "https://x.com/A"]
    assert state.stop_reason == "max_pages"
    assert list(state.queue) == ["https://x.com/A", "https://x.com/B"]
    assert "https://x.com/A" not in state.seen


def test_paginacao_segue_a_resposta_e_nao_o_tamanho_pedido(monkeypatch, logger):
    """Servidor que limita a pagina a 100 (com expand): os 250 filhos chegam."""
    from src import crawler
    from urllib.parse import parse_qs, urlparse

    total = 250
    calls = []

    async def fake_get(page, url):
        query = parse_qs(urlparse(url).query)
        start = int(query["start"][0])
        calls.append(start)
        chunk = range(start, min(start + 100, total))  # ignora limit=200
        data = {
            "results": [{"id": str(i), "_links": {"webui": f"/pages/viewpage.action?pageId={i}"}}
                        for i in chunk],
            "start": start, "limit": 100, "size": len(chunk),
        }
        if start + 100 < total:
            data["_links"] = {"next": "/rest/api/content/1/child/page?start=..."}
        return 200, {}, json.dumps(data), ""

    monkeypatch.setattr(crawler, "_http_get", fake_get)
    scope = parse_scope("https://tdn.totvs.com/display/public/PROT/Raiz")
    state = CrawlState(queue=deque(), queued=set())
    result = asyncio.run(crawler._api_list_children(
        MagicMock(), "1", "https://tdn.totvs.com/display/public/PROT/Raiz", scope, logger, state,
    ))
    assert result.status == "ok" and len(result.children) == total
    assert calls == [0, 100, 200]


def test_same_origin_ignora_esquema_e_caixa_do_host():
    """http -> https por redirect nao pode forcar uma navegacao por pagina."""
    from src.crawler import _same_origin, _site_base

    page = MagicMock()
    page.url = "https://TDN.totvs.com/display/public/PROT/Raiz"
    assert _same_origin(page, "http://tdn.totvs.com/display/public/PROT/Outra")
    assert _site_base(page, "http://tdn.totvs.com/x") == "https://TDN.totvs.com"
    assert not _same_origin(page, "https://outro.site/x")
    page.url = "about:blank"
    assert not _same_origin(page, "https://tdn.totvs.com/x")
    assert _site_base(page, "https://tdn.totvs.com/x") == "https://tdn.totvs.com"
    assert not _same_origin(MagicMock(), "https://tdn.totvs.com/x")  # url nao e str


def _nav_session(status: int):
    """Sessao falsa cuja navegacao responde `status` com uma pagina do Confluence."""
    from unittest.mock import AsyncMock

    from src.browser import BrowserSession

    response = MagicMock()
    response.status = status
    response.headers = {}
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.title = AsyncMock(return_value="Page Not Found - TDN")
    page.evaluate = AsyncMock(return_value=True)
    page.content = AsyncMock(return_value="<html>not found</html>")
    page.url = "https://x.com/display/PROT/S"
    session = BrowserSession()
    session.page = page
    session.context = MagicMock()
    return session


def test_erro_http_na_pagina_inicial_nunca_vira_mapa_de_uma_pagina(logger):
    """404 na pagina inicial: o no fica SEM resolver (crawl incompleto) em vez
    de 'resolvido sem filhos' — que fecharia o mapa com uma URL e, num
    --update, trocaria o mapa completo por ele."""
    from src import crawler

    state = CrawlState(queue=deque(), queued=set())
    state.start_url = "https://x.com/display/PROT/S"
    cfg = MagicMock(dom_budget_ms=1000, timeout_ms=2000, slow_threshold_seconds=60)
    result = asyncio.run(crawler._navigate_and_list(
        _nav_session(404), state.start_url, state, MagicMock(), cfg, logger, MagicMock(),
    ))
    assert result.status == "failed"

    # Pagina interna removida (404): sem filhos a listar, o crawl segue.
    inner = asyncio.run(crawler._navigate_and_list(
        _nav_session(404), "https://x.com/display/PROT/Removida", state, MagicMock(), cfg,
        logger, MagicMock(),
    ))
    assert inner.status == "ok" and inner.children == []
    # 403/500 em pagina interna tambem nao sao "sem filhos".
    denied = asyncio.run(crawler._navigate_and_list(
        _nav_session(403), "https://x.com/display/PROT/Restrita", state, MagicMock(), cfg,
        logger, MagicMock(),
    ))
    assert denied.status == "failed"


def test_remapeamento_que_so_acha_a_pagina_inicial_nao_substitui_o_mapa(tmp_path, logger):
    ck = Checkpoint(tmp_path, "https://x.com/S")
    previous = ["https://x.com/S", "https://x.com/A", "https://x.com/B"]
    ck.record_crawl_complete(previous)
    state = CrawlState(queue=deque(), queued=set())
    state.ordered_urls = ["https://x.com/S"]
    state.seen = {"https://x.com/S"}

    urls, complete, note = _finalize_crawl(state, ck, None, logger, staged_previous=previous)
    assert urls == previous and not complete and note
    assert ck.manifest.mapped_urls == previous


def test_desistencia_em_remapeamento_nao_deixa_registro_no_mapa_antigo(tmp_path, logger):
    from src.crawler import _INTRA_RUN_RETRY_LIMIT, _maybe_requeue_timed_out

    ck = Checkpoint(tmp_path, "https://x.com/S")
    state = CrawlState(queue=deque(), queued=set(), seen={"https://x.com/A"})
    state.staged = True
    state.attempts["https://x.com/A"] = _INTRA_RUN_RETRY_LIMIT - 1
    asyncio.run(_maybe_requeue_timed_out("https://x.com/A", state, logger, ck))
    assert ck.manifest.failures == {}


def test_diff_do_update_ignora_mudanca_de_forma_da_url(tmp_path, monkeypatch):
    """Mapa antigo com /display/PROT/X re-mapeado como /display/public/PROT/X:
    a pagina nao e nova nem removida."""
    from src.crawler import CrawlResult
    import src.main as main_module

    job, start, urls = _pipeline_job(tmp_path)
    manifest_path = job / MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_form = "https://tdn.totvs.com/display/PROT/Filha"
    new_form = "https://tdn.totvs.com/display/public/PROT/Filha"
    name = "0004-filha.pdf"
    _blank_pdf(job / "pages" / name)
    data["mapped_urls"].append(old_form)
    data["exported"][old_form] = {"filename": name, "title": "Filha"}
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    captured: dict = {}
    monkeypatch.setattr(main_module, "_print_summary_tables", lambda **k: captured.update(k))
    new_urls = urls + [new_form, "https://tdn.totvs.com/pages/viewpage.action?pageId=99"]
    _blank_pdf(job / "pages" / "0005-nova.pdf")
    entries = [(job / "pages" / f"{i:04d}-pagina.pdf", f"Pagina {i}") for i in range(1, 4)]

    async def fake_export(**kwargs):
        kwargs["checkpoint"].record_export(new_urls[-1], "0005-nova.pdf", "Nova", 4000, 1.0)
        return entries, [], []

    async def fake_crawl(**_kwargs):
        return CrawlResult(new_urls, [], True, "")

    monkeypatch.setattr(main_module, "crawl_confluence_tree", fake_crawl)
    monkeypatch.setattr(main_module, "export_pages_to_pdf", fake_export)
    from src.utils import get_console

    try:
        code = asyncio.run(main_module.run_pipeline(
            start_url=start, output_dir=job, consolidated_name="consolidado.pdf", headless=True,
            timeout_seconds=30, max_pages=None, slow_threshold_seconds=60.0, auto_confirm=True,
            force_recrawl=True, force_reexport=False, rate_limit=RateLimitConfig(),
            max_workers=1, quiet=True,
        ))
    finally:
        get_console().quiet = False
    assert code == 0
    assert captured["added_urls"] == [new_urls[-1]]
    assert captured["removed_urls"] == []
    # A pagina na forma antiga foi reaproveitada no consolidado (5 entradas).
    assert len(_outline_titles(job / "consolidado.pdf")) == 5


def test_retry_failed_em_mapa_antigo_com_desistencias_nao_sai_como_completo(tmp_path, monkeypatch):
    """Mapa fechado por versao antiga com paginas sem os filhos listados: o
    modo que pula o mapeamento tem de avisar (exit 3) e apontar a opcao [4]."""
    import src.main as main_module

    job, start, urls = _pipeline_job(tmp_path)
    manifest_path = job / MANIFEST_FILENAME
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["failures"] = {urls[1]: {"error": "crawl timeout apos 2 tentativas intra-run", "attempts": 1}}
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    panels: list = []
    monkeypatch.setattr(main_module, "_print_incomplete_panel", lambda *a, **k: panels.append(k))
    entries = [(job / "pages" / f"{i:04d}-pagina.pdf", f"Pagina {i}") for i in range(1, 4)]
    code = _run_stubbed_pipeline(
        monkeypatch, job, start,
        crawl_result=None, export_result=(entries, [], []),
        retry_failed_only=True,
    )
    assert code == 3
    assert panels[0]["crawl_complete"] is False
    assert panels[0]["hint"] == ("[4] Verificar atualizacoes", "update")
