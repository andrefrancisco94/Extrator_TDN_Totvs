# AGENTS.md — Extrator TDN TOTVS

## Objetivo do projeto
Criar um extrator para documentação Confluence (TDN/TOTVS) que:
1. receba uma URL inicial,
2. percorra a árvore de páginas (menu lateral/filhos),
3. gere um PDF de cada página em ordem,
4. mantenha todos os PDFs individuais salvos em pasta,
5. gere um PDF final consolidado com a aglutinação de todos.

## Linguagem escolhida (recomendada)
**Python 3.12+**

### Motivos da escolha
- Excelente automação web com **Playwright** (suporte robusto ao Chromium).
- Geração de PDF com resultado igual ao comportamento de impressão da página (similar ao `Ctrl+P` do navegador).
- Ecossistema simples para parsing e merge de PDF (`beautifulsoup4`, `pypdf`).
- Boa produtividade para crawler e tratamento de URLs/ordenação.
- Fácil execução em Windows (seu ambiente atual).

## Stack técnica padrão
- `playwright`: navegar, carregar páginas Confluence e exportar PDF.
- `beautifulsoup4` (ou seletores Playwright): mapear links da árvore/menu lateral.
- `pypdf`: unir PDFs individuais no arquivo final.
- `typer` (opcional): CLI amigável.
- `pathlib`, `urllib.parse`, `re`: organização de caminhos e normalização de URLs.

## Requisitos funcionais obrigatórios
- A URL inicial **sempre entra na fila** e deve ter PDF gerado.
- O mapeamento deve seguir os links filhos na navegação lateral da documentação.
- Não repetir páginas (deduplicação por URL canônica).
- Preservar a ordem de navegação definida pela árvore/menu.
- Salvar PDFs individuais em pasta dedicada (ex.: `output/pages/`).
- Gerar PDF consolidado final (ex.: `output/TDN_TOTVS_consolidado.pdf`).
- Registrar log de execução (ex.: `output/run.log`) com páginas processadas e erros.

## Regras de escopo para crawl
- Restringir navegação ao domínio `tdn.totvs.com`.
- Restringir às páginas da mesma área/raiz da documentação inicial (evitar sair para páginas externas não relacionadas).
- Ignorar âncoras (`#secao`) como páginas novas.
- Normalizar URLs removendo query params irrelevantes para evitar duplicatas.

## Fluxo de execução esperado
1. Receber URL inicial.
2. Abrir a página no browser headless.
3. Capturar links do menu lateral/árvore.
4. Construir lista ordenada de URLs (incluindo a inicial).
5. Visitar uma a uma e exportar PDF individual.
6. Ao final, mesclar todos os PDFs na mesma ordem.
7. Gerar resumo final (quantidade de páginas, falhas, caminho dos arquivos).

## Estrutura sugerida do projeto
- `src/main.py` — CLI e orquestração.
- `src/crawler.py` — coleta/mapeamento de links Confluence.
- `src/pdf_exporter.py` — exportação de páginas em PDF.
- `src/pdf_merge.py` — mesclagem dos PDFs.
- `src/utils.py` — normalização de URL, slug, logs.
- `output/pages/` — PDFs individuais.
- `output/` — PDF final e logs.

## Setup recomendado (Windows)
1. `py -m venv .venv`
2. `.\.venv\Scripts\Activate.ps1`
3. `pip install playwright beautifulsoup4 pypdf typer`
4. `playwright install chromium`

## Critério de aceite
- Dada uma URL inicial de documentação TDN, o processo gera:
  - PDFs individuais de todas as páginas da árvore em ordem.
  - Um PDF consolidado único com o conteúdo na mesma ordem.
  - Logs claros de sucesso/erro por URL.
