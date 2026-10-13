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
- Cria o ambiente virtual `.venv` (se não existir; recria se quebrado)
- Instala/atualiza as dependências (usa marker file para pular se já instalado)
- Instala o Chromium do Playwright (~150MB, só na primeira vez)
- Mostra um menu interativo:
  - **[1] Continuar trabalho anterior** — lista os jobs incompletos (em `output/` e na pasta atual), pergunta qual retomar e lê a URL do próprio job
  - **[F] Reprocessar páginas que faltaram** — tenta de novo **só** as páginas sem PDF; não re-mapeia a árvore (se o mapeamento tiver ficado incompleto, ele é retomado antes)
  - **[2] Iniciar novo trabalho** — só pede URL e pasta (um nome simples vira `output\<nome>`)
  - **[3] Execução avançada** — configura todos os parâmetros
  - **[4] Verificar atualizações** — escolhe um job existente na lista, re-mapeia a árvore e baixa só as páginas novas
  - **[5] Regenerar todos os PDFs** — escolhe um job existente e refaz tudo
  - **[6] Configurar velocidade** — ponto de partida do rate limit, workers e timeouts
  - **[P] Configurar proxy** — adiciona/testa/remove proxies (arquivo `proxies.txt`)
  - **[7] Listar trabalhos** | **[8] Reinstalar dependências** | **[9] Reinstalar Chromium** | **[0] Sair**

Nas opções 1, F, 4 e 5 **nada é digitado de novo**: URL, pasta e nome do consolidado vêm do `manifest.json` do job escolhido na lista (para um job guardado fora de `output/` e da pasta atual, digite o caminho da pasta em vez do número). Isso evita o erro de criar um job novo (e baixar tudo outra vez) por digitar uma pasta diferente da original.

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
- `--request-delay 2.0` — intervalo **inicial** entre requests; o extrator aumenta sozinho após um bloqueio e volta a reduzir aos poucos
- `--backoff-initial 30` / `--backoff-max 900` — cooldown usado quando o servidor bloqueia **sem** informar `Retry-After`
- `--max-block-wait 7200` — espera máxima por um único bloqueio; acima disso o run para com o progresso salvo (código de saída 3)
- `--max-workers 4` — workers paralelos para PDFs (padrão 4; máximo 32). Com vários proxies, cada worker usa um grupo exclusivo de saídas, com navegador e espera próprios; sem eles, as abas compartilham o intervalo por IP. O perfil paralelo do menu usa 16; a impressão em PDF tem concorrência limitada aos processadores lógicos, permitindo mais downloads sem multiplicar as impressões simultâneas
- `--proxy URL` — proxy de saída; repita a flag (ou separe por vírgula) para vários
- `--proxy-file proxies.txt` — arquivo com um proxy por linha
- `--no-proxy` — ignora arquivo de proxies e variáveis `HTTP_PROXY`/`HTTPS_PROXY`
- `--update` — re-mapeia a árvore procurando páginas novas (reusa PDFs existentes)
- `--regenerate` — re-mapeia E regenera todos os PDFs
- `--fresh` — ignora `manifest.json` e começa do zero

### Outros comandos

- `python -m src.main resume` — retoma/atualiza um job existente **sem redigitar a URL** (lista os jobs e pergunta qual)
  - `--output-dir <pasta>` escolhe o job direto (obrigatório em scripts: sem ele o comando sempre pergunta)
  - `--consolidated-name` troca o nome do PDF consolidado (padrão: o mesmo de quando o job foi criado)
  - `--mode auto` (continua de onde parou) | `retry-failed` (só as páginas que faltaram) | `update` (páginas novas) | `regenerate` (refaz tudo)
- `python -m src.main proxy add|list|test|clear` — gerencia a lista de proxies (`proxies.txt`)
- `python -m src.main reexport 110 69 --output-dir output/area` — marca páginas já exportadas para serem geradas de novo (ex.: PDF que saiu sem imagens). Os números são o prefixo `NNNN` do arquivo em `pages/` (`0110-...pdf` → `110`); depois rode `resume --mode retry-failed`
- `python -m src.main jobs` — lista trabalhos incompletos em `output/` e na pasta atual (`--all` inclui os concluídos, `--json` para scripts)
- `python -m src.main status --output-dir output/area` — snapshot detalhado de um job (progresso, falhas, disco)
- `python -m src.main failures --output-dir output/area` — lista URLs com falha (tentativas, último erro)
- `python -m src.main reset output/area [--hard]` — reseta job (soft = só manifest; hard = + PDFs)
- `python -m src.main clean-orphans --output-dir output/area` — remove PDFs sem entrada no manifest
- `python -m src.main clean-tmp --output-dir output/area` — remove `.pdf.tmp` órfãos
- `python -m src.main report --format csv|json` — exporta relatório com stats (p50/p95/p99)
- `python -m src.main version` — mostra a versão

### Flags úteis para CI/scripting

```bash
# Termina só o que faltou num job existente (URL lida do manifest)
python -m src.main resume --output-dir output/area --mode retry-failed

# Job grande: até 16 saídas em paralelo, mantendo 2s entre páginas por saída
python -m src.main resume --output-dir "C:\Extrator" --max-workers 16 --request-delay 2 --proxy-file proxies.txt

# Modo silencioso (suprime banners/progresso, só erros)
python -m src.main run URL --quiet --yes

# Debug verbose (DEBUG logging com correlation_id)
python -m src.main run URL --debug

# Saída JSON para integração
python -m src.main jobs --json
python -m src.main status --json --output-dir output/area
```

### Códigos de saída

| Código | Significado |
|---|---|
| `0` | Tudo exportado e consolidado |
| `1` | Nada foi gerado / erro fatal |
| `2` | Parâmetro inválido, pasta de outro job ou falha ao consolidar |
| `3` | **Documentação incompleta** (páginas sem PDF, páginas não regeneradas, mapeamento não concluído ou IP bloqueado por tempo demais). O progresso está salvo; o quadro final diz qual opção conclui |
| `4` | `resume` sem nada a fazer (nenhum job encontrado ou escolha cancelada) |
| `130` | Interrompido com Ctrl+C |

## Bloqueio do Cloudflare (erro 1015 / HTTP 429)

O TDN fica atrás do Cloudflare, que **limita os acessos por IP**. Quando o limite estoura, o IP é banido por um tempo (de segundos a 1 hora) e toda resposta vira `429` com a página "Error 1015 — You are being rate limited". O que o extrator faz:

- **Gasta poucos requests.** O mapeamento navega uma única vez e lista os filhos de cada página pela REST API do Confluence (um request por página **com** filhos; folhas não custam nada). Antes era uma navegação completa por página só para descobrir o id. Na exportação, telemetria e polling do Confluence são bloqueados no navegador.
- **Espera o tempo certo.** O `Retry-After` devolvido pelo servidor define o cooldown exato; nada é enviado durante o ban (insistir só o prolonga). Sem `Retry-After`, usa backoff exponencial.
- **Repete a mesma página.** Uma página bloqueada (ou com erro de rede transitório) é tentada de novo no próprio run, depois do cooldown, e ainda passa por repescagens no final. Um bloqueio não custa mais uma página da documentação.
- **Não grava PDF sem imagens.** Se o HTML chega mas as imagens da página são respondidas com 429, a página é tratada como bloqueada e refeita.
- **Ajusta o ritmo sozinho.** Cada bloqueio dobra o intervalo entre requests; uma sequência de sucessos o reduz devagar até o valor configurado.
- **Troca de IP se houver proxies.** Com mais de uma saída de rede, o IP bloqueado descansa e o trabalho segue por outro (ver *Proxies*).
- **Para de forma limpa se o ban for longo demais** (`--max-block-wait`, default 2h): código de saída 3, progresso salvo, e a opção [1] do menu continua depois (se era uma atualização/regeneração, repita a opção [4]/[5]: o quadro final indica qual).

O log mostra, por página, quantos requests ela custou (`PDF gerado ... (N requests)`), e o resumo final informa quantos bloqueios ocorreram.

## Proxies

Como o limite é por IP, um proxy muda o IP de saída. Vale a pena principalmente quando a rede é compartilhada (VPN/Zero Trust corporativo: várias pessoas saem pelo mesmo IP e dividem o mesmo limite).

```bat
REM pelo menu: opcao [P]
python -m src.main proxy add http://usuario:senha@host:8080
python -m src.main proxy add outro-host:3128
python -m src.main proxy add direct
python -m src.main proxy test
python -m src.main run URL --proxy-file proxies.txt
```

- Formatos aceitos: `http://usuario:senha@host:porta`, `host:porta`, `host:porta:usuario:senha`, `socks5://host:porta` e `direct` (conexão direta, para incluí-la no rodízio).
- **Um proxy:** todos os requests saem por ele. **Vários, com workers paralelos:** os proxies trabalham ao mesmo tempo, com uma sessão por grupo e saídas exclusivas. Proxies extras ficam como reservas do grupo. Uma troca fecha apenas o navegador daquele worker; os demais continuam. Com um worker, mantém o rodízio sequencial.
- **Página bloqueada volta para a fila** quando há outra saída livre, preservando o limite de tentativas. O worker bloqueado aguarda antes de reservar outra página; quando a fila termina, o run encerra sem esperar os bans dos workers ociosos.
- **Ritmo por saída:** um proxy livre usa o intervalo base, sem herdar a desaceleração do IP anterior. Se o extrator retornar ao IP antigo, recupera o ajuste daquele IP. Desafios de proxies com alternativas acionam o rodízio, sem interromper os outros workers pelo circuit breaker global.
- Proxy digitado errado **aborta** a execução (nunca roda "sem proxy" em silêncio). SOCKS com usuário/senha não é suportado pelo Chromium.
- **Proxy que falha sai do rodízio.** Erro de conexão **ou timeout de página** por um proxy conta contra *o proxy* (não vira "bloqueio do servidor": o ritmo dos requests não muda). Na 3ª falha seguida ele vai para a quarentena (10 min, depois 20) e o trabalho segue pelo próximo; na 3ª quarentena sem nenhum sucesso é descartado.
- **Lista grande (6+ proxies) passa por triagem** no início de cada run: um request leve por proxy, em paralelo e sem browser (2000 proxies levam cerca de 2 a 3 min). Só entram os que respondem, do mais rápido ao mais lento. `python -m src.main proxy test` faz o mesmo teste e oferece regravar o `proxies.txt` só com os aprovados (`--prune`; o original fica em `proxies.txt.bak`). `TDN_SKIP_PROXY_CHECK=1` desliga a triagem.
- A exportação PDF salva os selecionados em `<pasta do job>/proxies_aprovados.txt`. Para uma retomada próxima, use esse arquivo em `--proxy-file`: ele é revalidado, mas evita testar novamente milhares de endereços que já falharam. A lista original permanece disponível para uma nova triagem completa.
- **Listas públicas/gratuitas de proxy rendem pouco**: a maioria dos endereços está morta, os vivos são lentos e instáveis, e muitos já estão banidos no Cloudflare. A triagem e a quarentena evitam que o run trave neles, mas poucos proxies bons (pagos ou seus) funcionam melhor que milhares de públicos.
- **Intervalo por saída:** use inicialmente 2 s, inclusive com 16 workers. Os workers aumentam a concorrência entre saídas sem diminuir o intervalo de cada IP. No menu, o modo "Paralelo com proxies" configura 16 workers e 2 s. Se houver menos proxies utilizáveis que workers, usa uma sessão por proxy; sem proxies múltiplos, usa abas no mesmo navegador. A triagem mantém o proxy mais rápido quando vários endereços informam o mesmo IP de saída. O limite do servidor continua valendo por IP.
- `proxies.txt` pode conter senha: está no `.gitignore`. Nos logs as credenciais aparecem mascaradas.
- O extrator não fornece proxies; use um serviço/servidor seu.

## Resume parcial e checkpoint

Toda execução salva incrementalmente em `<pasta do job>/manifest.json`:

- O mapeamento persiste fila + páginas já resolvidas + ids das páginas
- Cada PDF gerado é registrado no manifest com tamanho, título, tempo e SHA-256
- URLs que continuaram falhando depois das tentativas do run ficam em `failures`

**Comportamento de retomada:**

- Interrompeu (Ctrl+C, kill, crash, ban longo)? A próxima execução retoma do ponto exato — o manifest é gravado também nesses casos
- Uma página cujos filhos não puderam ser listados **nunca** é tratada como "sem filhos": ela fica na fila e o mapeamento só é marcado como completo quando todas forem resolvidas
- `--update` num job com mapa completo só troca o mapa se o novo mapeamento terminar; se for interrompido, o mapa anterior é mantido e é preciso repetir a opção [4]
- O consolidado é sempre montado com **todas** as páginas que têm PDF válido, na ordem do mapa — um run abortado ou um `--regenerate` com falhas nunca o encolhe (e o run sai com código 3 dizendo o que não foi atualizado)
- Mapas fechados por versões antigas com páginas sem os filhos listados são re-mapeados automaticamente no próximo resume
- PDFs já gerados são reaproveitados (validação por header+EOF+tamanho mínimo de 3KB), inclusive quando a mesma página aparece como `/display/X/...` e `/display/public/X/...`
- PDFs em geração são escritos primeiro em `.tmp` e renomeados só após validação — Ctrl+C nunca deixa PDF parcial que parece válido
- Uma pasta que já contém um job de **outra** URL inicial é recusada (o registro existente não é descartado); use outra pasta ou `--fresh`

**Múltiplos jobs:** use uma pasta por projeto (`output/MinhaArea`). O menu detecta jobs em `output/` e na pasta atual.

## Saídas

- **PDFs individuais**: `output/pages/`
- **PDF consolidado**: `output/TDN_TOTVS_consolidado.pdf`
- **Log de execução**: `output/run.log`
- **Log de páginas lentas**: `output/slow_pages.log` (gerado se houver páginas lentas)

## Sobre timeout e páginas lentas

O extrator usa `wait_until="domcontentloaded"` em vez de `"load"`, então retorna **assim que o HTML está pronto** — não fica esperando analytics/tracking que nunca acaba.

**Orçamento por página** (`--timeout-seconds`, default 180s):

- Fase 1 — `goto` aguardando DOM: até metade do total (máximo 120s)
- Fase 2 — `wait_for_selector` por conteúdo/sidebar: o tempo restante
- Fase 3 — até 20 s para imagens e fontes do conteúdo, incluindo imagens lazy das seções expandidas. Não espera o evento `load` da página inteira, que inclui scripts e iframes secundários

**Detecção de páginas lentas:**

- Páginas que demoram ≥ `--slow-threshold-seconds` (default 60s) são marcadas como lentas
- São listadas no terminal em tabela amarela ao final
- São gravadas em `output/slow_pages.log` ordenadas do mais lento para o mais rápido
- Páginas que estouram o timeout total são registradas como `pdf-timeout` ou `crawl-timeout` no mesmo log

**Timeout**: a página não é repetida de imediato (com o mesmo orçamento estouraria de novo); ela entra na repescagem do fim do run. Bloqueios (429/5xx) e erros de rede transitórios são repetidos após o cooldown.

## Conteúdo colapsado e abas (Expand macro / Tabs)

Páginas Confluence com **macros Expand** (seções colapsáveis com `▶ Expandir`) e **macros Tabs** (abas como `01-Acesso | 02-Workflow`) têm conteúdo escondido por padrão. O extrator injeta CSS + JavaScript antes de gerar o PDF para:

- Expandir todas as seções `.expand-content` da macro Expand
- Mostrar todos os painéis de abas (`.tabs-pane`, `[role="tabpanel"]`, `.aui-tabs`)
- Adicionar um cabeçalho com o **nome da aba** antes de cada painel, para o PDF não perder contexto
- Abrir todos os elementos `<details>` nativos

Resultado: o PDF contém todo o conteúdo das abas e seções, com headings claros separando cada aba.

## Robustez

- **Rate limit adaptativo**: `Retry-After` do servidor, intervalo que aumenta a cada bloqueio e se recupera aos poucos
- **RateLimiter compartilhado** entre fase de crawl e export — cooldown propaga, não é resetado
- **Retry no mesmo run** de páginas bloqueadas ou com erro transitório + repescagens no final
- **Checagem de sub-recursos**: página com imagens bloqueadas (429) não vira PDF "com sucesso"
- **Manifest atômico thread-safe** (lock + write-tmp + rename + fsync) — múltiplos workers não corrompem
- **PDFs com escrita atômica** (.tmp + rename apenas após validar) — Ctrl+C nunca deixa arquivo parcial passando
- **Resume exato do crawl** (fila, páginas resolvidas e ids persistidos)
- **Consolidado na ordem do mapa**, mesmo com workers paralelos, proxies simultâneos e páginas redistribuídas/geradas em repescagem
- **Código de saída 3 + quadro "DOCUMENTAÇÃO INCOMPLETA"** quando falta alguma página — nunca termina como sucesso com buracos
- **Circuit breaker**: aborta export após 5 falhas consecutivas de login/desafio anti-bot
- **Log com rotação automática** (10MB × 5 backups) — não enche disco
- **Mensagens de erro acionáveis**: ao falhar, sugere ação concreta
- Output formatado com [rich] (barras de progresso, tabelas de resumo, tracebacks)
- PDFs corrompidos são pulados na consolidação (resto continua)

## Testes

```bat
.venv\Scripts\python.exe -m pytest tests/            &REM unitarios (sem rede, sem Chromium)
.venv\Scripts\python.exe -m pytest tests/ -m e2e     &REM ponta a ponta contra um TDN falso local
```

Os testes `e2e` sobem um servidor local que imita o Confluence e o rate limit do Cloudflare (429 + `Retry-After` + página 1015) e rodam o pipeline inteiro contra ele. Nenhum request sai da máquina.

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
- `tenacity` — dependência legada (os retries agora passam pelo rate limiter)

[tenacity]: https://github.com/jd/tenacity
[rich]: https://github.com/Textualize/rich
