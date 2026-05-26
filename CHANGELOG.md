# Changelog

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
