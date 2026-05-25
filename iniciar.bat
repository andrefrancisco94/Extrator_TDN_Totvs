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

:MENU
echo ============================================================
echo   Menu Principal
echo ============================================================
echo   [1] Execucao rapida    (so URL, resume automatico)
echo   [2] Execucao avancada  (configura todas as opcoes)
echo   [3] Verificar atualizacoes da TDN (--update: busca paginas novas)
echo   [4] Regenerar todos os PDFs (--regenerate: refaz tudo)
echo   [5] Reinstalar dependencias Python
echo   [6] Reinstalar Chromium (use se der erro de ICU/launch)
echo   [7] Sair
echo ============================================================
set "OPC="
set /p "OPC=Escolha uma opcao [1-7]: "

if "%OPC%"=="1" goto QUICK
if "%OPC%"=="2" goto ADVANCED
if "%OPC%"=="3" goto UPDATE_MODE
if "%OPC%"=="4" goto REGENERATE_MODE
if "%OPC%"=="5" goto REINSTALL
if "%OPC%"=="6" goto REINSTALL_CHROME
if "%OPC%"=="7" goto END
echo Opcao invalida.
echo.
goto MENU

REM ---------- Execucao rapida ----------
:QUICK
echo.
echo --- Execucao Rapida ---
set "URL="
set /p "URL=URL inicial da documentacao TDN: "
if defined URL set "URL=%URL:"=%"
if "%URL%"=="" (
    echo [ERRO] URL obrigatoria.
    echo.
    goto MENU
)
echo.
echo Executando com:
echo   URL          = %URL%
echo   Saida        = output
echo   Consolidado  = TDN_TOTVS_consolidado.pdf
echo   Timeout      = 120s (2min por pagina)
echo   Slow log     = paginas que demorarem 60s ou mais
echo   Headless     = sim
echo.
"%VENV_PY%" -m src.main run "%URL%" --output-dir output --timeout-seconds 120 --slow-threshold-seconds 60 --headless
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [ERRO] A execucao terminou com codigo %EXITCODE%. Veja a mensagem acima.
    pause
)
goto AFTER_RUN

REM ---------- Execucao avancada ----------
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

set "OUTDIR=output"
set /p "OUTDIR=Pasta de saida [output]: "
if defined OUTDIR set "OUTDIR=%OUTDIR:"=%"
if "%OUTDIR%"=="" set "OUTDIR=output"

set "CONSOL=TDN_TOTVS_consolidado.pdf"
set /p "CONSOL=Nome do PDF consolidado [TDN_TOTVS_consolidado.pdf]: "
if defined CONSOL set "CONSOL=%CONSOL:"=%"
if "%CONSOL%"=="" set "CONSOL=TDN_TOTVS_consolidado.pdf"

set "TIMEOUT=120"
set /p "TIMEOUT=Timeout por pagina em segundos [120]: "
if defined TIMEOUT set "TIMEOUT=%TIMEOUT:"=%"
if "%TIMEOUT%"=="" set "TIMEOUT=120"
echo %TIMEOUT%| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERRO] Timeout deve ser inteiro positivo. Recebido: %TIMEOUT%
    echo.
    goto MENU
)

set "SLOW=60"
set /p "SLOW=Limite para marcar como 'lenta' em segundos [60]: "
if defined SLOW set "SLOW=%SLOW:"=%"
if "%SLOW%"=="" set "SLOW=60"
echo %SLOW%| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERRO] Slow threshold deve ser inteiro positivo. Recebido: %SLOW%
    echo.
    goto MENU
)

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
echo   Timeout      = %TIMEOUT%s
echo   Slow log     = %SLOW%s
echo   Headless     = %HEADLESS_TXT%
if "%MAX%"=="" (
    echo   Max paginas  = sem limite
) else (
    echo   Max paginas  = %MAX%
)
echo.

"%VENV_PY%" -m src.main run "%URL%" --output-dir "%OUTDIR%" --consolidated-name "%CONSOL%" --timeout-seconds %TIMEOUT% --slow-threshold-seconds %SLOW% %HEADLESS_OPT% %MAX_OPT%
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [ERRO] A execucao terminou com codigo %EXITCODE%. Veja a mensagem acima.
    pause
)
goto AFTER_RUN

REM ---------- Verificar atualizacoes (--update) ----------
:UPDATE_MODE
echo.
echo --- Verificar Atualizacoes da TDN ---
echo Re-mapeia a arvore procurando paginas novas.
echo PDFs ja gerados em runs anteriores serao reaproveitados.
echo.
set "URL="
set /p "URL=URL inicial da documentacao TDN: "
if defined URL set "URL=%URL:"=%"
if "%URL%"=="" (
    echo [ERRO] URL obrigatoria.
    echo.
    goto MENU
)
set "OUTDIR=output"
set /p "OUTDIR=Pasta de saida [output]: "
if defined OUTDIR set "OUTDIR=%OUTDIR:"=%"
if "%OUTDIR%"=="" set "OUTDIR=output"
echo.
echo Executando em modo UPDATE...
echo.
"%VENV_PY%" -m src.main run "%URL%" --output-dir "%OUTDIR%" --timeout-seconds 120 --slow-threshold-seconds 60 --headless --update --yes
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [ERRO] A execucao terminou com codigo %EXITCODE%. Veja a mensagem acima.
    pause
)
goto AFTER_RUN

REM ---------- Regenerar tudo (--regenerate) ----------
:REGENERATE_MODE
echo.
echo --- Regenerar Todos os PDFs ---
echo Re-mapeia a arvore E regenera TODOS os PDFs.
echo Use isto quando suspeitar que paginas foram modificadas.
echo Mantem o manifest.json para historico.
echo.
set "URL="
set /p "URL=URL inicial da documentacao TDN: "
if defined URL set "URL=%URL:"=%"
if "%URL%"=="" (
    echo [ERRO] URL obrigatoria.
    echo.
    goto MENU
)
set "OUTDIR=output"
set /p "OUTDIR=Pasta de saida [output]: "
if defined OUTDIR set "OUTDIR=%OUTDIR:"=%"
if "%OUTDIR%"=="" set "OUTDIR=output"
echo.
echo Executando em modo REGENERATE (pode demorar - refaz todos os PDFs)...
echo.
"%VENV_PY%" -m src.main run "%URL%" --output-dir "%OUTDIR%" --timeout-seconds 120 --slow-threshold-seconds 60 --headless --regenerate --yes
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [ERRO] A execucao terminou com codigo %EXITCODE%. Veja a mensagem acima.
    pause
)
goto AFTER_RUN

REM ---------- Reinstalar dependencias ----------
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

REM ---------- Reinstalar Chromium (corrige corrupcao) ----------
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
