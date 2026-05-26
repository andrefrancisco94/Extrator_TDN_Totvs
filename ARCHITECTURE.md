# Architecture

Visão geral dos módulos e fluxo de execução do Extrator TDN TOTVS.

## Estrutura de módulos

```
src/
├── main.py            CLI (typer) + orquestração do pipeline
├── crawler.py         BFS pela árvore lateral do Confluence (REST API + DOM fallback)
├── pdf_exporter.py    Exportação de cada URL para PDF via Playwright
├── pdf_merge.py       Consolidação de PDFs individuais (pypdf) com bookmarks
├── browser.py         Helpers compartilhados de Playwright (session/launch/teardown)
└── utils.py           Manifest/Checkpoint, RateLimiter, JobLock, slugify, validações
```

## Fluxo de execução

```
                    ┌────────────────────────┐
                    │ main.py run <URL>      │
                    └───────────┬────────────┘
                                │
                                ▼
              ┌──────────────────────────────────┐
              │ JobLock(.extrator.lock)          │  ◄── impede 2 processos
              └──────────────┬───────────────────┘     no mesmo output_dir
                             │
                             ▼
              ┌──────────────────────────────────┐
              │ Checkpoint(manifest.json)        │
              │  - reconcile_with_disk           │  ◄── remove entries com
              │  - load v1→v2 migration          │     PDF deletado
              └──────────────┬───────────────────┘
                             │
                             ▼
              ┌──────────────────────────────────┐
              │ crawler.crawl_confluence_tree()  │
              │  - REST API /child/page primário │
              │  - DOM sidebar fallback          │  ◄── shared RateLimiter
              │  - BFS com fila persistida       │     entre fases
              └──────────────┬───────────────────┘
                             │
                             ▼ list[URL]
              ┌──────────────────────────────────┐
              │ pdf_exporter.export_pages_to_pdf │
              │  - 1 worker (default) ou N       │
              │  - storage_state compartilhado   │
              │  - HTML→PDF + SHA-256            │
              │  - circuit breaker login/CF      │
              └──────────────┬───────────────────┘
                             │
                             ▼ list[(Path, title)]
              ┌──────────────────────────────────┐
              │ pdf_merge.merge_pdfs()           │
              │  - bookmarks + metadata          │
              │  - compress_streams (≤5000 pg)   │
              └──────────────┬───────────────────┘
                             │
                             ▼
                  output/consolidado.pdf
```

## Decisões arquiteturais

### Resume robusto via Checkpoint
- `manifest.json` (v2) persiste após cada fase: `mapped_urls`, `crawl_queue`, `crawl_seen`, `exported{}`, `failures{}`
- Batched saves (`_save_every_n=20`) evita O(N²) write em runs grandes
- `reconcile_with_disk` no início do run remove entries cujo PDF sumiu
- Backup rotativo (`.bak.1` ... `.bak.5`) antes de operações destrutivas
- SHA-256 de cada PDF detecta corrupção pós-write

### Anti-bloqueio (Cloudflare 522, rate limit)
- `RateLimiter` (utils.py) com cooldown exponencial (30s → 60s → 120s → ... até 15min)
- Jitter **só aumenta** (0 a +25%) — não defeats purpose do backoff
- Shared entre crawl e export (cooldown propaga)
- User-Agent realista rotativo (Chrome/Edge/Firefox) + viewport variável
- Detecção de Cloudflare challenge no DOM (cooldown imediato)
- Circuit breaker: aborta após 5 failures consecutivas de sessão (login/CF)
- Cookies persistentes (`browser_state.json`)
- Suporte a proxy (`--proxy` + `HTTP_PROXY` env)

### Concorrência
- Workers paralelos (`--max-workers N`): 1 browser, N pages, asyncio.Queue
- Locks: `acc.lock` (mutações de accumulator), `browser_recovery_lock` (impede 2 workers relançarem), `_storage_state_lock` (escrita browser_state.json), `state_lock` (api_cache + attempts)
- `checkpoint._save_lock` (threading.Lock): manifest writes
- `JobLock`: impede 2 processos no mesmo output_dir (PID detection)

### Atomicidade
- PDF: escreve `.tmp` → valida (header + EOF + pages > 0) → `os.replace` atômico
- Manifest: escreve `.json.tmp` → fsync → `os.replace`
- `atomic_replace_with_retry`: 5 retries com backoff para contornar antivírus

### Segurança
- `validate_start_url`: bloqueia SSRF (localhost, 127.x, IPs privados, AWS metadata)
- `validate_safe_path`: rejeita path > 240 chars (Windows MAX_PATH) e traversal
- `sanitize_proxy_for_log`: mascara credenciais em log
- `find_orphan_pdfs`: protege nomes reservados (manifest.json, .tmp)

## Lifecycle de uma URL

```
1. crawl: REST API retorna webui link → canonicalize_url → enqueue
2. process_url:
   - rate limiter wait
   - goto(url, dom_budget)
   - detect Cloudflare challenge → report_block + recriar page
   - extract children → enqueue novos URLs
   - report_success no limiter
3. export:
   - rate limiter wait
   - goto(url) + expand macros (Expand, Tabs) + emulate print
   - page.pdf() em arquivo .tmp
   - validate: header + EOF + size > 3KB + count_pdf_pages > 0
   - sha256 do PDF
   - atomic_replace .tmp → .pdf
   - record_export no manifest (batched save)
4. merge:
   - pypdf.PdfWriter consolidando todos os PDFs
   - bookmarks com title sanitizado
   - compress_content_streams (skip se > 5000 paginas)
   - atomic_replace
```

## Estados do RateLimiter

```
NORMAL ─────────► report_block ───► COOLDOWN (30s)
   ▲                                     │
   │   report_success                    │
   └─────────────────────────────────────┘

COOLDOWN ──► report_block (sucessivo) ──► COOLDOWN (60s, 120s, ..., 900s)
```

## Estados do Checkpoint

```
EMPTY ──► crawl in progress ──► crawl_complete=False, queue populada
                                          │
                                          ▼ (todas URLs processadas)
                                  crawl_complete=True
                                          │
                                          ▼ (export inicia)
                                  exported[] crescendo
                                          │
                                          ▼ (ctrl+C / crash)
                                  manifest preservado, resume da pendência
```

## Convenções

- **Async em todo lugar**: Playwright é async; nunca usar `time.sleep()` em coroutines
- **Locks asyncio para concorrência cooperativa**, `threading.Lock` apenas no Checkpoint
- **Erros narráveis**: `_suggest_action_for_error` mapeia exceções → ação concreta
- **Logs com correlation_id**: rastreabilidade entre runs concorrentes
- **Validação no boundary**: URLs/paths validados na entrada CLI; código interno confia
