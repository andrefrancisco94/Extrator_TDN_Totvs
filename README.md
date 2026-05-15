# Extrator TDN TOTVS

Extrai páginas de documentação Confluence (TDN/TOTVS) a partir de uma URL inicial, gera um PDF por página e cria um PDF consolidado final.

## Requisitos
- Python 3.12+
- Windows PowerShell (ou terminal equivalente)

## Instalação
1. Criar ambiente virtual:
   - `py -m venv .venv`
2. Ativar ambiente virtual:
   - `.\.venv\Scripts\Activate.ps1`
3. Instalar dependências:
   - `pip install -r requirements.txt`
4. Instalar navegador Chromium do Playwright:
   - `playwright install chromium`

## Execução
Comando base:

`python -m src.main run "https://tdn.totvs.com/display/public/PROT/Fiscal+-+Protheus+12"`

### Opções úteis
- `--output-dir output`
- `--consolidated-name TDN_TOTVS_consolidado.pdf`
- `--headless` (padrão)
- `--headed` (abre navegador para depuração)
- `--timeout-seconds 45`
- `--max-pages 10` (teste rápido)

Exemplo com opções:

`python -m src.main run "https://tdn.totvs.com/display/public/PROT/Fiscal+-+Protheus+12" --output-dir output --headless --timeout-seconds 60`

## Saídas
- PDFs individuais: `output/pages/`
- PDF consolidado: `output/TDN_TOTVS_consolidado.pdf`
- Log de execução: `output/run.log`

## Observações de escopo
- A URL inicial sempre é incluída e exportada.
- O crawler tenta seguir a ordem do menu lateral/árvore do Confluence.
- A navegação é restrita ao domínio e área da URL inicial (mesmo espaço/documentação).
- URLs são normalizadas para evitar duplicatas.
