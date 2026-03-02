@echo off
setlocal

cd /d "%~dp0"

set "BUILD_VENV=.build_venv"
set "VENV_PY=%CD%\%BUILD_VENV%\Scripts\python.exe"
set "FLET_EXE=%CD%\%BUILD_VENV%\Scripts\flet.exe"

echo [0/6] Preparing isolated build environment...
if exist "%BUILD_VENV%" rmdir /s /q "%BUILD_VENV%"
python -m venv "%BUILD_VENV%"
if errorlevel 1 goto :error

if not exist "%VENV_PY%" (
  echo Unable to find venv python at "%VENV_PY%"
  goto :error
)

set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"

echo [1/6] Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :error

"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :error

"%VENV_PY%" -m pip install pyinstaller flet-cli==0.81.0 Pillow TypeTreeGeneratorAPI fmod_toolkit archspec
if errorlevel 1 goto :error

"%VENV_PY%" -m pip install --upgrade UnityPy
if errorlevel 1 goto :error

"%VENV_PY%" -m pip install --upgrade flet-desktop==0.81.0
if errorlevel 1 goto :error

if not exist "%FLET_EXE%" (
  echo Unable to find flet CLI at "%FLET_EXE%"
  goto :error
)

echo [2/6] Verifying UnityPy import path...
"%VENV_PY%" -c "import UnityPy; print('UnityPy import path:', UnityPy.__file__)"
if errorlevel 1 goto :error

echo [3/6] Building executable...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
"%FLET_EXE%" pack app.py ^
  --yes ^
  --name ps5_unity_texture_tool ^
  --distpath dist ^
  --hidden-import UnityPy ^
  --hidden-import UnityPy.export ^
  --hidden-import UnityPy.helpers ^
  --hidden-import UnityPy.enums ^
  --hidden-import UnityPy.resources ^
  --pyinstaller-build-args=--collect-data=UnityPy ^
  --pyinstaller-build-args=--collect-data=fmod_toolkit ^
  --pyinstaller-build-args=--collect-data=archspec ^
  --hidden-import texture2ddecoder
if errorlevel 1 goto :error

echo [4/6] Preparing release files...
if exist release rmdir /s /q release
mkdir release
if errorlevel 1 goto :error

copy /y dist\ps5_unity_texture_tool.exe release\ >nul
if errorlevel 1 goto :error

copy /y README.md release\ >nul
if errorlevel 1 goto :error

copy /y LICENSE release\ >nul
if errorlevel 1 goto :error

echo [5/6] Creating ZIP archive...
if exist PS5_Unity_Texture_Tool.zip del /f /q PS5_Unity_Texture_Tool.zip
"%VENV_PY%" -c "import shutil; shutil.make_archive('PS5_Unity_Texture_Tool', 'zip', root_dir='release')"
if errorlevel 1 goto :error

echo [6/6] Done.
echo EXE: dist\ps5_unity_texture_tool.exe
echo ZIP: PS5_Unity_Texture_Tool.zip
exit /b 0

:error
echo Build failed. Error level: %errorlevel%
exit /b %errorlevel%
