@echo off
setlocal
cd /d "%~dp0"
if not exist "toolstock.json" (
  echo First run configure.ps1 to set up the PostgreSQL connection.
  pause
  exit /b 1
)
toolstock.exe --config "%~dp0toolstock.json"
if errorlevel 1 pause
