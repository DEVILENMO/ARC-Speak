@echo off
REM Batch file to activate virtual environment and run the Flet client

REM --- Configuration ---
REM Set this to the name of your virtual environment folder
SET VENV_DIR=venv
REM For example, if your venv folder is named 'flask', set VENV_DIR=flask

REM --- Script --- 
REM Get the directory of this batch file (project root)
SET "SCRIPT_DIR=%~dp0"

REM Path to the activate script
SET "ACTIVATE_SCRIPT=%SCRIPT_DIR%%VENV_DIR%\Scripts\activate.bat"

REM Path to the client script
SET "CLIENT_SCRIPT=%SCRIPT_DIR%flet_client.py"

REM Check if virtual environment activate script exists
IF NOT EXIST "%ACTIVATE_SCRIPT%" (
    echo ERROR: Virtual environment activation script not found at "%ACTIVATE_SCRIPT%"
    echo Please ensure a virtual environment named '%VENV_DIR%' exists in the project root
    echo (relative to this .bat file) and contains the Scripts\activate.bat file.
    echo You might need to edit the VENV_DIR variable in this script.
    pause
    exit /b 1
)

REM Check if client script exists
IF NOT EXIST "%CLIENT_SCRIPT%" (
    echo ERROR: Client script (flet_client.py) not found at "%CLIENT_SCRIPT%"
    pause
    exit /b 1
)

echo Activating virtual environment from %VENV_DIR%...
call "%ACTIVATE_SCRIPT%"

IF DEFINED VIRTUAL_ENV (
    echo Virtual environment activated: %VIRTUAL_ENV%
    echo Starting Flet client (flet_client.py)...
    flet run "%CLIENT_SCRIPT%"
) ELSE (
    echo ERROR: Failed to activate virtual environment.
    echo Please check the VENV_DIR variable and the virtual environment path.
)

echo Client stopped.
pause 