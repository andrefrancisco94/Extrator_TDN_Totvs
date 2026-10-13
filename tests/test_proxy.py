"""Testes do modulo src/proxy.py (validacao, arquivo de lista e rotacao)."""
from __future__ import annotations

import pytest

from src.proxy import (
    AllProxiesDeadError,
    ProxyConfigError,
    ProxyPool,
    load_proxy_file,
    mask_proxy,
    normalize_proxy_url,
    parse_proxy_lines,
    resolve_exits,
    save_proxy_file,
    to_playwright_proxy,
)


# =============================================================================
# normalize_proxy_url
# =============================================================================

@pytest.mark.parametrize("raw,expected", [
    ("http://localhost:8080", "http://localhost:8080"),
    ("host:8080", "http://host:8080"),
    ("10.0.0.1:3128", "http://10.0.0.1:3128"),
    ("1.2.3.4:8000:user:pass", "http://user:pass@1.2.3.4:8000"),
    ("socks5://h", "socks5://h:1080"),
    ("socks5h://h:1081", "socks5://h:1081"),
    ("https://h", "https://h:443"),
    ("http://h", "http://h:80"),
    ("http://[2001:db8::1]:8080", "http://[2001:db8::1]:8080"),
])
def test_normalize_proxy_formatos_aceitos(raw, expected):
    assert normalize_proxy_url(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "direct", "DIRECT", "direto", "  "])
def test_normalize_proxy_direto_retorna_none(raw):
    assert normalize_proxy_url(raw) is None


@pytest.mark.parametrize("raw", [
    "ftp://h:21",
    "socks5://user:pass@h:1080",
    "http://host:abc",
    "http://:8080",
])
def test_normalize_proxy_invalido_levanta_erro(raw):
    """Proxy invalido nunca pode ser ignorado em silencio."""
    with pytest.raises(ProxyConfigError):
        normalize_proxy_url(raw)


def test_to_playwright_proxy_decodifica_credenciais():
    """Senha com caractere especial (percent-encoded) chega decodificada."""
    result = to_playwright_proxy("http://user:p%40ss@h:3128")
    assert result == {"server": "http://h:3128", "username": "user", "password": "p@ss"}


def test_to_playwright_proxy_direto():
    assert to_playwright_proxy(None) is None
    assert to_playwright_proxy("direct") is None


def test_mask_proxy_nao_vaza_credenciais():
    masked = mask_proxy("http://user:segredo@proxy.example.com:8080")
    assert "segredo" not in masked
    assert "user" not in masked
    assert "proxy.example.com:8080" in masked
    assert mask_proxy(None) == "conexao direta"


# =============================================================================
# Arquivo de proxies
# =============================================================================

def test_parse_proxy_lines_ignora_comentarios_e_duplicatas():
    exits = parse_proxy_lines([
        "# comentario", "", "a:1", "http://a:1", "direct", "b:2", "direct",
    ])
    assert exits == ["http://a:1", None, "http://b:2"]


def test_parse_proxy_lines_informa_linha_do_erro():
    with pytest.raises(ProxyConfigError, match="linha 2"):
        parse_proxy_lines(["a:1", "ftp://x:1"])


def test_proxy_file_roundtrip(tmp_path):
    path = tmp_path / "proxies.txt"
    save_proxy_file(path, ["http://user:pw@a:1", None, "socks5://b:1080"])
    assert load_proxy_file(path) == ["http://user:pw@a:1", None, "socks5://b:1080"]


def test_load_proxy_file_inexistente(tmp_path):
    with pytest.raises(ProxyConfigError):
        load_proxy_file(tmp_path / "nao-existe.txt")


# =============================================================================
# resolve_exits: precedencia flag > arquivo > ambiente > direto
# =============================================================================

def test_resolve_exits_precedencia(tmp_path):
    path = tmp_path / "proxies.txt"
    save_proxy_file(path, ["http://arquivo:1"])
    env = {"HTTPS_PROXY": "http://ambiente:3128"}

    exits, origin = resolve_exits(["flag:1,flag2:2"], path, env)
    assert exits == ["http://flag:1", "http://flag2:2"] and origin == "--proxy"

    exits, origin = resolve_exits(None, path, env)
    assert exits == ["http://arquivo:1"] and "arquivo" in origin

    exits, origin = resolve_exits(None, None, env)
    assert exits == ["http://ambiente:3128"] and "HTTPS_PROXY" in origin

    assert resolve_exits(None, None, {}) == ([None], "padrao")


def test_resolve_exits_env_invalido_cita_variavel():
    with pytest.raises(ProxyConfigError, match="HTTP_PROXY"):
        resolve_exits(None, None, {"HTTP_PROXY": "ftp://x:1"})


# =============================================================================
# ProxyPool: rotacao com cooldown por saida
# =============================================================================

def _pool(exits):
    clock = [0.0]
    return ProxyPool(exits, clock=lambda: clock[0]), clock


def test_pool_padrao_e_conexao_direta():
    pool = ProxyPool()
    assert len(pool) == 1
    assert pool.current is None
    assert not pool.can_rotate


def test_pool_troca_de_saida_ao_bloquear():
    """IP bloqueado: a proxima saida livre assume sem espera."""
    pool, _ = _pool(["http://a:1", "http://b:2"])
    pool.mark_blocked(600)
    switched, wait = pool.pick_best()
    assert switched and wait == 0.0
    assert pool.current == "http://b:2"


def test_pool_todas_bloqueadas_espera_a_que_libera_primeiro():
    pool, clock = _pool(["http://a:1", "http://b:2"])
    pool.mark_blocked(100)
    pool.pick_best()
    pool.mark_blocked(40)
    switched, wait = pool.pick_best()
    assert not switched and wait == 40.0
    assert pool.current == "http://b:2"
    clock[0] = 150.0
    switched, wait = pool.pick_best()
    assert wait == 0.0


def test_pool_saida_unica_nao_troca():
    pool, _ = _pool([None])
    pool.mark_blocked(30)
    assert pool.pick_best() == (False, 30.0)


def test_pool_descarta_proxy_morto_e_segue_com_os_demais():
    pool, _ = _pool(["http://morto:1", "http://vivo:2"])
    assert not pool.mark_failed()
    assert not pool.mark_failed()
    assert pool.mark_failed()  # 3a falha seguida descarta
    switched, wait = pool.pick_best()
    assert switched and pool.current == "http://vivo:2"
    assert not pool.can_rotate


def test_pool_mark_ok_zera_falhas_consecutivas():
    pool, _ = _pool(["http://a:1", "http://b:2"])
    pool.mark_failed()
    pool.mark_failed()
    pool.mark_ok()
    assert not pool.mark_failed()


def test_pool_todos_mortos_levanta_erro():
    pool, _ = _pool(["http://a:1"])
    for _ in range(3):
        pool.mark_failed()
    with pytest.raises(AllProxiesDeadError):
        pool.pick_best()


def test_pool_conexao_direta_nunca_e_descartada():
    """Falha na conexao direta e problema de rede local, nao proxy morto."""
    pool, _ = _pool([None])
    for _ in range(10):
        assert not pool.mark_failed()
    assert pool.pick_best() == (False, 0.0)
