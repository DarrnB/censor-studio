"""1.1 작업 파일과 호환되는 이미지·레이어·대기열 보존."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np

from .document import Document, Item, ItemPatch

DOC_FIELDS = ("path", "base", "alpha", "censor", "protect", "items", "auto_done",
              "dirty", "undo_stack", "redo_stack", "effect_key", "view_opacity")
ITEM_FIELDS = ("kind", "label", "score", "box", "region", "offset", "diameter")


class SessionWriter:
    def __init__(self, archive: ZipFile):
        self.archive = archive
        self.arrays = {}
        self.items = {}

    def encode(self, value):
        if isinstance(value, np.ndarray):
            key = id(value)
            if key not in self.arrays:
                name = f"arrays/{len(self.arrays)}.npy"
                with self.archive.open(name, "w", force_zip64=True) as stream:
                    np.save(stream, value, allow_pickle=False)
                self.arrays[key] = name
            return {"array": self.arrays[key]}
        if isinstance(value, Item):
            key = id(value)
            if key in self.items:
                return {"item_ref": self.items[key]}
            index = self.items[key] = len(self.items)
            data = {name: self.encode(getattr(value, name)) for name in ITEM_FIELDS}
            data["group"] = self.encode((value.footprint, value.enabled))
            return {"item": index, "fields": data}
        if isinstance(value, ItemPatch):
            return {"patch": self.encode(tuple(value)), "state": self.encode(value.item_state)}
        if isinstance(value, tuple):
            return {"tuple": [self.encode(v) for v in value]}
        if isinstance(value, list):
            return [self.encode(v) for v in value]
        if isinstance(value, dict):
            return {k: self.encode(v) for k, v in value.items()}
        return value.item() if isinstance(value, np.generic) else value


class SessionReader:
    def __init__(self, archive: ZipFile):
        self.archive = archive
        self.arrays = {}
        self.items = {}

    def decode(self, value):
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if not isinstance(value, dict):
            return value
        if "array" in value:
            name = value["array"]
            if name not in self.arrays:
                with self.archive.open(name) as stream:
                    self.arrays[name] = np.load(stream, allow_pickle=False)
            return self.arrays[name]
        if "item_ref" in value:
            return self.items[value["item_ref"]]
        if "item" in value:
            data = self.decode(value["fields"])
            footprint, enabled = data.pop("group", (None, False))
            item = self.items[value["item"]] = Item(**data, footprint=footprint, enabled=enabled)
            return item
        if "patch" in value:
            return ItemPatch(self.decode(value["patch"]), *self.decode(value["state"]))
        if "tuple" in value:
            return tuple(self.decode(v) for v in value["tuple"])
        return {k: self.decode(v) for k, v in value.items()}


class Session:
    def __init__(self, directory: Path):
        self.current = directory / "session.zip"
        self.previous = directory / "session.previous.zip"
        self.current_valid = False

    def load(self):
        error = None
        for path in (self.current, self.previous):
            if not path.exists():
                continue
            try:
                with ZipFile(path) as archive:
                    data = SessionReader(archive).decode(json.loads(archive.read("session.json")))
                if data["version"] != 1:
                    raise ValueError("지원하지 않는 작업 저장 버전입니다.")
                docs = {}
                for saved in data["docs"]:
                    doc = Document(saved["path"], (saved["base"], saved["alpha"]))
                    for field in ("censor", "protect", "items", "auto_done", "dirty", "undo_stack", "redo_stack"):
                        setattr(doc, field, saved[field])
                    if saved["effect_key"]:
                        doc.set_effect(*saved["effect_key"])
                    doc.set_view_opacity(saved["view_opacity"])
                    doc.compose()
                    docs[doc.path] = doc
                data["docs"] = docs
                self.current_valid = path == self.current
                return data
            except Exception as exc:
                error = exc
        if error is not None:
            raise error
        return None

    def save(self, data, docs):
        data = {"version": 1, **data,
                "docs": [{name: getattr(doc, name) for name in DOC_FIELDS} for doc in docs]}
        pending = self.current.with_name("session.pending.zip")
        backup = self.previous.with_suffix(".pending")
        try:
            with ZipFile(pending, "w", compression=ZIP_DEFLATED, compresslevel=1) as archive:
                encoded = SessionWriter(archive).encode(data)
                archive.writestr("session.json", json.dumps(encoded, ensure_ascii=False))
            with pending.open("r+b") as stream:
                os.fsync(stream.fileno())
            if self.current_valid:
                shutil.copyfile(self.current, backup)
                os.replace(backup, self.previous)
            os.replace(pending, self.current)
            self.current_valid = True
        finally:
            pending.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
