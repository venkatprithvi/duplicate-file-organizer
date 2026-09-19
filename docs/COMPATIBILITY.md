# Compatibility target

Development target: Python 3.12.x + PySide6.

The application uses `pathlib`, `os`, `shutil`, `hashlib`, `sqlite3`, and other cross-platform Python APIs rather than shell-specific commands. Python's `pathlib.Path` represents the concrete filesystem flavour of the operating system. Final supported OS versions should be verified during packaging.

Build the macOS application on macOS and the Windows application on Windows. PyInstaller does not cross-compile desktop packages between operating systems.
