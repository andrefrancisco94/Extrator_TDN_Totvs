"""Triagem de proxies e tratamento de proxy lento/morto durante o run.

Tudo em loopback: um servidor de destino e proxies HTTP/SOCKS5/SOCKS4 minimos
sobem dentro do proprio teste.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from types import SimpleNamespace

import pytest

from src import browser as browser_module
from src.browser import note_exit_failure, note_page_timeout
from src.proxy import AllProxiesDeadError, ProxyPool
from src.proxy_check import estimate_seconds, probe_many, probe_proxy, rank_working
from src.utils import RateLimitConfig, RateLimiter

LOGGER = logging.getLogger("test_proxy_check")


# ----------------------------------------------------------------------------
# Servidores de apoio
# ----------------------------------------------------------------------------

async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


async def _relay(reader, writer, host, port):
    upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


async def _start(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _origin(status=200, delay=0.0):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(delay)
        body = b"fl=1\nh=tdn\nip=203.0.113.7\nts=1\n"
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode() + body
        )
        await writer.drain()
        writer.close()
    return await _start(handle)


async def _http_proxy(refuse=False, delay=0.0):
    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        if refuse:
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        await asyncio.sleep(delay)
        host, port = head.split()[1].decode().rsplit(":", 1)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await _relay(reader, writer, host, int(port))
    return await _start(handle)


async def _socks5_proxy():
    async def handle(reader, writer):
        _version, methods = await reader.readexactly(2)
        await reader.readexactly(methods)
        writer.write(b"\x05\x00")
        head = await reader.readexactly(4)
        assert head[3] == 3, "o cliente deve mandar o NOME (resolucao no proxy)"
        size = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(size)).decode()
        port = int.from_bytes(await reader.readexactly(2), "big")
        writer.write(b"\x05\x00\x00\x01" + bytes(6))
        await writer.drain()
        await _relay(reader, writer, host, port)
    return await _start(handle)


async def _socks4_proxy():
    async def handle(reader, writer):
        head = await reader.readexactly(8)
        await reader.readuntil(b"\x00")
        port = int.from_bytes(head[2:4], "big")
        writer.write(b"\x00\x5a" + bytes(6))
        await writer.drain()
        await _relay(reader, writer, socket.inet_ntoa(head[4:8]), port)
    return await _start(handle)


async def _blackhole():
    async def handle(reader, writer):
        await asyncio.sleep(30)
    return await _start(handle)


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _trace(port: int) -> str:
    return f"http://127.0.0.1:{port}/cdn-cgi/trace"


# ----------------------------------------------------------------------------
# probe_proxy / probe_many
# ----------------------------------------------------------------------------

def test_probe_aprova_proxy_http_socks5_e_socks4():
    async def scenario():
        origin, origin_port = await _origin()
        servers = [origin]
        proxies = []
        for scheme, factory in (("http", _http_proxy), ("socks5", _socks5_proxy),
                                ("socks4", _socks4_proxy)):
            server, port = await factory()
            servers.append(server)
            proxies.append(f"{scheme}://127.0.0.1:{port}")
        try:
            return await probe_many(proxies, _trace(origin_port), timeout=5)
        finally:
            for server in servers:
                server.close()

    results = asyncio.run(scenario())
    assert [r.ok for r in results] == [True, True, True], [r.detail for r in results]
    assert all(r.status == 200 and r.ip == "203.0.113.7" for r in results)


def test_probe_reprova_morto_mudo_e_proxy_que_recusa():
    async def scenario():
        origin, origin_port = await _origin()
        mute, mute_port = await _blackhole()
        refusing, refusing_port = await _http_proxy(refuse=True)
        proxies = [
            f"http://127.0.0.1:{_closed_port()}",
            f"socks5://127.0.0.1:{mute_port}",
            f"http://127.0.0.1:{refusing_port}",
        ]
        try:
            return await probe_many(proxies, _trace(origin_port), timeout=0.6)
        finally:
            for server in (origin, mute, refusing):
                server.close()

    dead, mute, refusing = asyncio.run(scenario())
    # Porta fechada: recusa imediata no Linux; no Windows a recusa leva ~2s e
    # o teste a ve como timeout. Em ambos o proxy e reprovado.
    assert not dead.ok and dead.detail
    assert not mute.ok and "timeout" in mute.detail
    assert not refusing.ok and "403" in refusing.detail


def test_probe_reprova_quando_o_destino_responde_erro():
    """Proxy vivo, mas o destino barra aquele IP (403/429): nao serve."""
    async def scenario():
        origin, origin_port = await _origin(status=429)
        proxy, proxy_port = await _http_proxy()
        try:
            return await probe_proxy(f"http://127.0.0.1:{proxy_port}", _trace(origin_port), 5)
        finally:
            origin.close()
            proxy.close()

    result = asyncio.run(scenario())
    assert not result.ok and result.status == 429 and result.detail == "HTTP 429"


def test_probe_many_mantem_a_ordem_e_ranqueia_pelo_mais_rapido():
    async def scenario():
        origin, origin_port = await _origin()
        slow, slow_port = await _http_proxy(delay=0.5)
        fast, fast_port = await _http_proxy()
        proxies = [
            f"http://127.0.0.1:{slow_port}",
            f"http://127.0.0.1:{_closed_port()}",
            f"http://127.0.0.1:{fast_port}",
        ]
        seen = []
        try:
            results = await probe_many(
                proxies, _trace(origin_port), timeout=5,
                on_progress=lambda done, total: seen.append((done, total)),
            )
        finally:
            for server in (origin, slow, fast):
                server.close()
        return proxies, results, seen

    proxies, results, seen = asyncio.run(scenario())
    assert [r.proxy for r in results] == proxies
    assert [r.proxy for r in rank_working(results)] == [proxies[2], proxies[0]]
    assert sorted(seen) == [(1, 3), (2, 3), (3, 3)]


def test_proxy_https_fica_na_lista_sem_medicao_e_por_ultimo():
    result = asyncio.run(probe_proxy("https://proxy.exemplo:8443", "https://tdn.totvs.com/", 5))
    assert result.ok and not result.tested
    ranked = rank_working([result, type(result)("http://a:1", True, 7.9)])
    assert [r.proxy for r in ranked] == ["http://a:1", "https://proxy.exemplo:8443"]


def test_estimativa_do_pior_caso():
    assert estimate_seconds(2000, timeout=8, concurrency=100) == 160
    assert estimate_seconds(1, timeout=8, concurrency=100) == 8


# ----------------------------------------------------------------------------
# Triagem no inicio do run
# ----------------------------------------------------------------------------

def _quiet_console():
    return SimpleNamespace(print=lambda *args, **kwargs: None)


def test_triagem_do_run_usa_so_os_proxies_que_respondem(monkeypatch):
    from src import main as main_module

    monkeypatch.setattr(main_module, "_TRIAGE_MIN_PROXIES", 2)

    async def scenario():
        origin, origin_port = await _origin()
        good, good_port = await _http_proxy()
        dead = f"http://127.0.0.1:{_closed_port()}"
        try:
            pool = await main_module._build_checked_pool(
                [dead, f"http://127.0.0.1:{good_port}", None], None,
                f"http://127.0.0.1:{origin_port}/display/X/Pagina", LOGGER, _quiet_console(),
            )
            with pytest.raises(AllProxiesDeadError):
                await main_module._build_checked_pool(
                    [dead, f"http://127.0.0.1:{_closed_port()}"], None,
                    f"http://127.0.0.1:{origin_port}/display/X/Pagina", LOGGER,
                    _quiet_console(),
                )
            return pool, good_port
        finally:
            origin.close()
            good.close()

    pool, good_port = asyncio.run(scenario())
    # O morto saiu; 'direct' estava no fim da lista e continua como ultima opcao.
    assert pool.labels() == [f"http://127.0.0.1:{good_port}", "conexao direta"]


def test_lista_pequena_nao_passa_pela_triagem():
    from src import main as main_module

    pool = asyncio.run(main_module._build_checked_pool(
        ["http://127.0.0.1:9", "http://127.0.0.1:10"], None,
        "https://tdn.totvs.com/display/X/Y", LOGGER, _quiet_console(),
    ))
    assert len(pool) == 2


# ----------------------------------------------------------------------------
# Quarentena do proxy que falha
# ----------------------------------------------------------------------------

def _pool(exits):
    clock = [0.0]
    return ProxyPool(exits, clock=lambda: clock[0]), clock


def test_proxy_que_falha_vai_para_quarentena_e_volta():
    pool, clock = _pool(["http://ruim:1", "http://bom:2"])
    assert [pool.mark_failed() for _ in range(3)] == [False, False, True]
    assert pool.pick_best() == (True, 0.0) and pool.current == "http://bom:2"
    assert pool.can_rotate  # quarentena nao e descarte
    pool.mark_blocked(3600)  # o bom levou ban de 1h
    switched, wait = pool.pick_best()
    assert switched and pool.current == "http://ruim:1" and wait == 600.0
    clock[0] = 601.0
    assert pool.remaining_block() == 0.0


def test_proxy_e_descartado_na_terceira_quarentena_sem_sucesso():
    pool, _ = _pool(["http://ruim:1"])
    for _ in range(8):
        pool.mark_failed()
    assert pool.pick_best() == (False, 1200.0)  # 2a quarentena: 20 min
    assert pool.mark_failed()
    with pytest.raises(AllProxiesDeadError):
        pool.pick_best()


def test_sucesso_zera_o_historico_de_quarentenas():
    pool, _ = _pool(["http://a:1"])
    for _ in range(6):
        pool.mark_failed()
    pool.mark_ok()
    for _ in range(6):
        pool.mark_failed()
    assert pool.pick_best()[0] is False  # ainda vivo (so 2 quarentenas seguidas)


# ----------------------------------------------------------------------------
# note_exit_failure / note_page_timeout
# ----------------------------------------------------------------------------

def _session(pool, monkeypatch):
    session = SimpleNamespace(pool=pool, rotation_lock=asyncio.Lock(), generation=0)

    async def fake_relaunch(sess, logger):
        sess.generation += 1

    monkeypatch.setattr(browser_module, "relaunch_session", fake_relaunch)
    return session


def _limiter(delay=0.01):
    return RateLimiter(RateLimitConfig(base_delay_seconds=delay, backoff_initial_seconds=30.0))


def test_timeout_por_proxy_troca_de_saida_sem_desacelerar(monkeypatch):
    async def scenario():
        pool = ProxyPool(["http://lento:1", "http://bom:2"])
        session = _session(pool, monkeypatch)
        limiter = _limiter(delay=0.5)
        for _ in range(3):
            assert await note_page_timeout(session, limiter, LOGGER, "timeout no PDF", generation=0)
        return pool, session, limiter

    pool, session, limiter = asyncio.run(scenario())
    assert pool.current == "http://bom:2" and session.generation == 1
    assert limiter.total_blocks == 0 and limiter.current_delay == 0.5
    assert limiter.cooldown_remaining() == 0.0


def test_timeout_em_conexao_direta_conta_como_bloqueio(monkeypatch):
    async def scenario():
        session = _session(ProxyPool([None]), monkeypatch)
        limiter = _limiter(delay=0.5)
        via_proxy = await note_page_timeout(session, limiter, LOGGER, "timeout no PDF")
        return via_proxy, limiter

    via_proxy, limiter = asyncio.run(scenario())
    assert via_proxy is False
    assert limiter.total_blocks == 1 and limiter.current_delay == 1.0


def test_falha_vista_no_proxy_antigo_nao_conta_contra_o_novo(monkeypatch):
    """8 workers falham juntos no mesmo proxy morto: as 3 primeiras falhas o
    tiram do rodizio; as outras 5 (mesma geracao antiga) nao podem derrubar o
    proxy novo."""
    async def scenario():
        pool = ProxyPool(["http://morto:1", "http://bom:2", "http://reserva:3"])
        session = _session(pool, monkeypatch)
        limiter = _limiter()
        for _ in range(8):
            await note_exit_failure(session, LOGGER, limiter=limiter, generation=0)
        return pool, session

    pool, session = asyncio.run(scenario())
    assert pool.current == "http://bom:2" and session.generation == 1


def test_unico_proxy_falhando_espera_a_quarentena_em_vez_de_martelar(monkeypatch):
    async def scenario():
        session = _session(ProxyPool(["http://unico:1"]), monkeypatch)
        limiter = _limiter()
        for _ in range(3):
            switched = await note_exit_failure(session, LOGGER, limiter=limiter)
        return switched, limiter.cooldown_remaining()

    switched, cooldown = asyncio.run(scenario())
    assert switched is False and 590.0 < cooldown <= 600.0


# ----------------------------------------------------------------------------
# RateLimiter.set_cooldown acorda quem dorme o cooldown antigo
# ----------------------------------------------------------------------------

def test_troca_de_proxy_libera_worker_que_dormia_o_ban_do_ip_antigo():
    async def scenario():
        limiter = _limiter()
        await limiter.report_block(LOGGER, "429", retry_after=3600)
        started = time.monotonic()
        waiters = [asyncio.create_task(limiter.wait()) for _ in range(3)]
        await asyncio.sleep(0.05)  # todos reservaram slot depois do ban de 1h
        assert not any(task.done() for task in waiters)
        await limiter.set_cooldown(0.0)  # trocou para um IP livre
        await asyncio.wait_for(asyncio.gather(*waiters), timeout=2.0)
        await asyncio.wait_for(limiter.wait(), timeout=2.0)  # slots futuros tambem
        return time.monotonic() - started, limiter.total_wait_seconds

    elapsed, waited = asyncio.run(scenario())
    assert elapsed < 2.0
    assert waited < 2.0  # contabiliza o que dormiu de fato, nao a hora inteira
