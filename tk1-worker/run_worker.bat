@echo off
REM TK1 Adobe Media Worker — inicialização Windows
REM Execute como: .\run_worker.bat

cd /d "%~dp0"

echo Instalando dependências...
pip install -r requirements.txt -q

echo.
echo Iniciando TK1 Adobe Media Worker...
echo Certifique-se de que:
echo   1. node proxy.js está rodando em adb-proxy-socket\
echo   2. Photoshop está aberto com o plugin MCP conectado
echo   3. Illustrator está aberto com Window ^> Extensions ^> Illustrator MCP Agent
echo.

python adobe_media_worker.py
