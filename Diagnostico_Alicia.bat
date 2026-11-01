@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Mesmos criterios do Iniciar_Alicia.bat (alicia_core\envcheck.py): verifica, mostra o resultado e oferece corrigir.
call Iniciar_Alicia.bat --check
echo.
pause
