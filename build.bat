@echo off
REM ---------------------------------------------------------------------------
REM Build multibuy.exe with PyInstaller, bundling the native Solana/crypto libs
REM and CA certificates so networking + Solana work in the frozen app. Then
REM optionally build an installer with Inno Setup (if installed).
REM Run from the multibuy folder:  build.bat
REM ---------------------------------------------------------------------------

echo Installing build dependencies...
py -m pip install -r requirements.txt pywebview pyinstaller certifi || goto :err

echo Building multibuy.exe ...
py -m PyInstaller ^
  --noconfirm --clean ^
  --name multibuy ^
  --onefile ^
  --windowed ^
  --icon multibuy.ico ^
  --add-data "index.html;." ^
  --add-data "multibuy.ico;." ^
  --collect-all solders ^
  --collect-all solana ^
  --collect-all base58 ^
  --collect-all certifi ^
  --collect-all cryptography ^
  --collect-submodules web3 ^
  --collect-submodules eth_account ^
  --hidden-import cffi ^
  desktop.py || goto :err

echo.
echo Built: dist\multibuy.exe
echo.

set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ISCC%" (
  echo Building installer with Inno Setup...
  "%ISCC%" multibuy.iss || goto :err
  echo Installer: dist\multibuy-setup.exe
) else (
  echo Inno Setup not found - skipping installer.
  echo   Get it from https://jrsoftware.org/isdl.php then re-run build.bat.
)

echo.
echo Done. Vault + settings are stored in:  %%APPDATA%%\multibuy
goto :eof

:err
echo.
echo Build failed. See the messages above.
exit /b 1
