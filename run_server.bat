@echo off
REM Activate the Conda environment named "Flask"
echo Activating Conda environment: Flask...
call conda activate Flask

REM Check if activation was successful (optional, basic check)
if "%CONDA_DEFAULT_ENV%" NEQ "Flask" (
    echo Failed to activate Conda environment "Flask". Make sure Conda is installed and the environment exists.
    goto end
)

REM Run the server script
echo Starting server (app.py)...
python app.py

:end
pause
