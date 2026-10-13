"""Triagem rapida de proxies: testa centenas em paralelo, sem abrir browser.

Listas publicas de proxy costumam ter a grande maioria dos enderecos morta ou
lenta demais. Descobrir isso pelo browser custa um relancamento do Chromium e
um timeout por proxy; aqui cada um e testado com UM request leve (handshake do
proxy + TLS + GET), dezenas ao mesmo tempo.

O certificado do servidor e validado de ponta a ponta: proxy que intercepta o
TLS e reprovado.
"""
from __future__ import annotations

import asyncio
import base64
import re
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

from .proxy import mask_proxy

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_CONCURRENCY = 100
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
_STATUS_RE = re.compile(rb"^HTTP/\d(?:\.\d)? (\d{3})")
_IP_RE = re.compile(rb"^ip=(\S+)", re.MULTILINE)


@dataclass
class ProbeResult:
    """Resultado do teste de um proxy."""
    proxy: str
    ok: bool
    seconds: float
    status: int | None = None
    ip: str = ""
    detail: str = ""
    tested: bool = True

    @property
    def label(self) -> str:
        return mask_proxy(self.proxy)


class _ProbeError(Exception):
    """O proxy respondeu, mas recusou ou falhou em abrir o tunel."""


async def _tunnel_http(reader, writer, host: str, port: int, username: str, password: str) -> None:
    auth = ""
    if username:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        auth = f"Proxy-Authorization: Basic {token}\r\n"
    writer.write(
        f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode()
    )
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    match = _STATUS_RE.match(head)
    if match is None:
        raise _ProbeError("resposta invalida ao CONNECT")
    if match.group(1) != b"200":
        raise _ProbeError(f"proxy recusou o tunel (HTTP {match.group(1).decode()})")


async def _tunnel_socks5(reader, writer, host: str, port: int) -> None:
    writer.write(b"\x05\x01\x00")  # versao 5, 1 metodo: sem autenticacao
    await writer.drain()
    if await reader.readexactly(2) != b"\x05\x00":
        raise _ProbeError("nao respondeu como SOCKS5 sem autenticacao")
    name = host.encode("idna")
    # Nome resolvido PELO proxy, como o Chromium faz com socks5://.
    writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    await writer.drain()
    head = await reader.readexactly(4)
    if head[0] != 5 or head[1] != 0:
        raise _ProbeError(f"SOCKS5 recusou a conexao (codigo {head[1]})")
    if head[3] == 1:
        await reader.readexactly(4 + 2)
    elif head[3] == 4:
        await reader.readexactly(16 + 2)
    elif head[3] == 3:
        size = (await reader.readexactly(1))[0]
        await reader.readexactly(size + 2)
    else:
        raise _ProbeError("resposta SOCKS5 invalida")


async def _tunnel_socks4(reader, writer, ipv4: str, port: int) -> None:
    # SOCKS4 nao resolve nomes: vai o IPv4 resolvido localmente (como o Chromium).
    writer.write(b"\x04\x01" + port.to_bytes(2, "big") + socket.inet_aton(ipv4) + b"\x00")
    await writer.drain()
    reply = await reader.readexactly(8)
    if reply[1] != 0x5A:
        raise _ProbeError(f"SOCKS4 recusou a conexao (codigo {reply[1]})")


async def _http_get(reader, writer, host_header: str, path: str) -> tuple[int | None, str]:
    writer.write(
        (
            f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUser-Agent: {_USER_AGENT}\r\n"
            "Accept: */*\r\nConnection: close\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    data = b""
    while len(data) < 8192 and b"\r\n\r\n" not in data:
        chunk = await reader.read(4096)
        if not chunk:
            break
        data += chunk
    if b"\r\n\r\n" in data and _IP_RE.search(data) is None:
        # O corpo pode chegar num segundo pacote; nao vale esperar muito por ele.
        try:
            data += await asyncio.wait_for(reader.read(4096), timeout=1.5)
        except asyncio.TimeoutError:
            pass
    status = _STATUS_RE.match(data)
    ip = _IP_RE.search(data)
    return (
        int(status.group(1)) if status else None,
        ip.group(1).decode("ascii", "replace") if ip else "",
    )


async def _probe(proxy_url: str, target, target_ipv4: str | None, ssl_context) -> tuple[int | None, str]:
    proxy = urlsplit(proxy_url)
    scheme = (proxy.scheme or "http").lower()
    host = target.hostname or ""
    secure = target.scheme == "https"
    port = target.port or (443 if secure else 80)
    path = target.path or "/"
    if target.query:
        path += f"?{target.query}"
    reader, writer = await asyncio.open_connection(proxy.hostname, proxy.port)
    try:
        if scheme == "http":
            await _tunnel_http(
                reader, writer, host, port,
                unquote(proxy.username or ""), unquote(proxy.password or ""),
            )
        elif scheme == "socks5":
            await _tunnel_socks5(reader, writer, host, port)
        elif scheme == "socks4":
            if not target_ipv4:
                raise _ProbeError("destino sem IPv4 (SOCKS4 nao resolve nomes)")
            await _tunnel_socks4(reader, writer, target_ipv4, port)
        else:
            raise _ProbeError(f"esquema {scheme} nao testado")
        if secure:
            await writer.start_tls(
                ssl_context or ssl.create_default_context(), server_hostname=host,
            )
        default_port = 443 if secure else 80
        return await _http_get(
            reader, writer, host if port == default_port else f"{host}:{port}", path,
        )
    finally:
        writer.close()


async def probe_proxy(
    proxy_url: str,
    target_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    *,
    target_ipv4: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> ProbeResult:
    """Faz um GET em `target_url` atraves do proxy. Nunca levanta excecao."""
    scheme = (urlsplit(proxy_url).scheme or "http").lower()
    if scheme not in ("http", "socks5", "socks4"):
        # Proxy https:// (TLS ate o proxy): fica na lista, sem medicao.
        return ProbeResult(proxy_url, True, timeout, detail="nao testado", tested=False)
    started = time.monotonic()
    try:
        status, ip = await asyncio.wait_for(
            _probe(proxy_url, urlsplit(target_url), target_ipv4, ssl_context), timeout,
        )
    except asyncio.TimeoutError:
        return ProbeResult(proxy_url, False, timeout, detail="sem resposta (timeout)")
    except _ProbeError as exc:
        return ProbeResult(proxy_url, False, time.monotonic() - started, detail=str(exc))
    except ssl.SSLError as exc:
        return ProbeResult(
            proxy_url, False, time.monotonic() - started,
            detail=f"falha de TLS ({exc.reason or type(exc).__name__})",
        )
    except (OSError, EOFError, asyncio.LimitOverrunError, ValueError) as exc:
        return ProbeResult(
            proxy_url, False, time.monotonic() - started,
            detail=f"sem conexao ({type(exc).__name__})",
        )
    elapsed = time.monotonic() - started
    if status is None:
        return ProbeResult(proxy_url, False, elapsed, detail="resposta invalida do destino")
    if status >= 400:
        return ProbeResult(proxy_url, False, elapsed, status=status, ip=ip, detail=f"HTTP {status}")
    return ProbeResult(proxy_url, True, elapsed, status=status, ip=ip)


async def probe_many(
    proxies: Iterable[str],
    target_url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    on_progress: Callable[[int, int], None] | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> list[ProbeResult]:
    """Testa todos os proxies em paralelo. Resultados na ordem de entrada."""
    items = list(proxies)
    target = urlsplit(target_url)
    target_ipv4: str | None = None
    if any(urlsplit(item).scheme.lower() == "socks4" for item in items):
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                target.hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM,
            )
            target_ipv4 = infos[0][4][0] if infos else None
        except OSError:
            target_ipv4 = None
    gate = asyncio.Semaphore(max(1, concurrency))
    done = 0

    async def one(proxy_url: str) -> ProbeResult:
        nonlocal done
        async with gate:
            result = await probe_proxy(
                proxy_url, target_url, timeout,
                target_ipv4=target_ipv4, ssl_context=ssl_context,
            )
        done += 1
        if on_progress is not None:
            on_progress(done, len(items))
        return result

    return list(await asyncio.gather(*(one(item) for item in items)))


def rank_working(results: Iterable[ProbeResult], *, unique_ips: bool = False) -> list[ProbeResult]:
    """Proxies aprovados, do mais rapido ao mais lento (nao testados por ultimo)."""
    ranked = sorted((r for r in results if r.ok), key=lambda r: (not r.tested, r.seconds))
    if not unique_ips:
        return ranked
    # Enderecos/portas diferentes podem sair pelo MESMO IP. Conserva o mais
    # rapido para que workers independentes nao dividam o limite desse IP.
    seen: set[str] = set()
    distinct = []
    for result in ranked:
        if result.ip and result.ip in seen:
            continue
        if result.ip:
            seen.add(result.ip)
        distinct.append(result)
    return distinct


def estimate_seconds(count: int, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                     concurrency: int = DEFAULT_CONCURRENCY) -> float:
    """Pior caso da triagem (todos os proxies esgotando o timeout)."""
    waves = -(-count // max(1, concurrency))
    return waves * timeout
