@echo off
setlocal
python -m pip install -e ".[dev]"
python -m pytest -q
python -m PyInstaller --noconfirm --clean --windowed --name DuplicateFileOrganizer src\duplicate_file_organizer\main.py
endlocal
