@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title Alicia
set "PYTHONIOENCODING=utf-8"

rem ---------------------------------------------------------------------------
rem Iniciar_Alicia.bat
rem   1. Confere se o Windows e de 64 bits.
rem   2. Procura um Python 3.10+ de 64 bits (prefere 3.12, 3.11, 3.10).
rem   3. Se NAO houver Python, EXPLICA e PEDE AUTORIZACAO antes de baixar/instalar.
rem   4. Entrega o resto para o launcher.py (verifica bibliotecas, pergunta, instala, repara e abre a Alicia).
rem ---------------------------------------------------------------------------

if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 (
    echo.
    echo [ERRO] Este Windows e de 32 bits. A Alicia precisa do Windows 10 ou 11 de 64 bits.
    echo.
    pause
    exit /b 1
)

call :find_python
if defined PY goto :run_launcher

echo.
echo Nao encontrei o Python 3.10 ou mais novo ^(64 bits^) neste computador.
echo.
echo   O que e: o Python e o programa que executa a Alicia. E gratuito.
echo   Por que preciso: sem ele a Alicia nao consegue abrir.
echo.
echo Se voce autorizar, eu faco somente isto:
echo   1. Baixo o instalador OFICIAL do Python 3.12 em python.org ^(cerca de 25 MB^).
echo   2. Confiro a assinatura digital da Python Software Foundation.
echo   3. Abro o instalador com uma barra de progresso VISIVEL ^(nada silencioso^).
echo   4. Instalo so para o seu usuario: NAO pede senha de administrador.
echo   5. NAO altero o PATH do Windows. O instalador do Python registra o programa
echo      para o seu usuario, como qualquer outro programa.
echo.
choice /C SN /N /M "Posso baixar e instalar o Python agora? [S/N] "
if errorlevel 2 (
    echo.
    echo Tudo bem, nada foi baixado nem instalado.
    echo Para instalar voce mesmo: https://www.python.org/downloads/windows/
    echo Depois de instalar, de dois cliques em Iniciar_Alicia.bat de novo.
    echo.
    pause
    exit /b 1
)

set "PYVER=3.12.8"
set "PYURL=https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-amd64.exe"
set "PYEXE=%TEMP%\python-%PYVER%-amd64.exe"
if exist "%PYEXE%" del /q "%PYEXE%" >nul 2>nul

echo.
echo Baixando o instalador oficial do Python %PYVER% ...
powershell -NoProfile -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -UseBasicParsing -Uri '%PYURL%' -OutFile '%PYEXE%' } catch { exit 1 }"
if errorlevel 1 goto :download_failed
if not exist "%PYEXE%" goto :download_failed

echo Conferindo a assinatura digital do instalador ...
powershell -NoProfile -Command "$s = Get-AuthenticodeSignature -FilePath '%PYEXE%'; if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -like '*Python Software Foundation*') { exit 0 } else { exit 1 }"
if errorlevel 1 (
    del /q "%PYEXE%" >nul 2>nul
    echo.
    echo [ERRO] Nao consegui confirmar a assinatura digital do arquivo baixado. Por seguranca, NAO instalei nada.
    echo Baixe o Python manualmente em https://www.python.org/downloads/windows/ e rode este arquivo de novo.
    echo.
    pause
    exit /b 1
)

echo Abrindo o instalador do Python. Acompanhe a barra de progresso; pode levar 1 a 2 minutos.
"%PYEXE%" /passive InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_pip=1 Include_test=0 Shortcuts=0
del /q "%PYEXE%" >nul 2>nul

call :find_python
if not defined PY (
    echo.
    echo [ERRO] O Python nao ficou disponivel depois da instalacao.
    echo  1. Baixe o Python 3.12 de 64 bits em https://www.python.org/downloads/windows/
    echo  2. Instale e depois de dois cliques em Iniciar_Alicia.bat de novo.
    echo.
    pause
    exit /b 1
)
echo Python instalado.
goto :run_launcher

:download_failed
echo.
echo [ERRO] Nao consegui baixar o Python. Confira sua conexao com a internet e tente de novo.
echo Se preferir, baixe manualmente em https://www.python.org/downloads/windows/ ^(Windows installer 64-bit^).
echo.
pause
exit /b 1

:run_launcher
%PY% "%~dp0launcher.py" %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo O iniciador terminou com o codigo %CODE%.
    echo Detalhes: logs\instalacao_alicia.log e logs\alicia_system.log
    pause
)
exit /b %CODE%

rem ---------------------------------------------------------------------------
rem Procura um Python 3.10+ de 64 bits. Define PY (vazio se nao achar).
rem ---------------------------------------------------------------------------
:find_python
set "PY="
set "PYCHECK=import sys,struct; sys.exit(0 if sys.version_info[:2]>=(3,10) and struct.calcsize('P')==8 else 1)"

where py >nul 2>nul
if not errorlevel 1 (
    for %%V in (3.12 3.11 3.10) do (
        py -%%V -c "%PYCHECK%" >nul 2>nul
        if not errorlevel 1 (
            set "PY=py -%%V"
            goto :eof
        )
    )
)

where python >nul 2>nul
if not errorlevel 1 (
    python -c "%PYCHECK%" >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
        goto :eof
    )
)

rem Instalacoes comuns que nao estao no PATH ^(inclusive recem-instalado: o PATH desta janela nao enxerga^)
for %%V in (312 311 310 313) do (
    if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" (
        "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe" -c "%PYCHECK%" >nul 2>nul
        if not errorlevel 1 (
            set PY="%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
            goto :eof
        )
    )
    if exist "%ProgramFiles%\Python%%V\python.exe" (
        "%ProgramFiles%\Python%%V\python.exe" -c "%PYCHECK%" >nul 2>nul
        if not errorlevel 1 (
            set PY="%ProgramFiles%\Python%%V\python.exe"
            goto :eof
        )
    )
)

rem Ultimo recurso: qualquer Python 3 do launcher que atenda
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "%PYCHECK%" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
        goto :eof
    )
)
goto :eof
