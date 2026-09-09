@echo off
cd /d "C:\Users\LENOVO\Documents\Prediction"
".venv\Scripts\python.exe" "tools\push_idxflow.py" >> "data\logs\idxflow_push.log" 2>&1
