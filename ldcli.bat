@echo off
rem ldcli launcher - put this folder on PATH to use `ldcli` anywhere.
setlocal
set "DIR=%~dp0"
python "%DIR%ldcli.py" %*
exit /b %ERRORLEVEL%
