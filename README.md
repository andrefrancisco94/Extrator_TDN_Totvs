# Extrator TDN TOTVS

Extrai páginas de documentação Confluence (TDN/TOTVS) a partir de uma URL inicial, gera um PDF por página e cria um PDF consolidado final.

## Requisitos

- Python 3.12+
- Windows (testado em Windows 10/11)

## Uso rápido (recomendado)

Basta dar duplo clique em **`iniciar.bat`** ou rodá-lo no terminal:

```bat
iniciar.bat
```

O script faz tudo automaticamente:

- Pede privilégios de administrador (UAC)
- Relança no **Windows Terminal** se disponível (fonte bonita)
- Cria o ambiente virtual `.venv` (se não existir; recria se quebrado)
- Instala/atualiza as dependências (usa marker file para pular se já instalado)
- Instala o Chromium do Playwright (~150MB, só na primeira vez)
- Mostra um menu interativo:
  - **[1] Continuar trabalho anterior** — detecta jobs incompletos em `output/` e oferece retomar (resume automático)
  - **[2] Iniciar novo trabalho** — só pede URL e pasta, usa rate limit configurado
  - **[3] Execução avançada** — configura todos os parâmetros
  - **[4] Verificar atualizações** (`--update`) — busca páginas novas, reusa PDFs existentes
  - **[5] Regenerar todos os PDFs** (`--regenerate`)
  - **[6] Configurar velocidade** — escolhe perfil (lento/médio/rápido/custom) que controla rate limit, workers e timeouts
  - **[7] Listar trabalhos pendentes** — mostra todos os jobs incompletos no diretório
  - **[8] Reinstalar dependências** | **[9] Reinstalar Chromium** | **[0] Sair**

## Uso manual (CLI)

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m src.main run "https://tdn.totvs.com/display/public/PROT/Fiscal+-+Protheus+12"
```

### Opções

- `--output-dir output`
- `--consolidated-name TDN_TOTVS_consolidado.pdf`
- `--headless` (padrão) | `--headed` (abre navegador para depuração)
- `--timeout-seconds 180` — **limite total por página** (default 180s = 3min; TDN tem páginas pesadas)
- `--slow-threshold-seconds 60` — páginas demorando ≥ este valor são registradas em `slow_pages.log`
- `--max-pages 10` (teste rápido)
- `--request-delay 2.0` — pausa em segundos entre requests (anti-rate-limit, default 2s)
- `--backoff-initial 30` — cooldown inicial em segundos após detectar bloqueio (5xx/429/timeout)
- `--backoff-max 900` — teto do cooldown (default 900s = 15min) após bloqueios sucessivos
- `--max-workers 1` — workers paralelos para PDFs (cuidado: mais workers = mais risco de bloqueio)
- `--update` — re-mapeia a árvore procurando páginas novas (reusa PDFs existentes)
- `--regenerate` — re-mapeia E regenera todos os PDFs
- `--fresh` — ignora `manifest.json` e começa do zero

### Outros comandos

- `python -m src.main jobs --output-dir output` — lista trabalhos incompletos (`--json` para scripts)
- `python -m src.main status --output-dir output/area` — snapshot detalhado de um job (progresso, falhas, disco)
- `python -m src.main failures --output-dir output/area` — lista URLs com falha (tentativas, último erro)
- `python -m src.main reset output/area [--hard]` — reseta job (soft = só manifest; hard = + PDFs)
- `python -m src.main clean-orphans --output-dir output/area` — remove PDFs sem entrada no manifest
- `python -m src.main clean-tmp --output-dir output/area` — remove `.pdf.tmp` órfãos
- `python -m src.main report --format csv|json` — exporta relatório com stats (p50/p95/p99)
- `python -m src.main version` — mostra a versão

### Flags úteis para CI/scripting

```bash
# Retry rápido só de URLs com falha (skip crawl)
python -m src.main run URL --retry-failed-only --yes

# Modo silencioso (suprime banners/progresso, só erros)
python -m src.main run URL --quiet --yes

# Debug verbose (DEBUG logging com correlation_id)
python -m src.main run URL --debug

# Saída JSON para integração
python -m src.main jobs --json
python -m src.main status --json --output-dir output/area
```

## Anti-bloqueio (Cloudflare 522/429)

O servidor TDN usa Cloudflare e bloqueia clientes que fazem requests rápidos demais. O extrator inclui defesas:

- **Rate limit configurável** (`--request-delay`) — pausa entre cada request
- **Backoff exponencial** — quando detecta 5xx/429/timeout, aumenta o cooldown (30s → 60s → 120s → ... até `--backoff-max`)
- **User-Agent realista rotativo** — cada navegador aberto usa UA de Chrome/Edge/Firefox real (não "HeadlessChrome")
- **Viewport variável** — tamanhos de janela realistas para reduzir fingerprinting
- **Locale pt-BR + headers Accept-Language** — comportamento de usuário brasileiro real
- **Detecção de challenge do Cloudflare** — se o servidor retornar página de "verifying you are human", aciona cooldown longo
- **Circuit breaker** — após 5 falhas consecutivas de login/CF, aborta o pipeline (resume retoma depois)

**Se você receber erro 522:** espere 15-60min, depois rode com `--request-delay 5` ou use o modo `[Lento e seguro]` do `iniciar.bat`.

## Resume parcial e checkpoint

Toda execução salva incrementalmente em `output/manifest.json`:

- A cada 10 URLs mapeadas, o crawl persiste fila + URLs vistas + URLs já mapeadas
- Cada PDF gerado é registrado no manifest com tamanho, título e tempo
- URLs com falha (timeout, 5xx, login) ficam em `failures` com contagem de tentativas

**Comportamento de retomada:**

- Se você interromper (Ctrl+C, kill, crash) o **crawl** no meio, a próxima execução retoma da fila salva — não re-mapeia o que já foi
- URLs com timeout no crawl voltam para a fila com até 2 tentativas intra-run; se desistir, vão para `failures` e são re-tentadas no próximo run (até 5 tentativas totais)
- PDFs já gerados são reaproveitados (validação por header+EOF+tamanho mínimo de 3KB)
- PDFs em geração são escritos primeiro em `.tmp` e renomeados só após validação — Ctrl+C nunca deixa PDF parcial que parece válido

**Múltiplos jobs:** rode com `--output-dir output/MinhaArea` para isolar projetos diferentes. O `iniciar.bat` opção [1] detecta automaticamente jobs incompletos em qualquer subpasta de `output/`.

## Saídas

- **PDFs individuais**: `output/pages/`
- **PDF consolidado**: `output/TDN_TOTVS_consolidado.pdf`
- **Log de execução**: `output/run.log`
- **Log de páginas lentas**: `output/slow_pages.log` (gerado se houver páginas lentas)

## Sobre timeout e páginas lentas

O extrator usa `wait_until="domcontentloaded"` em vez de `"load"`, então retorna **assim que o HTML está pronto** — não fica esperando analytics/tracking que nunca acaba.

**Orçamento por página** (`--timeout-seconds`, default 120s):

- Fase 1 — `goto` aguardando DOM: até 30s (ou metade do total, o que for menor)
- Fase 2 — `wait_for_selector` por conteúdo/sidebar: o tempo restante
- Fase 3 — best-effort para imagens completarem (não falha se não terminar)

**Detecção de páginas lentas:**

- Páginas que demoram ≥ `--slow-threshold-seconds` (default 60s) são marcadas como lentas
- São listadas no terminal em tabela amarela ao final
- São gravadas em `output/slow_pages.log` ordenadas do mais lento para o mais rápido
- Páginas que estouram o timeout total são registradas como `pdf-timeout` ou `crawl-timeout` no mesmo log

**Sem retry em timeout**: retry com mesmo timeout vai timeoutar de novo. Apenas erros de rede transientes (conexão, 5xx) tentam de novo.

## Conteúdo colapsado e abas (Expand macro / Tabs)

Páginas Confluence com **macros Expand** (seções colapsáveis com `▶ Expandir`) e **macros Tabs** (abas como `01-Acesso | 02-Workflow`) têm conteúdo escondido por padrão. O extrator injeta CSS + JavaScript antes de gerar o PDF para:

- Expandir todas as seções `.expand-content` da macro Expand
- Mostrar todos os painéis de abas (`.tabs-pane`, `[role="tabpanel"]`, `.aui-tabs`)
- Adicionar um cabeçalho com o **nome da aba** antes de cada painel, para o PDF não perder contexto
- Abrir todos os elementos `<details>` nativos

Resultado: o PDF contém todo o conteúdo das abas e seções, com headings claros separando cada aba.

## Robustez

- **Rate limit adaptativo** com backoff exponencial em bloqueios (5xx/429/timeout/CF challenge)
- **RateLimiter compartilhado** entre fase de crawl e export — cooldown propaga, não é resetado
- **Manifest atômico thread-safe** (lock + write-tmp + rename + fsync) — múltiplos workers não corrompem
- **PDFs com escrita atômica** (.tmp + rename apenas após validar) — Ctrl+C nunca deixa arquivo parcial passando
- **Resume parcial do crawl** (queue + seen persistidos a cada 10 URLs)
- **Retry automático entre runs**: URLs em `failures` voltam para nova tentativa (até 5x)
- **Circuit breaker**: aborta export após 5 falhas consecutivas de login/CF challenge
- **Log com rotação automática** (10MB × 5 backups) — não enche disco
- **Mensagens de erro acionáveis**: ao falhar, sugere ação concreta (aumentar delay, reinstalar Chromium, etc)
- Cada navegação tem **retry** via [tenacity] em erros de rede transientes
- Output formatado com [rich] (barras de progresso, tabelas de resumo, tracebacks)
- PDFs corrompidos são pulados na consolidação (resto continua)

## Observações de escopo

- A URL inicial sempre é incluída e exportada.
- O crawler segue a ordem do menu lateral/árvore do Confluence.
- A navegação é restrita ao domínio e área da URL inicial (mesmo espaço/documentação).
- URLs são normalizadas para evitar duplicatas.

## Stack

- `playwright` — navegação headless e exportação PDF
- `beautifulsoup4` — parsing HTML (uso pontual)
- `pypdf` — merge dos PDFs individuais
- `typer` — CLI
- `rich` — UI no terminal
- `colorama` — compatibilidade ANSI no Windows
- `tenacity` — retry em operações de rede

[tenacity]: https://github.com/jd/tenacity
[rich]: https://github.com/Textualize/rich
