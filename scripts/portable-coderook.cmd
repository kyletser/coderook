@echo off
set "CODEROOK_PORTABLE_ROOT=%~dp0"
"%CODEROOK_PORTABLE_ROOT%runtime\python.exe" -m code_rook.cli.main %*
