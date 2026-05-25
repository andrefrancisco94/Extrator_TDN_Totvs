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
  - **[1] Execução rápida** — só pede a URL, resto usa padrões
  - **[2] Execução avançada** — pergunta URL, pasta de saída, nome do consolidado, timeout, slow threshold, headless/headed e limite de páginas
  - **[3] Reinstalar dependências** — força refresh
  - **[4] Sair**

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
- `--timeout-seconds 120` — **limite total por página** (default 120s = 2min)
- `--slow-threshold-seconds 60` — páginas demorando ≥ este valor são registradas em `slow_pages.log`
- `--max-pages 10` (teste rápido)

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

- Cada navegação tem **retry** (até 2 tentativas) em erros de rede transientes via [tenacity]
- Output formatado com [rich] (barras de progresso, tabelas de resumo, tracebacks)
- Compatibilidade total com terminal Windows via [colorama]
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
[colorama]: https://github.com/tartley/colorama
