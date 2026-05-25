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
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

REM Quando elevado via UAC, o CWD vira system32. Forcar pasta do bat:
cd /d "%~dp0"

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
echo   [1] Execucao rapida   (so URL, demais com padroes)
echo   [2] Execucao avancada (configura todas as opcoes)
echo   [3] Reinstalar dependencias (forcar refresh)
echo   [4] Sair
echo ============================================================
set "OPC="
set /p "OPC=Escolha uma opcao [1-4]: "

if "%OPC%"=="1" goto QUICK
if "%OPC%"=="2" goto ADVANCED
if "%OPC%"=="3" goto REINSTALL
if "%OPC%"=="4" goto END
echo Opcao invalida.
echo.
goto MENU

REM ---------- Execucao rapida ----------
:QUICK
echo.
echo --- Execucao Rapida ---
set "URL="
set /p "URL=URL inicial da documentacao TDN: "
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
echo   Timeout      = 60s
echo   Headless     = sim
echo.
"%VENV_PY%" -m src.main run "%URL%" --output-dir output --timeout-seconds 60 --headless
goto AFTER_RUN

REM ---------- Execucao avancada ----------
:ADVANCED
echo.
echo --- Execucao Avancada (Enter aceita o padrao mostrado entre colchetes) ---
echo.

set "URL="
set /p "URL=URL inicial da documentacao TDN: "
if "%URL%"=="" (
    echo [ERRO] URL obrigatoria.
    echo.
    goto MENU
)

set "OUTDIR=output"
set /p "OUTDIR=Pasta de saida [output]: "
if "%OUTDIR%"=="" set "OUTDIR=output"

set "CONSOL=TDN_TOTVS_consolidado.pdf"
set /p "CONSOL=Nome do PDF consolidado [TDN_TOTVS_consolidado.pdf]: "
if "%CONSOL%"=="" set "CONSOL=TDN_TOTVS_consolidado.pdf"

set "TIMEOUT=60"
set /p "TIMEOUT=Timeout por pagina em segundos [60]: "
if "%TIMEOUT%"=="" set "TIMEOUT=60"

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
set "MAX_OPT="
if not "%MAX%"=="" set "MAX_OPT=--max-pages %MAX%"

echo.
echo Executando com:
echo   URL          = %URL%
echo   Saida        = %OUTDIR%
echo   Consolidado  = %CONSOL%
echo   Timeout      = %TIMEOUT%s
echo   Headless     = %HEADLESS_TXT%
if "%MAX%"=="" (
    echo   Max paginas  = sem limite
) else (
    echo   Max paginas  = %MAX%
)
echo.

"%VENV_PY%" -m src.main run "%URL%" --output-dir "%OUTDIR%" --consolidated-name "%CONSOL%" --timeout-seconds %TIMEOUT% %HEADLESS_OPT% %MAX_OPT%
goto AFTER_RUN

REM ---------- Reinstalar dependencias ----------
:REINSTALL
echo.
echo --- Forcando reinstalacao das dependencias ---
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
