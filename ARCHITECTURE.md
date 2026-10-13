# Architecture

Visão geral dos módulos e fluxo de execução do Extrator TDN TOTVS.

## Estrutura de módulos

```
src/
├── main.py              CLI (typer) + orquestração do pipeline (run, resume, proxy, jobs...)
├── crawler.py           BFS pela árvore do Confluence (REST API por id; DOM como fallback)
├── pdf_exporter.py      Exportação de cada URL para PDF (retry por URL + repescagens)
├── markdown_exporter.py Exportação para Markdown (mesmo tratamento de bloqueio)
├── pdf_merge.py         Consolidação de PDFs individuais (pypdf) com bookmarks
├── browser.py           Sessão Playwright: launch/relaunch, proxy, bloqueio de requests
│                        supérfluos (CDP), contabilidade de rede por página
├── blocking.py          Classificação de bloqueios (429/1015, desafio, WAF, 5xx),
│                        Retry-After e erros de rede transitórios
├── proxy.py             Validação de proxies, arquivo de lista e rodízio (ProxyPool)
└── utils.py             Manifest/Checkpoint, RateLimiter, JobLock, slugify, validações
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
              │  - 4 workers (default), até 32   │
              │  - sessões por grupo de proxies  │
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
- `manifest.json` (v2) persiste: `mapped_urls`, `crawl_queue`, `crawl_seen`, `page_ids`, `exported{}`, `failures{}`
- Batched saves (`_save_every_n=20`) evita O(N²) write em runs grandes; `flush()` em `finally` (Ctrl+C não perde registros)
- `reconcile_with_disk` no início do run remove entries cujo PDF sumiu
- Backup rotativo (`.bak.1` ... `.bak.5`) antes de operações destrutivas
- SHA-256 de cada PDF detecta corrupção pós-write
- **Invariante de completude**: `crawl_complete=True` só com fila vazia E todos os nós com os filhos listados. Nó bloqueado/com erro fica em `crawl_queue`
- **Re-mapeamento transacional** (`--update`): o mapa completo anterior só é substituído quando o novo termina
- **Identidade de página**: `/display/public/X/T` e `/display/X/T` são a mesma página (`display_url_variant`); a URL inicial é comparada por `start_url_key` e uma pasta de outro job é recusada

### Bloqueio por IP (Cloudflare 1015 / HTTP 429)
O limite do TDN é por IP e conta requests. A estratégia tem três partes:

1. **Gastar menos requests**
   - Crawl: 1 navegação + 1 request REST por nó com filhos (`/rest/api/content/{id}/child/page?expand=children.page`); folhas não custam nada
   - REST via `fetch()` dentro da página (pilha de rede do Chromium); `APIRequestContext` como reserva
   - Export: telemetria/polling/rastreadores bloqueados via CDP `Network.setBlockedURLs` (não `page.route`, que desliga o cache HTTP)
2. **Reagir certo ao bloqueio** (`blocking.py` classifica; `RateLimiter` decide a espera)
   - `Retry-After` do servidor = cooldown exato; sem ele, backoff exponencial (30s → ... → 15min)
   - Nenhum request durante o cooldown; `wait()` re-checa ao acordar
   - A MESMA URL/nó é repetida depois do cooldown (bloqueio é do IP, não da página)
   - AIMD: bloqueio dobra o intervalo (teto 20s); 12 sucessos o reduzem 20% até o base
   - Ban acima de `max_block_wait_seconds` → `BlockedTooLongError` → exit 3 com progresso salvo
3. **Trocar de IP** (`proxy.py`)
   - `ProxyPool`: cooldown por saída; em bloqueio a próxima saída livre assume e o browser é relançado (`relaunch_session`, `generation` invalida pages antigas)
   - Proxy com 3 falhas de conexão seguidas é descartado; credenciais sempre mascaradas

### Exportação em paralelo
- Com múltiplos proxies e workers, `ProxyPool.partition` distribui saídas em grupos exclusivos, preservando cooldown e quarentena. Cada grupo usa um navegador e um `RateLimiter`; proxies extras ficam como reservas
- Cada grupo tem um worker consumindo uma fila comum. Se uma saída entra em cooldown e outra está livre, a URL volta à fila sem perder a contagem de tentativas
- `queue.join()` inclui páginas redistribuídas. Ao concluir, cancela os workers ociosos ou aguardando ban; erro fatal/cancelamento encerra e aguarda todas as tarefas antes de fechar os navegadores
- Com uma saída, os workers compartilham contexto e limiter: sobrepõem carregamento/renderização sem multiplicar o ritmo permitido por IP
- Os resultados continuam indexados pela posição original; conclusão fora de ordem não altera nomes nem ordem do consolidado. As métricas dos limiters são somadas no resumo
- A preparação espera imagens e fontes do conteúdo, tornando imagens lazy eager depois de expandir seções, sem esperar scripts/iframes secundários do evento `load`
- A etapa de impressão usa um semáforo com capacidade igual aos processadores lógicos. Assim, 16/32 workers podem carregar páginas sem imprimir todos os PDFs ao mesmo tempo
- A triagem elimina IPs de saída repetidos, mantendo o endereço mais rápido; proxies que não informam IP continuam na lista
- O `RateLimiter` guarda intervalo, sequência de sucessos e bloqueios por saída; uma troca usa o ajuste da nova saída. Timestamps de bloqueio permanecem disponíveis para reconhecer respostas antigas ainda em voo
- Desafios de proxies com reservas são tratados pelo rodízio, sem somar falhas no circuit breaker global. Login e saídas sem alternativas mantêm essa proteção

Não confundir: desafio anti-bot / bloqueio de WAF (403) e tela de login contam no circuit breaker (5 seguidos abortam). Rate limit não conta — ele é esperado e tratado.

### Integridade do PDF
- `PageNetStats` (browser.py) registra, por navegação, sub-recursos respondidos com 429/5xx ou que falharam por rede
- `_verify_page_assets` roda antes de `page.pdf()`: imagem com 429 ⇒ página tratada como bloqueada e refeita. Na última tentativa da última repescagem, 5xx/erro de rede em recurso vira aviso (imagem quebrada na origem não pode custar a página); 429 nunca é tolerado

### Concorrência
- Workers paralelos (`--max-workers N`): 1 browser, N pages, asyncio.Queue. O limiter é único: workers não aumentam o ritmo por IP
- `_PageSlot` por worker: page + geração da sessão (recriada após relaunch)
- Resultado indexado pela posição da URL (`acc.exported[index]`): o merge segue a ordem do mapa
- Locks: `acc.lock` (accumulators), `browser_recovery_lock`, `session.rotation_lock` (troca de proxy), `_storage_state_lock`, `state_lock` (api_cache + attempts)
- `checkpoint._save_lock` (threading.Lock): manifest writes
- `JobLock`: impede 2 processos no mesmo output_dir (PID detection)

### Atomicidade
- PDF: escreve `.tmp` → valida (header + EOF + pages > 0) → `os.replace` atômico
- Manifest: escreve `.json.tmp` → fsync → `os.replace`
- `atomic_replace_with_retry`: 5 retries com backoff para contornar antivírus

### Segurança
- `validate_start_url`: bloqueia SSRF (localhost, 127.x, IPs privados, AWS metadata)
- `validate_safe_path`: rejeita path > 240 chars (Windows MAX_PATH) e traversal
- Proxies: validação estrita na CLI, credenciais mascaradas em log/tela, `proxies.txt` e `browser_state.json` no `.gitignore`
- `find_orphan_pdfs`: protege nomes reservados (manifest.json, .tmp)

## Lifecycle de uma URL

```
1. crawl:
   - nó com filhos conhecidos = 0 (folha) → resolvido sem request
   - senão: limiter.wait → REST child/page (ou navegação+DOM se a API é negada)
       OK        → filhos enfileirados (id + se é folha)
       BLOCKED   → report_block(Retry-After) → [troca de proxy] → repete o MESMO nó
       FAILED    → pausa curta, repete; persistindo fica pendente (crawl incompleto)
2. export (por URL, até 4 tentativas + repescagens):
   - limiter.wait → goto → classifica (429/desafio/5xx/login/4xx)
   - expand macros (Expand, Tabs) + emulate print
   - _verify_page_assets (imagens/CSS bloqueados?)
   - page.pdf() em .tmp → valida → sha256 → rename → record_export
       bloqueio/erro transitório → cooldown e repete a MESMA URL
3. merge:
   - entradas na ordem do mapa
   - pypdf.PdfWriter + bookmarks + compress_content_streams (skip se > 5000 paginas)
4. resumo:
   - faltou página ou o mapa não fechou → quadro "DOCUMENTAÇÃO INCOMPLETA" + exit 3
```

## Estados do RateLimiter

```
NORMAL (intervalo = base) ──► report_block ──► COOLDOWN (Retry-After, ou 30s/60s/.../900s)
   ▲                                              │  intervalo x2 (teto 20s)
   │  12 sucessos seguidos: intervalo x0.8        │
   └──────────────────────────────────────────────┘
COOLDOWN > max_block_wait ──► BlockedTooLongError ──► exit 3 (progresso salvo)
troca de proxy ──► set_cooldown(espera da nova saída, normalmente 0)
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
