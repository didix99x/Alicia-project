@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0"

set "REPO_URL=https://github.com/didix99x/Alicia-project.git"
set "PRIMEIRA_VEZ="

echo ==============================================
echo   Atualizar o GitHub da Alicia
echo   %REPO_URL%
echo ==============================================
echo.

where git >nul 2>nul
if errorlevel 1 goto :sem_git

REM ---------- 1) repositorio local ----------
if exist ".git" goto :remoto
echo Esta pasta ainda nao e um repositorio Git. Configurando...
git init >nul
if errorlevel 1 goto :erro
git symbolic-ref HEAD refs/heads/main
set "PRIMEIRA_VEZ=1"

:remoto
git remote get-url origin >nul 2>nul
if errorlevel 1 (
    git remote add origin "%REPO_URL%"
) else (
    for /f "delims=" %%u in ('git remote get-url origin') do echo Remoto atual: %%u
)

REM ---------- 2) .gitignore: segredos e dados pessoais nunca sobem ----------
if not exist ".gitignore" (
    echo Criando .gitignore de seguranca...
    > ".gitignore" echo .env
    >> ".gitignore" echo *.env
    >> ".gitignore" echo config.json
    >> ".gitignore" echo alicia_memory.db
    >> ".gitignore" echo alicia_memory.db-wal
    >> ".gitignore" echo alicia_memory.db-shm
    >> ".gitignore" echo gamer_profiles.json
    >> ".gitignore" echo logs/
    >> ".gitignore" echo backups/
    >> ".gitignore" echo exportacoes/
    >> ".gitignore" echo .venv/
    >> ".gitignore" echo __pycache__/
    >> ".gitignore" echo *.pyc
)
REM se algum desses ja estava versionado, sai do Git (o arquivo continua na sua pasta)
git rm -r --cached -q --ignore-unmatch .env config.json alicia_memory.db alicia_memory.db-wal alicia_memory.db-shm gamer_profiles.json logs backups exportacoes >nul 2>nul

REM ---------- 3) identidade do autor ----------
set "GN="
for /f "delims=" %%a in ('git config user.name') do set "GN=%%a"
if not defined GN (
    set /p "GN=Seu nome para os commits: "
    git config user.name "%GN%"
)
set "GE="
for /f "delims=" %%a in ('git config user.email') do set "GE=%%a"
if not defined GE (
    set /p "GE=Seu e-mail do GitHub: "
    git config user.email "%GE%"
)

REM ---------- 4) branch e primeira sincronizacao ----------
set "BRANCH="
for /f "delims=" %%b in ('git branch --show-current') do set "BRANCH=%%b"
if not defined BRANCH set "BRANCH=main"

echo.
echo Buscando o que ja existe no GitHub...
git fetch origin
if errorlevel 1 goto :erro_rede

git rev-parse --verify -q HEAD >nul 2>nul
if errorlevel 1 (
    git rev-parse --verify -q origin/%BRANCH% >nul 2>nul
    if not errorlevel 1 (
        echo Ligando esta pasta ao historico do GitHub, sem mexer nos seus arquivos...
        git reset -q origin/%BRANCH%
        set "PRIMEIRA_VEZ=1"
    )
)

REM ---------- 5) o que vai subir ----------
git add -A
echo.
echo Alteracoes que serao enviadas:
git status --short
echo.

set "APAGADOS="
for /f "delims=" %%d in ('git diff --cached --name-only --diff-filter=D') do set "APAGADOS=1"
if defined APAGADOS (
    echo [ATENCAO] Estes arquivos serao APAGADOS do GitHub, pois nao existem nesta pasta:
    git diff --cached --name-only --diff-filter=D
    echo.
    choice /c SN /n /m "Continuar mesmo assim? [S/N] "
    if errorlevel 2 (
        git reset -q
        echo Cancelado. Nada foi enviado.
        goto :fim
    )
)

REM ---------- 6) commit ----------
git diff --cached --quiet
if not errorlevel 1 (
    echo Nada novo para commitar.
    goto :enviar
)

for /f "delims=" %%t in ('powershell -NoProfile -Command "Get-Date -Format \"yyyy-MM-dd HH:mm\""') do set "AGORA=%%t"
set "MSG="
set /p "MSG=Mensagem do commit [Enter = automatica]: "
if not defined MSG set "MSG=Atualizacao %AGORA%"
set "MSG=%MSG:"=%"
git commit -q -m "%MSG%"
if errorlevel 1 goto :erro

REM ---------- 7) trazer novidades do GitHub e enviar ----------
:enviar
git ls-remote --exit-code --heads origin %BRANCH% >nul 2>nul
if not errorlevel 1 (
    echo.
    echo Juntando com as novidades do GitHub...
    git pull --rebase --autostash -q origin %BRANCH%
    if errorlevel 1 goto :conflito
)

echo.
echo Enviando para o GitHub...
git push -u origin %BRANCH%
if errorlevel 1 goto :erro_push

echo.
echo ==============================================
echo   Pronto! GitHub atualizado.
echo ==============================================
git log -1 --pretty=format:"Ultimo commit: %%h - %%s"
echo.
goto :fim

:sem_git
echo [ERRO] O Git nao esta instalado, ou nao esta no PATH.
echo Baixe em https://git-scm.com/download/win e rode este arquivo de novo.
goto :fim

:erro_rede
echo.
echo [ERRO] Nao consegui falar com o GitHub. Veja a internet e, se pedir login,
echo entre com sua conta do GitHub ^(ou um token de acesso pessoal^).
goto :fim

:conflito
echo.
echo [ERRO] Houve conflito ao juntar com o GitHub. Nada foi enviado.
echo Abra os arquivos marcados, resolva, depois rode: git add -A ^&^& git rebase --continue
echo Para desistir da juncao: git rebase --abort
goto :fim

:erro_push
echo.
echo [ERRO] O envio falhou. Causas comuns: login do GitHub nao feito ^(use token de acesso pessoal
echo no lugar da senha^), sem permissao no repositorio, ou o GitHub tem commits novos.
echo Rode este arquivo de novo; ele junta as novidades antes de enviar.
goto :fim

:erro
echo.
echo [ERRO] Algo deu errado. Veja a mensagem acima.

:fim
echo.
pause
endlocal
