@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Alicia

call :find_python

if not defined PY (
    echo.
    echo Python nao encontrado. Vou instalar o Python 3.12 automaticamente.
    echo.
    where winget >nul 2>nul
    if not errorlevel 1 (
        echo [1/2] Tentando pelo winget...
        winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        call :find_python
    )
)

if not defined PY (
    echo [2/2] Baixando o instalador oficial em python.org ^(cerca de 25 MB^)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile '%TEMP%\python-3.12.8-amd64.exe' } catch { exit 1 }"
    if exist "%TEMP%\python-3.12.8-amd64.exe" (
        echo Instalando o Python ^(sem janelas, pode levar 1 a 2 minutos^)...
        "%TEMP%\python-3.12.8-amd64.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 Include_test=0
        call :find_python
    )
)

if not defined PY (
    echo.
    echo [ERRO] Nao consegui instalar o Python automaticamente.
    echo  1. Baixe o Python 3.12 em https://www.python.org/downloads/windows/
    echo  2. No instalador, MARQUE "Add python.exe to PATH".
    echo  3. Depois de instalar, de dois cliques em Iniciar_Alicia.bat de novo.
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0launcher.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo A Alicia terminou com o codigo %CODE%. Detalhes em: logs\alicia_system.log
    pause
)
exit /b %CODE%

:find_python
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
        goto :eof
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; sys.exit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
        goto :eof
    )
)
rem Recem-instalado: o PATH desta janela ainda nao enxerga o Python novo
for %%V in (312 313 311 310) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
        goto :eof
    )
)
goto :eof
