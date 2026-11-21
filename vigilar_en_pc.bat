@echo off
title Vigilante PS5
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo No encuentro Python. Instalalo desde python.org marcando "Add python.exe to PATH" y vuelve a abrir este archivo.
  pause
  exit /b 1
)

echo Buscando la ultima version en GitHub...
curl -fsSL -o vigilar.py.nuevo https://raw.githubusercontent.com/jortigosa7/vigilante_play5/main/vigilar.py && move /y vigilar.py.nuevo vigilar.py >nul
curl -fsSL -o productos.json.nuevo https://raw.githubusercontent.com/jortigosa7/vigilante_play5/main/productos.json && move /y productos.json.nuevo productos.json >nul
curl -fsSL -o requirements.txt.nuevo https://raw.githubusercontent.com/jortigosa7/vigilante_play5/main/requirements.txt && move /y requirements.txt.nuevo requirements.txt >nul
del /q *.nuevo 2>nul

if not exist ".instalado" (
  echo Instalando lo necesario, solo la primera vez...
  python -m pip install -r requirements.txt
  python -m playwright install chromium
  echo ok> .instalado
)

if not exist "avisos_local.txt" python vigilar.py --configurar

echo.
echo Vigilando. Deja esta ventana abierta o minimizada. Para parar: Ctrl+C o cierra la ventana.
echo.
python vigilar.py --bucle 120 --resumen
pause
