# -*- mode: python ; coding: utf-8 -*-
from importlib.metadata import distributions
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

root = Path(SPECPATH).resolve().parent
version_text = (root / "censor_studio" / "__init__.py").read_text("utf-8").split('"')[1]
version = tuple(map(int, version_text.split("."))) + (0,)

data = [(str(root / filename), ".") for filename in ("icon.ico", "사용법.md")]
data.append((str(root / "packaging" / "licenses"), "licenses/Qt"))
data.append((str(root / "packaging" / "directml"), "licenses/DirectML"))
data.append((str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses/Python"))

for dist in distributions():
    if dist.metadata["Name"].lower() in {"pip", "pyside6_essentials", "shiboken6"}:
        continue
    for path in dist.files or ():
        if path.parent.name.endswith(".dist-info") and path.name.lower().startswith(("license", "copying", "notice")):
            data.append((str(dist.locate_file(path)), f"licenses/{dist.metadata['Name']}"))
        elif "licenses" in path.parts and path.parts[0].endswith(".dist-info"):
            relative = Path(*path.parts[path.parts.index("licenses") + 1:-1])
            data.append((str(dist.locate_file(path)), str(Path("licenses", dist.metadata["Name"], relative))))

ort_data, binaries, hidden = collect_all("onnxruntime")
data += [entry for entry in ort_data if entry[1].replace("\\", "/") != "onnxruntime/datasets"]
binaries += collect_dynamic_libs("hf_xet")
hidden += collect_submodules("huggingface_hub")
hidden += ["cv2"]

a = Analysis(
    [str(root / "run_app.py")],
    pathex=[str(root)],
    binaries=binaries,
    datas=data,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick"],
)
# Windows 10/11 provide these DLLs. Do not collect unrelated copies from PATH.
os_dlls = {"ucrtbase.dll", "icuuc.dll", "icuin.dll", "icudt.dll"}
a.binaries = [entry for entry in a.binaries
              if Path(entry[0]).name.lower() not in os_dlls
              and not Path(entry[0]).name.lower().startswith(("api-ms-win-", "icudt"))]
pyz = PYZ(a.pure)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version, prodvers=version, OS=0x40004, fileType=1),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("FileDescription", "Censor Studio"),
            StringStruct("FileVersion", version_text),
            StringStruct("ProductName", "Censor Studio"),
            StringStruct("ProductVersion", version_text),
            StringStruct("LegalCopyright", "Copyright (c) 2026 DarrnB"),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="CensorStudio", console=False,
          upx=False, icon=str(root / "icon.ico"), version=version_info)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="CensorStudio")
