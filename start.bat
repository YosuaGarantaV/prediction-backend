@echo off
REM ============================================================
REM  Saham IDX Prediction Engine - launcher sekali klik
REM ============================================================
cd /d "%~dp0"
echo Menjalankan Saham IDX Prediction Engine...
echo Dashboard akan terbuka di http://localhost:8800
start "" http://localhost:8800
REM serve.py = supervisor auto-restart (pulih sendiri dari crash curl_cffi exit-4)
".venv\Scripts\python.exe" serve.py
echo.
echo Server berhenti. Tekan tombol apa saja untuk menutup.
pause >nul
