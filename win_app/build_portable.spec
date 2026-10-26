from pathlib import Path


source_dir = Path(SPECPATH)
app_modules = sorted(path.stem for path in source_dir.glob("*.py") if path.stem != "kvm_bridge_win")
core_modules = sorted(f"core.{path.stem}" for path in (source_dir.parent / "core").glob("*.py") if path.stem != "__init__")

analysis = Analysis(
    [str(source_dir / "portable_entry.py")],
    pathex=[str(source_dir), str(source_dir.parent)],
    binaries=[],
    datas=[(str(source_dir / "Beamer.ico"), "."), (str(source_dir / "assets"), "assets"),
           (str(source_dir.parent / "VERSION"), "."),
           (str(source_dir / "identify_layer_shell.py"), ".")]
          + [(str(path), "locales") for path in (source_dir.parent / "locales").glob("*.po")],
    hiddenimports=["kvm_bridge_win", "nacl", "_cffi_backend", "segno", "pyaudiowpatch", "win32com.client", "pythoncom", "pywintypes", "comtypes", "pycaw"] + app_modules + core_modules,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(source_dir / "hooks" / "portable_storage.py")],
    excludes=["PIL", "pystray", "tkinter", "numpy", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore", "PySide6.QtMultimedia"],
    noarchive=False,
)

# Keep the installed build's UCRT policy: Windows supplies these, while Qt's ICU DLLs stay.
filtered_binaries = [
    binary for binary in analysis.binaries
    if binary[0].lower() != "ucrtbase.dll"
    and not binary[0].lower().startswith("api-ms-win-")
]

python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Beamer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(source_dir / "Beamer.ico"),
    # The one-folder bootloader needs no temporary extraction or elevation.
    uac_admin=False,
    uac_uiaccess=False,
    contents_directory="_internal",
)

portable = COLLECT(
    executable,
    filtered_binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Beamer-portable",
)
