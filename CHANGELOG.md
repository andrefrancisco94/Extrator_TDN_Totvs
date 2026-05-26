# Changelog

## [0.7.0] - 2026-05-25

### Auditoria abrangente (5 agents paralelos, 132 achados)

**Bugs CRÍTICOS corrigidos (concorrência/async):**
- `RateLimiter.report_success()` agora é async com lock (race em workers paralelos)
- `acc.consecutive_session_failures` protegido por lock (race em circuit breaker)
- `_ensure_browser_alive` com lock dedicado (`browser_recovery_lock`) impede 2 workers relançarem browser simultaneamente
- `state.api_cache` e `state.attempts` protegidos por `state_lock` (asyncio.Lock)
- `asyncio.Queue` workers usam `try/finally` com `task_done()` (sem isso, exception mata worker e gather hang)
- `_maybe_requeue_timed_out` agora async com lock

**Bugs CRÍTICOS corrigidos (lógica/edge cases):**
- `_restore_state_from_checkpoint` deduplica `mapped_urls` (manifest editado com duplicatas)
- `_expand_urls_with_retries` ignora URLs já exportadas (evita PDF duplicado)
- Migração v1→v2 extrai número de tentativas da mensagem ("timeout apos 3 tentativas")
- `reconcile_with_disk` valida `is_dir()` antes (não corrompe manifest se pages_dir for arquivo)
- `slugify` preserva info de zero-width chars, RTL markers, ™/®/©/@
- `count_pdf_pages` retorna -1 em erro (distingue de PDF válido com 0 páginas)
- `find_pending_jobs` cobre 3 níveis de profundidade (era 1)
- `find_orphan_pdfs` protege nomes reservados (manifest.json, browser_state.json) e arquivos .tmp

**Bugs CRÍTICOS corrigidos (Windows/segurança):**
- `validate_start_url` bloqueia SSRF: localhost, 127.x, IPs privados, metadata cloud (169.254.169.254)
- `validate_safe_path` rejeita paths > 240 chars (Windows MAX_PATH legacy) e path traversal
- `atomic_replace_with_retry` retry para `os.replace` (antivírus segura arquivo recém-escrito)
- `sanitize_proxy_for_log` mascara credenciais (`http://***@host:port`) — não vaza password no log

**Bugs ALTOS corrigidos:**
- **Batched saves do manifest** (`_save_every_n=20`): evita O(N²) em runs grandes (era 1 save por PDF gerado)
- **Checagem de disco mid-run** (`_check_disk_or_abort` a cada 25 PDFs): aborta antes de corromper PDFs
- **CSV report** com `QUOTE_ALL` e sanitização de newlines (URLs/titles com vírgula/aspas não quebram parsing)
- `reset` valida que `output_dir` é pasta (não arquivo)
- `_maybe_force_recrawl_for_max_pages` lógica explícita para None vs valor

**Refactor:**
- **Novo `src/browser.py`**: extraídos `BrowserSession`, `launch_browser_session`, `is_browser_alive`, `safe_close_page`, `new_page`, `save_storage_state`, `teardown_session` — eliminou ~150 linhas duplicadas entre `crawler.py` e `pdf_exporter.py`

**Melhorias:**
- **`pyproject.toml`** com metadata, dev dependencies, entry-point `extrator-tdn`, pytest config
- **`Makefile`** com targets `test`, `test-cov`, `install`, `install-dev`, `clean`
- **SIGTERM handler** (signal.SIGTERM → KeyboardInterrupt): systemd/docker stop dispara cleanup normal
- **`--json` output** em `jobs` e `failures` (scripting/integração CI)
- **`--dry-run`** (já existia, agora documentado)

**Testes (40 → 99, +59 testes):**
- `tests/test_v07_fixes.py`: 25 testes para os bugs novos (deadlock, slugify Unicode, SSRF, MAX_PATH, batched saves, clean-orphans safe, etc.)
- `tests/test_pdf_merge.py`: 7 testes com PDFs sintéticos (merge básico, skip corrompidos, bookmarks, title truncado)
- `tests/test_main_cli.py`: 18 testes da CLI via `typer.testing.CliRunner` (version, jobs, failures, reset, clean-orphans, report CSV/JSON)
- `tests/test_crawler_logic.py`: 9 testes da lógica isolada (restore, enqueue, requeue_timed_out)

## [0.6.0] - 2026-05-25

### Adicionado (resiliência anti-bloqueio)
- **Rate limit configurável** (`--request-delay`) com pausa entre requests
- **Backoff exponencial** com cooldown crescente (30s → 60s → 120s → ... `--backoff-max`)
- **User-Agent realista rotativo** (Chrome 120/121, Edge, Firefox)
- **Viewport variável** (5 tamanhos) + locale pt-BR + headers Accept-Language
- **Detecção de Cloudflare challenge** com cooldown imediato
- **Circuit breaker**: aborta export após 5 falhas consecutivas de sessão (login/CF)
- **RateLimiter compartilhado** entre crawl e export (cooldown propaga)
- **Cookies persistentes entre runs** via `storage_state` (`browser_state.json`)
- **Suporte a proxy** (`--proxy` + fallback `HTTP_PROXY`/`HTTPS_PROXY`)

### Adicionado (resume e checkpoint)
- **Resume parcial do crawl**: queue + seen + attempts persistidos a cada 10 URLs
- **Retry automático entre runs**: URLs em `failures` re-tentadas até 5x
- **Validação cruzada manifest×disco** no início do run (remove órfãos do manifest)
- **Auto-backup do manifest** antes de operações destrutivas (`--fresh`, `reset`)
- **Migração v1→v2** automática do manifest
- **Cache REST API intra-run** evita re-fetch quando URL é re-visitada após timeout

### Adicionado (CLI e UX)
- `python -m src.main jobs` — lista trabalhos incompletos
- `python -m src.main failures` — lista URLs com falha e tentativas
- `python -m src.main reset [--hard]` — reseta job (soft = só manifest; hard = + PDFs)
- `python -m src.main clean-orphans` — remove PDFs órfãos sem entrada no manifest
- `python -m src.main report --format csv|json` — exporta relatório de execução
- `--dry-run` — mapeia mas não exporta PDFs (estimativa rápida)
- `--max-workers N` — paralelização (1 browser/context, N pages)
- `iniciar.bat`: novo menu com **[1] Continuar trabalho anterior** (detecta jobs automaticamente)
- `iniciar.bat`: **[6] Configurar velocidade** com perfis (lento/médio/rápido/custom)
- Mensagens de erro **acionáveis** (sugere remediar 522, Chromium, disco cheio, permissão)

### Adicionado (qualidade)
- **PDF parcial não passa validação**: escrita em `.tmp` + rename atômico só após validar header+EOF+páginas
- **`count_pdf_pages`**: validação adicional (rejeita PDF sem páginas legíveis)
- **`_MIN_VALID_PDF_SIZE`** subido de 1KB → 3KB (filtra páginas de erro)
- **Slugify Unicode-friendly** (preserva acentos via NFKD, transformações semânticas para C++/C#/&)
- **PDF consolidado com compressão** de streams (`compress_content_streams`)
- **DOM budget cap** subido de 60s → 120s (TDN tem páginas pesadas até 2min)
- **Timeout default** subido de 120s → 180s
- **Log com rotação automática** (10MB × 5 backups)

### Corrigido
- Race condition no `RateLimiter` (lock segurava durante `asyncio.sleep`, bloqueava workers paralelos)
- `_maybe_requeue_timed_out` removia de `ordered_urls` causando inconsistência no resume
- URLs com timeout não iam para `failures` (resume futuro perdia)
- `urls_pending_retry` ignorado em `--regenerate`
- Worker paralelo não propagava `page` recriada após timeout
- `checkpoint.save()` sem lock corrompia manifest em paralelo
- PDFs parciais por Ctrl+C passavam validação (header+EOF intactos)
- RateLimiter recriado entre crawl/export perdia cooldown
- `force_recrawl` não persistia limpeza de queue/seen
- `_validate_generated_pdf` só 3 retries (~0.6s) → 8 retries (~3.6s) para antivírus pesado

### Mudanças incompatíveis
- Manifest format: v1 → v2. `failures` mudou de `dict[str, str]` para `dict[str, dict]` com `{error, attempts, last_attempt}`. Migração automática preserva dados.
- `Manifest` agora tem campos `crawl_queue`, `crawl_seen`, `crawl_max_pages`.

## [0.5.0] - anterior
- Versão inicial com extração TDN → PDF consolidado, resume básico, slow page detection, expansão de macros Expand/Tabs.
