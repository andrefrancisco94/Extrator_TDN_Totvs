"""Saidas de rede (proxies) do extrator: validacao, arquivo de lista e rotacao.

O Cloudflare do TDN limita requests POR IP (erro 1015 / HTTP 429). Um proxy
troca o IP de saida; com varios proxies o extrator alterna para o proximo
quando o IP atual e bloqueado, em vez de ficar parado esperando o ban acabar.

Este modulo nao depende de Playwright nem de src.utils (pode ser importado de
qualquer lugar sem ciclo).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote, unquote, urlparse

# Palavra-chave aceita na lista de proxies: conexao direta (sem proxy).
DIRECT_KEYWORDS = frozenset({"direct", "direto", "none", "sem-proxy"})

PROXY_FILE_DEFAULT = "proxies.txt"

_DEFAULT_PORTS = {"http": 80, "https": 443, "socks5": 1080, "socks4": 1080}
_SCHEME_ALIASES = {"socks5h": "socks5", "socks": "socks5", "socks4a": "socks4"}

# Erros de conexao consecutivos ate um proxy ser considerado fora do ar.
_DEAD_AFTER_FAILURES = 3      # falhas seguidas ate a saida ir para a quarentena
_QUARANTINE_SECONDS = 600.0   # 1a quarentena; dobra na reincidencia
_DEAD_AFTER_STRIKES = 3       # quarentenas sem nenhum sucesso ate o descarte

_FORMATS_HINT = (
    "Formatos aceitos: http://host:porta | http://usuario:senha@host:porta | "
    "host:porta | host:porta:usuario:senha | socks5://host:porta | direct"
)


class ProxyConfigError(ValueError):
    """Proxy informado e invalido ou nao suportado."""


class AllProxiesDeadError(RuntimeError):
    """Nenhuma saida de rede utilizavel (todos os proxies falharam)."""


def _split_host_port_user_pass(raw: str) -> str | None:
    """Converte 'host:porta:usuario:senha' (formato comum de provedores) em URL.

    A senha e tudo depois do 3o ':' — pode conter ':' e '@'.
    """
    if "://" in raw or raw.startswith("["):
        return None
    parts = raw.split(":", 3)
    if len(parts) != 4 or not parts[1].isdigit() or "@" in parts[0]:
        return None
    host, port, user, password = parts
    if not host or not user:
        return None
    return f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"


def normalize_proxy_url(raw: str | None) -> str | None:
    """Valida e normaliza um proxy. Retorna None para conexao direta.

    Levanta ProxyConfigError com mensagem acionavel se o valor for invalido —
    um proxy digitado errado NUNCA pode ser ignorado em silencio (o usuario
    acharia que esta protegido enquanto os requests saem do IP real).
    """
    value = (raw or "").strip()
    if not value or value.lower() in DIRECT_KEYWORDS:
        return None

    value = _split_host_port_user_pass(value) or value
    if "://" not in value:
        value = "http://" + value

    try:
        parsed = urlparse(value)
        scheme = _SCHEME_ALIASES.get(parsed.scheme.lower(), parsed.scheme.lower())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigError(f"Proxy invalido ({raw!r}): {exc}. {_FORMATS_HINT}") from exc

    if scheme not in _DEFAULT_PORTS:
        raise ProxyConfigError(
            f"Esquema de proxy nao suportado em {raw!r}: {parsed.scheme!r}. {_FORMATS_HINT}"
        )
    if not hostname:
        raise ProxyConfigError(f"Proxy sem host em {raw!r}. {_FORMATS_HINT}")
    if port == 0:
        raise ProxyConfigError(f"Porta 0 invalida em {raw!r}. {_FORMATS_HINT}")
    if scheme.startswith("socks") and (parsed.username or parsed.password):
        raise ProxyConfigError(
            f"Proxy SOCKS com usuario/senha nao e suportado pelo Chromium ({raw!r}). "
            "Use um proxy HTTP autenticado ou um SOCKS sem autenticacao."
        )

    host = f"[{hostname}]" if ":" in hostname else hostname
    auth = ""
    if parsed.username:
        auth = quote(unquote(parsed.username), safe="")
        if parsed.password:
            auth += ":" + quote(unquote(parsed.password), safe="")
        auth += "@"
    return f"{scheme}://{auth}{host}:{port or _DEFAULT_PORTS[scheme]}"


def to_playwright_proxy(proxy_url: str | None) -> dict | None:
    """Converte URL de proxy (ja normalizada ou nao) no dict do Playwright."""
    normalized = normalize_proxy_url(proxy_url)
    if normalized is None:
        return None
    parsed = urlparse(normalized)
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    result = {"server": f"{parsed.scheme}://{host}:{parsed.port}"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def mask_proxy(proxy_url: str | None) -> str:
    """Representacao segura para log/tela: nunca mostra usuario/senha."""
    if not proxy_url:
        return "conexao direta"
    try:
        parsed = urlparse(proxy_url)
        hostname = parsed.hostname or "?"
        host = f"[{hostname}]" if ":" in hostname else hostname
        creds = "***:***@" if (parsed.username or parsed.password) else ""
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme or 'http'}://{creds}{host}{port}"
    except ValueError:
        return "[proxy invalido]"


def parse_proxy_lines(lines: Iterable[str]) -> list[str | None]:
    """Le uma lista de proxies (um por linha). Ignora vazias e comentarios (#).

    Remove duplicatas preservando a ordem. 'direct' vira None (conexao direta).
    """
    exits: list[str | None] = []
    seen: set[str | None] = set()
    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            normalized = normalize_proxy_url(text)
        except ProxyConfigError as exc:
            raise ProxyConfigError(f"linha {number}: {exc}") from exc
        if normalized in seen:
            continue
        seen.add(normalized)
        exits.append(normalized)
    return exits


def load_proxy_file(path: Path) -> list[str | None]:
    """Carrega arquivo de proxies. Levanta ProxyConfigError se ilegivel/invalido."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ProxyConfigError(f"Nao foi possivel ler {path}: {exc}") from exc
    try:
        return parse_proxy_lines(text.splitlines())
    except ProxyConfigError as exc:
        raise ProxyConfigError(f"{path}: {exc}") from exc


def save_proxy_file(path: Path, exits: Iterable[str | None]) -> None:
    """Grava a lista de proxies (contem credenciais: mantenha fora do git)."""
    lines = [
        "# Proxies do Extrator TDN - um por linha. Linhas com # sao ignoradas.",
        "# Use 'direct' para incluir a conexao direta (sem proxy) no rodizio.",
        "# ATENCAO: pode conter usuario/senha. Nao versionar este arquivo.",
    ]
    lines.extend("direct" if item is None else item for item in exits)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class ExitState:
    """Estado de uma saida de rede (um proxy, ou None = conexao direta)."""
    proxy: str | None
    blocked_until: float = 0.0
    blocks: int = 0
    consecutive_failures: int = 0
    strikes: int = 0
    dead: bool = False


class ProxyPool:
    """Rodizio de saidas de rede com cooldown POR saida.

    Um ban do Cloudflare vale para o IP, nao para o extrator: quando a saida
    atual e bloqueada, a proxima saida livre assume imediatamente. So ha
    espera quando todas as saidas estao bloqueadas ao mesmo tempo.
    """

    def __init__(
        self,
        exits: Iterable[str | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        unique: list[str | None] = []
        for item in (exits or [None]):
            if item not in unique:
                unique.append(item)
        self._exits = [ExitState(proxy=item) for item in (unique or [None])]
        self._index = 0
        self._clock = clock

    def __len__(self) -> int:
        return len(self._exits)

    @property
    def current(self) -> str | None:
        return self._exits[self._index].proxy

    @property
    def current_label(self) -> str:
        return mask_proxy(self.current)

    @property
    def can_rotate(self) -> bool:
        return sum(1 for e in self._exits if not e.dead) > 1

    def labels(self) -> list[str]:
        return [mask_proxy(e.proxy) for e in self._exits]

    def summary(self, limit: int = 5) -> str:
        """Saidas para o log: as primeiras `limit` e quantas mais existem."""
        labels = self.labels()
        extra = len(labels) - limit
        return ", ".join(labels[:limit]) + (f" ... (+{extra})" if extra > 0 else "")

    def mark_ok(self) -> None:
        state = self._exits[self._index]
        state.consecutive_failures = 0
        state.strikes = 0

    def mark_blocked(self, seconds: float) -> None:
        """Registra que a saida atual esta banida pelos proximos `seconds`."""
        state = self._exits[self._index]
        state.blocks += 1
        state.blocked_until = max(state.blocked_until, self._clock() + max(0.0, seconds))

    def mark_failed(self) -> bool:
        """Erro de conexao ou timeout na saida atual. Retorna True se ela saiu
        do rodizio.

        Na 3a falha seguida o proxy vai para a quarentena (10 min, depois 20):
        pode ser so uma instabilidade, entao ele volta a ser tentado. Na 3a
        quarentena sem nenhum sucesso no meio, e descartado de vez. A conexao
        direta nunca sai do rodizio (falha nela e problema de rede local).
        """
        state = self._exits[self._index]
        state.consecutive_failures += 1
        if state.proxy is None or state.consecutive_failures < _DEAD_AFTER_FAILURES:
            return False
        state.consecutive_failures = 0
        state.strikes += 1
        if state.strikes >= _DEAD_AFTER_STRIKES:
            state.dead = True
        else:
            penalty = _QUARANTINE_SECONDS * (2 ** (state.strikes - 1))
            state.blocked_until = max(state.blocked_until, self._clock() + penalty)
        return True

    def remaining_block(self) -> float:
        """Segundos de ban restantes na saida atual (0 se livre)."""
        return max(0.0, self._exits[self._index].blocked_until - self._clock())

    def pick_best(self) -> tuple[bool, float]:
        """Seleciona a saida que libera primeiro. Retorna (trocou, espera_seg).

        Empate (varias livres): a proxima na ordem circular apos a atual, para
        distribuir carga entre os proxies. Levanta AllProxiesDeadError se nao
        sobrou nenhuma saida utilizavel.
        """
        now = self._clock()
        count = len(self._exits)
        best_index: int | None = None
        best_wait = 0.0
        for offset in range(1, count + 1):
            index = (self._index + offset) % count
            state = self._exits[index]
            if state.dead:
                continue
            wait = max(0.0, state.blocked_until - now)
            if best_index is None or wait < best_wait:
                best_index, best_wait = index, wait
        if best_index is None:
            raise AllProxiesDeadError(
                "Todos os proxies configurados falharam ao conectar. "
                "Verifique a lista de proxies (opcao [P] do menu) ou remova-a."
            )
        switched = best_index != self._index
        self._index = best_index
        return switched, best_wait


def resolve_exits(
    proxy_args: Iterable[str] | None,
    proxy_file: Path | None,
    env: dict[str, str] | None = None,
) -> tuple[list[str | None], str]:
    """Decide as saidas de rede efetivas. Retorna (saidas, origem).

    Precedencia: --proxy (um ou varios) > --proxy-file > variaveis de ambiente
    HTTP(S)_PROXY > conexao direta. A origem e devolvida para aparecer no log:
    o usuario precisa saber por onde os requests estao saindo.
    """
    args = [a for a in (proxy_args or []) if a and a.strip()]
    if args:
        expanded: list[str] = []
        for arg in args:
            expanded.extend(part for part in arg.split(",") if part.strip())
        return parse_proxy_lines(expanded) or [None], "--proxy"
    if proxy_file is not None:
        exits = load_proxy_file(proxy_file)
        if exits:
            return exits, f"arquivo {proxy_file}"
    environment = env if env is not None else {}
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (environment.get(name) or "").strip()
        if value:
            try:
                return [normalize_proxy_url(value)], f"variavel de ambiente {name}"
            except ProxyConfigError as exc:
                raise ProxyConfigError(f"variavel de ambiente {name}: {exc}") from exc
    return [None], "padrao"
