@echo off
echo Installing dependencies...
pip install -r requirements.txt

echo Building executable...
pyinstaller --noconfirm --onefile --windowed --name "AIAnswer" --icon=assets/icon.ico main.py

echo Done! Executable is in dist\AIAnswer.exe
pause
