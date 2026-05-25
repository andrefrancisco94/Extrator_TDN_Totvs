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

- Cria o ambiente virtual `.venv` (se não existir)
- Instala/atualiza as dependências
- Instala o Chromium do Playwright
- Mostra um menu interativo com duas opções:
  - **[1] Execução rápida** — só pede a URL, resto usa padrões
  - **[2] Execução avançada** — pergunta URL, pasta de saída, nome do consolidado, timeout, headless/headed e limite de páginas

## Uso manual (CLI)

Se preferir rodar pela linha de comando direto:

1. Criar ambiente virtual: `py -m venv .venv`
2. Ativar: `.\.venv\Scripts\Activate.ps1`
3. Instalar dependências: `pip install -r requirements.txt`
4. Instalar Chromium: `playwright install chromium`
5. Executar:

```powershell
python -m src.main run "https://tdn.totvs.com/display/public/PROT/Fiscal+-+Protheus+12"
```

### Opções

- `--output-dir output`
- `--consolidated-name TDN_TOTVS_consolidado.pdf`
- `--headless` (padrão) | `--headed` (abre navegador para depuração)
- `--timeout-seconds 60` (tempo de carregamento por página; aumente se a TDN estiver lenta)
- `--max-pages 10` (teste rápido)

## Saídas

- PDFs individuais: `output/pages/`
- PDF consolidado: `output/TDN_TOTVS_consolidado.pdf`
- Log de execução: `output/run.log`

## Sobre o timeout

O `--timeout-seconds` é o tempo limite para **carregar cada página** (`page.goto`). Os timeouts internos de espera por elementos (sidebar, árvore AJAX, conteúdo) **escalam automaticamente** com esse valor, então aumentar `--timeout-seconds` ajuda em páginas lentas como um todo.

## Robustez

- Cada navegação tem **retry exponencial** (até 3 tentativas) em falhas transitórias via [tenacity].
- Output formatado com [rich] (barras de progresso, tabelas, tracebacks).
- Compatibilidade total com terminal Windows via [colorama].

## Observações de escopo

- A URL inicial sempre é incluída e exportada.
- O crawler tenta seguir a ordem do menu lateral/árvore do Confluence.
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
