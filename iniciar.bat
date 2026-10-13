@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Extrator TDN TOTVS

REM ============================================================
REM  Auto-elevacao para Administrador
REM ============================================================
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Solicitando privilegios de administrador...
    powershell -NoProfile -Command "try { Start-Process -FilePath '%~f0' -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
    if errorlevel 1 (
        echo.
        echo [AVISO] Elevacao negada ou cancelada.
        echo Este script precisa de privilegios de administrador para funcionar.
        echo.
        pause
        exit /b 1
    )
    exit /b
)

REM Quando elevado via UAC, o CWD vira system32. Forcar pasta do bat:
cd /d "%~dp0"

REM ============================================================
REM  Setup do console: fonte Cascadia Mono, buffer p/ scroll, janela maior.
REM ============================================================
if exist "console_setup.ps1" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "console_setup.ps1" >nul 2>nul
)

cls
echo.
echo ============================================================
echo   Extrator TDN TOTVS - Inicializador (Administrador)
echo ============================================================
echo.

REM ---------- Verifica Python ----------
where py >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [ERRO] Python nao encontrado no PATH.
        echo Instale Python 3.12+ em https://www.python.org/downloads/
        echo e marque "Add Python to PATH" durante a instalacao.
        pause
        exit /b 1
    )
    set "PY_BOOT=python"
) else (
    set "PY_BOOT=py -3"
)

set "VENV_PY=.venv\Scripts\python.exe"

REM ---------- Detecta venv quebrado ou faltante ----------
set "RECREATE=0"
if not exist ".venv" set "RECREATE=1"
if not exist "%VENV_PY%" set "RECREATE=1"

if "%RECREATE%"=="1" (
    if exist ".venv" (
        echo [1/3] Detectado venv quebrado/incompleto. Recriando...
        rmdir /s /q ".venv"
    ) else (
        echo [1/3] Criando ambiente virtual em .venv ...
    )
    %PY_BOOT% -m venv .venv
    if errorlevel 1 (
        echo [ERRO] Falha ao criar o ambiente virtual.
        pause
        exit /b 1
    )
    if not exist "%VENV_PY%" (
        echo [ERRO] Venv criado mas python.exe nao foi encontrado em %VENV_PY%
        pause
        exit /b 1
    )
    echo       Venv criado com sucesso.
) else (
    echo [1/3] Ambiente virtual OK.
)

REM ---------- Instala dependencias (com cache via marker) ----------
set "DEPS_MARKER=.venv\.deps_ok"
set "DEPS_NEEDED=0"

if not exist "%DEPS_MARKER%" (
    set "DEPS_NEEDED=1"
) else (
    REM Se requirements.txt foi alterado depois do marker, reinstala
    for %%A in ("requirements.txt") do set "REQ_TIME=%%~tA"
    for %%A in ("%DEPS_MARKER%") do set "MARK_TIME=%%~tA"
    REM Comparacao via PowerShell (datas pt-BR sao chatas em puro bat)
    powershell -NoProfile -Command "exit ([int]((Get-Item 'requirements.txt').LastWriteTime -gt (Get-Item '%DEPS_MARKER%').LastWriteTime))"
    if !errorlevel! equ 1 set "DEPS_NEEDED=1"
)

if "%DEPS_NEEDED%"=="1" (
    echo [2/3] Instalando/atualizando dependencias...
    echo       ^(primeira vez pode demorar alguns minutos - baixa Playwright + libs^)
    echo.
    "%VENV_PY%" -m pip install --upgrade pip
    if errorlevel 1 (
        echo [ERRO] Falha ao atualizar pip.
        pause
        exit /b 1
    )
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERRO] Falha ao instalar dependencias.
        pause
        exit /b 1
    )
    echo. > "%DEPS_MARKER%"
    echo.
    echo       Dependencias instaladas.
) else (
    echo [2/3] Dependencias ja instaladas ^(use 'forcar_reinstalacao.bat' ou apague .venv\.deps_ok para reinstalar^).
)

REM ---------- Instala Chromium (detecta se ja existe) ----------
set "CHROME_FOUND=0"
if exist "%LOCALAPPDATA%\ms-playwright" (
    dir /b /a:d "%LOCALAPPDATA%\ms-playwright\chromium-*" >nul 2>nul
    if not errorlevel 1 set "CHROME_FOUND=1"
)

if "%CHROME_FOUND%"=="1" (
    echo [3/3] Chromium do Playwright ja instalado.
) else (
    echo [3/3] Baixando Chromium do Playwright...
    echo       ^(~150MB, apenas na primeira execucao^)
    echo.
    "%VENV_PY%" -m playwright install chromium
    if errorlevel 1 (
        echo [ERRO] Nao foi possivel instalar o Chromium.
        pause
        exit /b 1
    )
    echo       Chromium instalado.
)

echo.
echo ============================================================
echo   Ambiente pronto.
echo ============================================================
echo.

REM ============================================================
REM  Variaveis para a sessao (configuraveis no menu de velocidade)
REM ============================================================
set "SPEED_MODE=safe"
set "REQUEST_DELAY=2.0"
set "BACKOFF_INITIAL=30"
set "BACKOFF_MAX=900"
set "WORKERS=4"
set "TIMEOUT=180"
set "SLOW=60"

:MENU
REM Proxies: se existir proxies.txt (opcao [P]), todas as execucoes o utilizam.
set "PROXY_OPT="
set "PROXY_TXT=sem proxy"
if exist "proxies.txt" (
    set "PROXY_OPT=--proxy-file proxies.txt"
    set "PROXY_TXT=com proxy"
)
set "RUN_FLAGS=--timeout-seconds %TIMEOUT% --slow-threshold-seconds %SLOW% --headless --request-delay %REQUEST_DELAY% --backoff-initial %BACKOFF_INITIAL% --backoff-max %BACKOFF_MAX% --max-workers %WORKERS% %PROXY_OPT%"
echo.
echo ============================================================
echo   Menu Principal  ^(velocidade: %SPEED_MODE% ^| %REQUEST_DELAY%s/req ^| %WORKERS% worker^(s^) ^| %PROXY_TXT%^)
echo ============================================================
echo   [1] Continuar trabalho anterior  ^(retoma de onde parou^)
echo   [F] Reprocessar paginas que faltaram  ^(so as falhas, sem re-mapear^)
echo   [2] Iniciar novo trabalho  ^(rapido, so URL + pasta^)
echo   [3] Execucao avancada  ^(configura tudo^)
echo   [4] Verificar atualizacoes  ^(escolhe um job e busca paginas novas^)
echo   [5] Regenerar todos os PDFs  ^(escolhe um job e refaz tudo^)
echo   [6] Configurar velocidade  ^(rate limit, workers^)
echo   [P] Configurar proxy  ^(contorna o bloqueio 1015 por IP^)
echo   [7] Listar trabalhos  ^(jobs encontrados^)
echo   [8] Reinstalar dependencias Python
echo   [9] Reinstalar Chromium  ^(use se der erro de ICU/launch^)
echo   [0] Sair
echo ============================================================
set "OPC="
set /p "OPC=Escolha uma opcao [0-9, F, P]: "

if "%OPC%"=="1" goto RESUME
if /i "%OPC%"=="F" goto RETRY_FAILED
if "%OPC%"=="2" goto QUICK
if "%OPC%"=="3" goto ADVANCED
if "%OPC%"=="4" goto UPDATE_MODE
if "%OPC%"=="5" goto REGENERATE_MODE
if "%OPC%"=="6" goto SPEED_MENU
if /i "%OPC%"=="P" goto PROXY_MENU
if "%OPC%"=="7" goto LIST_JOBS
if "%OPC%"=="8" goto REINSTALL
if "%OPC%"=="9" goto REINSTALL_CHROME
if "%OPC%"=="0" goto END
echo Opcao invalida.
echo.
goto MENU

REM ============================================================
REM  [1] Continuar trabalho anterior
REM      Lista os jobs incompletos (em output\ e na pasta atual), pergunta
REM      qual retomar e le a URL do proprio job - nada de redigitar.
REM ============================================================
:RESUME
echo.
echo --- Continuar Trabalho Anterior ---
echo.
"%VENV_PY%" -m src.main resume --mode auto %RUN_FLAGS%
set "EXITCODE=%errorlevel%"
REM 4 = nenhum job / escolha cancelada: volta ao menu sem encerrar.
if "%EXITCODE%"=="4" goto MENU
call :REPORT_EXIT
goto AFTER_RUN

REM ============================================================
REM  [F] Reprocessar somente as paginas que faltaram
REM      Nao re-mapeia a arvore: tenta de novo so o que ficou sem PDF.
REM ============================================================
:RETRY_FAILED
echo.
echo --- Reprocessar Paginas que Faltaram ---
echo.
"%VENV_PY%" -m src.main resume --mode retry-failed %RUN_FLAGS%
set "EXITCODE=%errorlevel%"
REM 4 = nenhum job / escolha cancelada: volta ao menu sem encerrar.
if "%EXITCODE%"=="4" goto MENU
call :REPORT_EXIT
goto AFTER_RUN

REM ============================================================
REM  [2] Execucao rapida - novo trabalho
REM ============================================================
:QUICK
echo.
echo --- Iniciar Novo Trabalho ---
set "URL="
set /p "URL=URL inicial da documentacao TDN: "
if defined URL set "URL=%URL:"=%"
if "%URL%"=="" (
    echo [ERRO] URL obrigatoria.
    echo.
    goto MENU
)

REM Sugere um nome de pasta baseado na URL (ultimo segmento)
set "DEFAULT_DIR=output"
set "OUTDIR="
echo Sugestao: use uma subpasta por projeto, ex: output\MinhaArea
set /p "OUTDIR=Pasta de saida [%DEFAULT_DIR%]: "
if defined OUTDIR set "OUTDIR=%OUTDIR:"=%"
if "%OUTDIR%"=="" set "OUTDIR=%DEFAULT_DIR%"
call :NORMALIZE_OUTDIR

echo.
echo Executando com:
echo   URL          = %URL%
echo   Saida        = %OUTDIR%
echo   Consolidado  = TDN_TOTVS_consolidado.pdf
echo   Timeout      = %TIMEOUT%s/pagina  Slow = %SLOW%s
echo   Rate limit   = %REQUEST_DELAY%s/req  Workers = %WORKERS%  Rede = %PROXY_TXT%
echo   Backoff      = %BACKOFF_INITIAL%s -^> %BACKOFF_MAX%s (em bloqueio)
echo.
"%VENV_PY%" -m src.main run "%URL%" --output-dir "%OUTDIR%" %RUN_FLAGS%
set "EXITCODE=%errorlevel%"
call :REPORT_EXIT
goto AFTER_RUN

REM ============================================================
REM  [3] Execucao avancada
REM ============================================================
:ADVANCED
echo.
echo --- Execucao Avancada (Enter aceita o padrao mostrado entre colchetes) ---
echo.

set "URL="
set /p "URL=URL inicial da documentacao TDN: "
if defined URL set "URL=%URL:"=%"
if "%URL%"=="" (
    echo [ERRO] URL obrigatoria.
    echo.
    goto MENU
)

set "OUTDIR="
set /p "OUTDIR=Pasta de saida [output]: "
if defined OUTDIR set "OUTDIR=%OUTDIR:"=%"
if "%OUTDIR%"=="" set "OUTDIR=output"
call :NORMALIZE_OUTDIR

set "CONSOL="
set /p "CONSOL=Nome do PDF consolidado [TDN_TOTVS_consolidado.pdf]: "
if defined CONSOL set "CONSOL=%CONSOL:"=%"
if "%CONSOL%"=="" set "CONSOL=TDN_TOTVS_consolidado.pdf"

set "ADV_TIMEOUT=%TIMEOUT%"
set /p "ADV_TIMEOUT=Timeout por pagina em segundos [%TIMEOUT%]: "
if defined ADV_TIMEOUT set "ADV_TIMEOUT=%ADV_TIMEOUT:"=%"
if "%ADV_TIMEOUT%"=="" set "ADV_TIMEOUT=%TIMEOUT%"
echo %ADV_TIMEOUT%| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERRO] Timeout deve ser inteiro positivo. Recebido: %ADV_TIMEOUT%
    echo.
    goto MENU
)

set "ADV_SLOW=%SLOW%"
set /p "ADV_SLOW=Limite para marcar como 'lenta' em segundos [%SLOW%]: "
if defined ADV_SLOW set "ADV_SLOW=%ADV_SLOW:"=%"
if "%ADV_SLOW%"=="" set "ADV_SLOW=%SLOW%"
echo %ADV_SLOW%| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERRO] Slow threshold deve ser inteiro positivo. Recebido: %ADV_SLOW%
    echo.
    goto MENU
)

set "ADV_DELAY=%REQUEST_DELAY%"
set /p "ADV_DELAY=Delay entre requests em segundos [%REQUEST_DELAY%]: "
if defined ADV_DELAY set "ADV_DELAY=%ADV_DELAY:"=%"
if "%ADV_DELAY%"=="" set "ADV_DELAY=%REQUEST_DELAY%"

set "ADV_WORKERS=%WORKERS%"
set /p "ADV_WORKERS=Workers paralelos (1-8) [%WORKERS%]: "
if defined ADV_WORKERS set "ADV_WORKERS=%ADV_WORKERS:"=%"
if "%ADV_WORKERS%"=="" set "ADV_WORKERS=%WORKERS%"

set "HEADLESS_OPT=--headless"
set "HEADLESS_TXT=sim"
set "HEAD="
set /p "HEAD=Executar em modo headless (sem janela)? [S/n]: "
if /i "%HEAD%"=="n" (
    set "HEADLESS_OPT=--headed"
    set "HEADLESS_TXT=nao"
)

set "MAX="
set /p "MAX=Limite de paginas (vazio = sem limite): "
if defined MAX set "MAX=%MAX:"=%"
set "MAX_OPT="
if not "%MAX%"=="" (
    echo %MAX%| findstr /r "^[1-9][0-9]*$" >nul
    if errorlevel 1 (
        echo [ERRO] Max paginas deve ser inteiro positivo. Recebido: %MAX%
        echo.
        goto MENU
    )
    set "MAX_OPT=--max-pages %MAX%"
)

echo.
echo Executando com:
echo   URL          = %URL%
echo   Saida        = %OUTDIR%
echo   Consolidado  = %CONSOL%
echo   Timeout      = %ADV_TIMEOUT%s    Slow = %ADV_SLOW%s
echo   Rate         = %ADV_DELAY%s/req  Workers = %ADV_WORKERS%
echo   Headless     = %HEADLESS_TXT%
if "%MAX%"=="" (
    echo   Max paginas  = sem limite
) else (
    echo   Max paginas  = %MAX%
)
echo.

"%VENV_PY%" -m src.main run "%URL%" --output-dir "%OUTDIR%" --consolidated-name "%CONSOL%" --timeout-seconds %ADV_TIMEOUT% --slow-threshold-seconds %ADV_SLOW% %HEADLESS_OPT% %MAX_OPT% --request-delay %ADV_DELAY% --backoff-initial %BACKOFF_INITIAL% --backoff-max %BACKOFF_MAX% --max-workers %ADV_WORKERS% %PROXY_OPT%
set "EXITCODE=%errorlevel%"
call :REPORT_EXIT
goto AFTER_RUN

REM ============================================================
REM  [4] Verificar atualizacoes (--update)
REM      Escolhe um job EXISTENTE na lista (URL e pasta vem do job). Antes a
REM      pasta era digitada: um nome diferente do job criava um job novo e
REM      baixava tudo de novo.
REM ============================================================
:UPDATE_MODE
echo.
echo --- Verificar Atualizacoes da TDN ---
echo Re-mapeia a arvore do job escolhido procurando paginas novas.
echo PDFs ja gerados sao reaproveitados; so o que e novo ou faltou e baixado.
echo.
"%VENV_PY%" -m src.main resume --mode update %RUN_FLAGS%
set "EXITCODE=%errorlevel%"
REM 4 = nenhum job / escolha cancelada: volta ao menu sem encerrar.
if "%EXITCODE%"=="4" goto MENU
call :REPORT_EXIT
goto AFTER_RUN

REM ============================================================
REM  [5] Regenerar tudo (--regenerate)
REM ============================================================
:REGENERATE_MODE
echo.
echo --- Regenerar Todos os PDFs ---
echo Re-mapeia a arvore do job escolhido E regenera TODOS os PDFs.
echo Use isto quando suspeitar que paginas foram modificadas.
echo.
"%VENV_PY%" -m src.main resume --mode regenerate %RUN_FLAGS%
set "EXITCODE=%errorlevel%"
REM 4 = nenhum job / escolha cancelada: volta ao menu sem encerrar.
if "%EXITCODE%"=="4" goto MENU
call :REPORT_EXIT
goto AFTER_RUN

REM ============================================================
REM  [6] Menu de velocidade
REM ============================================================
:SPEED_MENU
echo.
echo --- Configurar Velocidade ---
echo Modo atual: %SPEED_MODE%  ^(delay=%REQUEST_DELAY%s ^| workers=%WORKERS% ^| timeout=%TIMEOUT%s^)
echo.
echo O intervalo e o PONTO DE PARTIDA: a cada bloqueio do servidor o extrator
echo desacelera sozinho e volta a acelerar aos poucos. Os workers geram PDFs
echo em paralelo; com varios proxies, cada saida tem navegador e espera proprios.
echo.
echo   [1] Seguro               ^(2s/saida, 4 workers, timeout 180s^) - RECOMENDADO
echo   [2] Medio                ^(1s/saida, 4 workers, timeout 150s^)
echo   [3] Paralelo com proxies ^(2s/saida, 8 workers, timeout 120s^)
echo   [4] Personalizado
echo   [0] Voltar
echo.
set "SP="
set /p "SP=Escolha [0-4]: "
if "%SP%"=="1" (
    set "SPEED_MODE=safe"
    set "REQUEST_DELAY=2.0"
    set "BACKOFF_INITIAL=30"
    set "BACKOFF_MAX=900"
    set "WORKERS=4"
    set "TIMEOUT=180"
    set "SLOW=60"
    echo Modo seguro ativado.
    goto MENU
)
if "%SP%"=="2" (
    set "SPEED_MODE=medio"
    set "REQUEST_DELAY=1.0"
    set "BACKOFF_INITIAL=60"
    set "BACKOFF_MAX=900"
    set "WORKERS=4"
    set "TIMEOUT=150"
    set "SLOW=60"
    echo Modo medio ativado.
    goto MENU
)
if "%SP%"=="3" (
    set "SPEED_MODE=paralelo"
    set "REQUEST_DELAY=2.0"
    set "BACKOFF_INITIAL=120"
    set "BACKOFF_MAX=1800"
    set "WORKERS=8"
    set "TIMEOUT=120"
    set "SLOW=60"
    echo Modo paralelo ativado: ate 8 saidas simultaneas, preservando o intervalo por IP.
    goto MENU
)
if "%SP%"=="4" goto SPEED_CUSTOM
if "%SP%"=="0" goto MENU
echo Opcao invalida.
goto SPEED_MENU

:SPEED_CUSTOM
set "SPEED_MODE=custom"
set "VAL="
set /p "VAL=Delay entre requests em segundos [%REQUEST_DELAY%]: "
if defined VAL if not "%VAL%"=="" set "REQUEST_DELAY=%VAL%"
set "VAL="
set /p "VAL=Workers paralelos (1-8) [%WORKERS%]: "
if defined VAL if not "%VAL%"=="" set "WORKERS=%VAL%"
set "VAL="
set /p "VAL=Timeout por pagina em segundos [%TIMEOUT%]: "
if defined VAL if not "%VAL%"=="" set "TIMEOUT=%VAL%"
set "VAL="
set /p "VAL=Backoff inicial em segundos (apos bloqueio) [%BACKOFF_INITIAL%]: "
if defined VAL if not "%VAL%"=="" set "BACKOFF_INITIAL=%VAL%"
set "VAL="
set /p "VAL=Backoff maximo em segundos [%BACKOFF_MAX%]: "
if defined VAL if not "%VAL%"=="" set "BACKOFF_MAX=%VAL%"
echo Configuracao personalizada salva.
goto MENU

REM ============================================================
REM  [7] Listar jobs (em output\ e na pasta atual)
REM ============================================================
:LIST_JOBS
echo.
"%VENV_PY%" -m src.main jobs --all
echo.
pause
goto MENU

REM ============================================================
REM  [P] Proxies (arquivo proxies.txt, usado por todas as execucoes)
REM ============================================================
:PROXY_MENU
echo.
echo --- Configurar Proxy ---
echo O TDN limita os acessos POR IP (Cloudflare erro 1015). Com um proxy os
echo requests saem por outro IP; com varios e workers paralelos, usa varias
echo saidas ao mesmo tempo, com trocas e esperas independentes.
echo Formatos: http://usuario:senha@host:porta  ^|  host:porta  ^|  socks5://host:porta
echo.
"%VENV_PY%" -m src.main proxy list
echo.
echo   [1] Adicionar proxy
echo   [2] Testar  ^(1 request por saida de rede^)
echo   [3] Remover todos  ^(volta para a conexao direta^)
echo   [0] Voltar
echo.
set "PX="
set /p "PX=Escolha [0-3]: "
if "%PX%"=="1" goto PROXY_ADD
if "%PX%"=="2" goto PROXY_TEST
if "%PX%"=="3" goto PROXY_CLEAR
if "%PX%"=="0" goto MENU
echo Opcao invalida.
goto PROXY_MENU

:PROXY_ADD
echo.
"%VENV_PY%" -m src.main proxy add
goto PROXY_MENU

:PROXY_TEST
echo.
"%VENV_PY%" -m src.main proxy test
echo.
pause
goto PROXY_MENU

:PROXY_CLEAR
echo.
"%VENV_PY%" -m src.main proxy clear
goto PROXY_MENU

REM ============================================================
REM  [8] Reinstalar dependencias
REM ============================================================
:REINSTALL
echo.
echo --- Forcando reinstalacao das dependencias Python ---
if exist "%DEPS_MARKER%" del "%DEPS_MARKER%"
"%VENV_PY%" -m pip install --upgrade pip
"%VENV_PY%" -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
    echo [ERRO] Falha na reinstalacao.
    pause
    goto MENU
)
echo. > "%DEPS_MARKER%"
echo.
echo Dependencias reinstaladas.
echo.
goto MENU

REM ============================================================
REM  [9] Reinstalar Chromium
REM ============================================================
:REINSTALL_CHROME
echo.
echo --- Forcando download limpo do Chromium ---
echo Isso baixa o Chromium novamente (~150MB). Pode demorar.
echo.
"%VENV_PY%" -m playwright install --force chromium
if errorlevel 1 (
    echo [ERRO] Falha ao reinstalar o Chromium.
    pause
    goto MENU
)
echo.
echo Chromium reinstalado com sucesso.
echo.
goto MENU

REM ============================================================
REM  Sub-rotinas
REM ============================================================

REM Nome simples (sem \ / :) vira subpasta de output\. Evita espalhar jobs pela
REM pasta do projeto: "CFGTRIB" e "output\CFGTRIB" seriam jobs diferentes.
:NORMALIZE_OUTDIR
if /i "%OUTDIR%"=="output" goto :eof
set "_T=%OUTDIR%"
if not "%_T:\=%"=="%_T%" goto :eof
if not "%_T:/=%"=="%_T%" goto :eof
if not "%_T::=%"=="%_T%" goto :eof
set "OUTDIR=output\%OUTDIR%"
echo [INFO] Pasta de saida ajustada para %OUTDIR%
goto :eof

REM Explica o codigo de saida da ultima execucao.
REM   0 = completo ^| 3 = documentacao incompleta ^(progresso salvo^) ^| 4 = nada a fazer ^| 130 = Ctrl+C
:REPORT_EXIT
if "%EXITCODE%"=="0" goto :eof
if "%EXITCODE%"=="4" goto :eof
if "%EXITCODE%"=="130" goto :eof
echo.
if "%EXITCODE%"=="3" goto REPORT_INCOMPLETE
echo [ERRO] A execucao terminou com codigo %EXITCODE%. Veja a mensagem acima.
pause
goto :eof

:REPORT_INCOMPLETE
echo [ATENCAO] A documentacao ficou INCOMPLETA - veja o quadro acima.
echo           O progresso esta salvo. O quadro indica a opcao do menu que
echo           conclui o que faltou, sem refazer o que ja esta pronto.
pause
goto :eof

:AFTER_RUN
set "AGAIN="
echo.
set /p "AGAIN=Executar novamente? [s/N]: "
if /i "%AGAIN%"=="s" (
    echo.
    goto MENU
)

:END
echo.
echo Encerrando.
pause
endlocal
exit /b 0
