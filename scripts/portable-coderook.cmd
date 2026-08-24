@echo off
set "CODEROOK_PORTABLE_ROOT=%~dp0"
set "PATH=%CODEROOK_PORTABLE_ROOT%runtime;%CODEROOK_PORTABLE_ROOT%runtime\Scripts;%PATH%"
"%CODEROOK_PORTABLE_ROOT%runtime\python.exe" -c "from code_rook.cli.main import main; raise SystemExit(main())" %*
