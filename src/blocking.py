"""Classificacao de bloqueios do servidor e de erros de rede.

Separa tres situacoes que pedem reacoes diferentes:

  * RATE_LIMIT   - HTTP 429 / Cloudflare erro 1015. O IP foi banido por um
                   tempo determinado (header Retry-After). Reacao: esperar
                   exatamente esse tempo (ou trocar de proxy) e repetir a MESMA
                   URL. Insistir durante o ban so piora.
  * CHALLENGE /  - desafio anti-bot ou bloqueio de WAF (403). Esperar pouco
    ACCESS_DENIED  adianta; a URL e re-tentada com cooldown e, se persistir,
                   o circuit breaker encerra o run.
  * SERVER_ERROR - 5xx / 52x: indisponibilidade momentanea da origem.

E, do lado do cliente, erros de rede transitorios (queda de conexao, DNS,
firewall local, proxy fora do ar), que nao tem relacao com rate limit.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

RATE_LIMIT = "rate-limit"
CHALLENGE = "challenge"
ACCESS_DENIED = "access-denied"
SERVER_ERROR = "server-error"

# Retry-After absurdo (>24h) e tratado como ausente.
_MAX_RETRY_AFTER_SECONDS = 24 * 3600.0

_RATE_LIMIT_MARKERS = (
    "error 1015",
    "error code: 1015",
    "you are being rate limited",
)
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "verifying you are human",
    "verify you are human",
    "cf-browser-verification",
    "cf-chl-",
)
_ACCESS_DENIED_MARKERS = (
    "sorry, you have been blocked",
    "used cloudflare to restrict access",
    "cf-error-details",
    "error code: 10",
    "access denied",
)
_CLOUDFLARE_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "cloudflare",
)

_PROXY_ERROR_MARKERS = (
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_socks_connection_failed",
    "err_socks_connection_host_unreachable",
    "err_proxy_auth_unsupported",
    "err_proxy_auth_requested",
    "err_proxy_certificate_invalid",
    "err_mandatory_proxy_configuration_failed",
    "err_no_supported_proxies",
    "proxy authentication required",
    "proxy connection",
)

_TRANSIENT_NETWORK_MARKERS = (
    "err_network_access_denied",
    "err_network_changed",
    "err_network_io_suspended",
    "err_internet_disconnected",
    "err_name_not_resolved",
    "err_connection_refused",
    "err_connection_reset",
    "err_connection_closed",
    "err_connection_aborted",
    "err_connection_timed_out",
    "err_connection_failed",
    "err_timed_out",
    "err_address_unreachable",
    "err_empty_response",
    "err_http2_protocol_error",
    "err_quic_protocol_error",
    "err_ssl_protocol_error",
    "err_aborted",
    "econnreset",
    "econnrefused",
    "econnaborted",
    "etimedout",
    "enotfound",
    "ehostunreach",
    "enetunreach",
    "eai_again",
    "socket hang up",
)

_URL_RE = re.compile(r"https?://\S+")


@dataclass(frozen=True)
class BlockInfo:
    """Resultado da classificacao de uma resposta bloqueada."""
    kind: str
    status: int | None = None
    retry_after: float | None = None
    detail: str = ""

    def describe(self) -> str:
        parts = [self.detail or self.kind]
        if self.status:
            parts.append(f"HTTP {self.status}")
        if self.retry_after:
            parts.append(f"Retry-After {self.retry_after:.0f}s")
        return " | ".join(parts)


def parse_retry_after(value, now: float | None = None) -> float | None:
    """Converte o header Retry-After (segundos ou data HTTP) em segundos.

    Retorna None se ausente, invalido, negativo ou absurdo (> 24h).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - (now if now is not None else time.time())
    if seconds < 0 or seconds > _MAX_RETRY_AFTER_SECONDS:
        return None
    return seconds


def _header(headers, name: str):
    if not headers:
        return None
    try:
        for key, value in headers.items():
            if str(key).lower() == name:
                return value
    except AttributeError:
        return None
    return None


def classify_response(
    status: int | None,
    headers=None,
    title: str = "",
    body: str = "",
    is_confluence_page: bool = False,
) -> BlockInfo | None:
    """Classifica uma resposta HTTP. Retorna None se nao for bloqueio.

    A decisao parte do STATUS e dos headers. O corpo so e inspecionado quando
    o status nao e 2xx ou quando a pagina nao tem cara de Confluence: uma
    pagina de documentacao legitima pode conter textos como "Ray ID:" e o
    Cloudflare injeta scripts "challenge-platform" em paginas normais, entao
    marcador solto no HTML de uma pagina 200 nao prova bloqueio.
    """
    retry_after = parse_retry_after(_header(headers, "retry-after"))
    title_lower = (title or "").lower()
    body_lower = (body or "").lower()
    text = f"{title_lower}\n{body_lower}"

    if status == 429:
        return BlockInfo(RATE_LIMIT, status, retry_after, "rate limit (Cloudflare 1015)")

    mitigated = str(_header(headers, "cf-mitigated") or "").lower()
    if mitigated == "challenge":
        return BlockInfo(CHALLENGE, status, retry_after, "desafio anti-bot do Cloudflare")

    ok_status = status is not None and 200 <= status < 300
    if ok_status and is_confluence_page:
        return None

    cloudflare_title = any(marker in title_lower for marker in _CLOUDFLARE_TITLE_MARKERS)
    if ok_status and not cloudflare_title:
        return None

    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return BlockInfo(RATE_LIMIT, status, retry_after, "rate limit (Cloudflare 1015)")
    if any(marker in text for marker in _CHALLENGE_MARKERS):
        return BlockInfo(CHALLENGE, status, retry_after, "desafio anti-bot do Cloudflare")
    if status is not None and status >= 500:
        return BlockInfo(SERVER_ERROR, status, retry_after, "erro do servidor")
    # 403 da propria aplicacao (pagina restrita do Confluence) NAO e bloqueio de
    # rede: so conta como ACCESS_DENIED se a pagina de erro for do Cloudflare.
    if "cloudflare" in text and any(marker in text for marker in _ACCESS_DENIED_MARKERS):
        return BlockInfo(ACCESS_DENIED, status, retry_after, "acesso negado pelo Cloudflare")
    return None


def error_headline(exc: BaseException) -> str:
    """Primeira parte da mensagem de erro, em minusculas e SEM URLs.

    O Playwright anexa um "Call log" com eco dos headers (inclusive cookies) e
    inclui a URL na mensagem; numeros de pageId, timestamps ou titulos de
    pagina (ex: "Registro C503") nao podem ser confundidos com codigos HTTP.
    """
    headline = str(exc).split("\nCall log:", 1)[0]
    return _URL_RE.sub("<url>", headline).lower()


def short_error(exc: BaseException, limit: int = 300) -> str:
    """Mensagem enxuta para gravar em failures (sem o Call log do Playwright)."""
    text = str(exc).split("\nCall log:", 1)[0].strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def is_proxy_error(exc: BaseException) -> bool:
    """Erro de conexao com o PROXY (proxy fora do ar, recusando ou sem auth)."""
    headline = error_headline(exc)
    return any(marker in headline for marker in _PROXY_ERROR_MARKERS)


def is_transient_network_error(exc: BaseException) -> bool:
    """Erro de rede do lado do cliente que tende a sumir sozinho em segundos.

    Inclui net::ERR_NETWORK_ACCESS_DENIED (firewall/VPN local negando acesso
    por um instante), queda de conexao, DNS e erros de proxy.
    """
    headline = error_headline(exc)
    return is_proxy_error(exc) or any(
        marker in headline for marker in _TRANSIENT_NETWORK_MARKERS
    )
