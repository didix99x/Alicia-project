@echo off
cd /d "%~dp0"

echo [1/3] Verificando se o Ollama esta rodando...

:: Verifica se o processo ollama.exe esta ativo na lista de tarefas
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if "%ERRORLEVEL%"=="0" (
    echo [OK] O Ollama ja esta em execucao!
) else (
    echo [!] O Ollama nao esta rodando. Iniciando o Ollama...
    
    :: Inicia o Ollama em uma nova janela oculta ou segundo plano
    start /b ollama serve >nul 2>&1
    
    echo Aguardando o Ollama inicializar...
    timeout /t 5 /nobreak >nul
)

echo [2/3] Verificando modelo llama3.2...
:: Opcional: garante que o modelo esta pronto/carregado
ollama run llama3.2 "oi" >nul 2>&1

echo [3/3] Iniciando a Alicia Desktop Pet...
echo --------------------------------------------------

:: Executa o script principal da assistente
python assistente.py

pause