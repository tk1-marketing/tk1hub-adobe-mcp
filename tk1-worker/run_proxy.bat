@echo off
REM Inicia o proxy WebSocket do adb-mcp
REM Execute em um terminal separado ANTES do worker

cd /d "%~dp0..\adb-proxy-socket"
echo Iniciando proxy adb-mcp em ws://localhost:3001 ...
node proxy.js
