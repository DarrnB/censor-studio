"""이미지 한 장의 상태: 원본, 검열 레이어, 보호(누끼) 레이어, 실행취소."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import cv2
import numpy as np
from PySide6.QtGui import QImage

from .brush import BrushPreset, Rect, StrokeEngine, fill_region, union

LABELS_KO = {"nipple": "유두", "penis": "음경", "vagina": "음부", "anus": "항문", "pubic": "음모",
             "hand": "손 (보호)"}


@dataclass
class Item:
    """검수 목록의 한 항목."""
    kind: str                 # "censor" | "hand"
    label: str
    score: float
    box: Rect
    region: np.ndarray | None = None      # 칠할 영역(불리언), offset 기준
    offset: tuple[int, int] = (0, 0)
    diameter: float = 60.0
    footprint: tuple[Rect, np.ndarray] | None = field(default=None, repr=False)
    enabled: bool = False

    @property
    def title(self) -> str:
        return f"{LABELS_KO.get(self.label, self.label)}  {self.score * 100:.0f}%"


class ItemPatch(tuple):
    """레이어 변경과 검수 항목의 켜짐 상태를 함께 실행취소한다."""

    def __new__(cls, pixels, item, before, after):
        patch = super().__new__(cls, pixels)
        patch.item_state = (item, before, after)
        return patch


def read_image(path: str) -> tuple[np.ndarray, np.ndarray | None]:
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"이미지를 열 수 없습니다: {path}")
    if img.dtype != np.uint8:
        img = (img.astype(np.float32) / np.iinfo(img.dtype).max * 255).astype(np.uint8)
    alpha = None
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:
        alpha = np.ascontiguousarray(img[..., 3])
        img = img[..., :3]
    return np.ascontiguousarray(img), alpha


class Document:
    def __init__(self, path: str, image: tuple[np.ndarray, np.ndarray | None] | None = None):
        self.path = path
        self.base, self.alpha = read_image(path) if image is None else image
        h, w = self.base.shape[:2]
        self.h, self.w = h, w
        self.censor = np.zeros((h, w), np.float32)    # 검열 레이어 알파
        self.protect = np.zeros((h, w), np.float32)   # 보호 레이어(누끼) — 여기는 검열이 뚫린다
        self.effect = np.full((h, w, 3), 255, np.uint8)
        self.effect_key: tuple | None = None
        self.view_opacity = 1.0   # 화면에서만 검열을 비쳐 보이게 하는 값 (저장에는 영향 없음)
        self._base_bgra = np.ascontiguousarray(cv2.cvtColor(self.base, cv2.COLOR_BGR2BGRA))
        self._disp = self._base_bgra.copy()
        self._overlay = np.zeros((h, w, 4), np.uint8)
        self.qbase = QImage(self._base_bgra.data, w, h, w * 4, QImage.Format.Format_RGB32)
        self.qimage = QImage(self._disp.data, w, h, w * 4, QImage.Format.Format_RGB32)
        self.qoverlay = QImage(self._overlay.data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied)
        self.items: list[Item] = []
        self.sam_emb: dict | None = None
        self.auto_done = False
        self.dirty = False
        self.undo_stack: list[list[tuple[str, Rect, np.ndarray, np.ndarray]]] = []
        self.redo_stack: list[list[tuple[str, Rect, np.ndarray, np.ndarray]]] = []

    # ---- 검열 표현 방식 -------------------------------------------------
    def set_effect(self, mode: str, color: tuple[int, int, int], strength: int) -> None:
        key = (mode, color, strength)
        if key == self.effect_key:
            return
        self.effect_key = key
        if mode == "mosaic":
            b = max(int(strength), 2)
            small = cv2.resize(self.base, (max(self.w // b, 1), max(self.h // b, 1)), interpolation=cv2.INTER_AREA)
            self.effect = cv2.resize(small, (self.w, self.h), interpolation=cv2.INTER_NEAREST)
        elif mode == "blur":
            k = max(int(strength), 1) * 2 + 1
            self.effect = cv2.GaussianBlur(self.base, (k, k), 0)
        else:
            self.effect = np.empty((self.h, self.w, 3), np.uint8)
            self.effect[:] = (color[2], color[1], color[0])
        self.compose()

    # ---- 합성 -----------------------------------------------------------
    def layer(self, name: str) -> np.ndarray:
        return self.censor if name == "censor" else self.protect

    def final_alpha(self, rect: Rect | None = None) -> np.ndarray:
        x0, y0, x1, y1 = rect or (0, 0, self.w, self.h)
        return self.censor[y0:y1, x0:x1] * (1.0 - self.protect[y0:y1, x0:x1])

    def compose(self, rect: Rect | None = None) -> None:
        x0, y0, x1, y1 = rect or (0, 0, self.w, self.h)
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, self.w), min(y1, self.h)
        if x0 >= x1 or y0 >= y1:
            return
        sl = (slice(y0, y1), slice(x0, x1))
        a = (self.final_alpha((x0, y0, x1, y1)) * self.view_opacity)[..., None]
        base = self.base[sl].astype(np.float32)
        out = base + (self.effect[sl].astype(np.float32) - base) * a
        self._disp[y0:y1, x0:x1, :3] = (out + 0.5).astype(np.uint8)
        p = self.protect[sl]
        ov = self._overlay[sl]
        ov[..., 0] = (p * 60).astype(np.uint8)    # B
        ov[..., 1] = (p * 130).astype(np.uint8)   # G
        ov[..., 2] = 0
        ov[..., 3] = (p * 130).astype(np.uint8)

    def set_view_opacity(self, value: float) -> None:
        if value != self.view_opacity:
            self.view_opacity = value
            self.compose()

    def result_image(self) -> np.ndarray:
        a = self.final_alpha()[..., None]
        base = self.base.astype(np.float32)
        out = (base + (self.effect.astype(np.float32) - base) * a + 0.5).astype(np.uint8)
        if self.alpha is not None:
            out = np.dstack([out, self.alpha])
        return out

    def save(self, path: str) -> None:
        ext = Path(path).suffix.lower() or ".png"
        img = self.result_image()
        if ext in (".jpg", ".jpeg") and img.shape[2] == 4:
            img = img[..., :3]
        params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else \
                 [cv2.IMWRITE_WEBP_QUALITY, 95] if ext == ".webp" else []
        ok, buf = cv2.imencode(ext, img, params)
        if not ok:
            raise ValueError("저장에 실패했습니다.")
        # 먼저 같은 폴더에 완성한 다음 교체한다. 실패하면 기존 파일이 남는다.
        dest = Path(path)
        with NamedTemporaryFile(dir=dest.parent, prefix=".censor-", suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
            try:
                stream.write(buf)
                stream.close()
                os.replace(temp, dest)
            finally:
                stream.close()
                temp.unlink(missing_ok=True)
        self.dirty = False

    # ---- 편집 + 실행취소 ------------------------------------------------
    def begin_stroke(self, layer: str, preset: BrushPreset, erase: bool) -> StrokeEngine:
        eng = StrokeEngine(self.layer(layer), preset, erase=erase)
        eng.layer_name = layer
        return eng

    def _patch(self, layer: str, rect: Rect, before: np.ndarray) -> tuple[str, Rect, np.ndarray, np.ndarray]:
        x0, y0, x1, y1 = rect
        return (layer, rect, before[y0:y1, x0:x1].copy(), self.layer(layer)[y0:y1, x0:x1].copy())

    def push(self, patches: list[tuple[str, Rect, np.ndarray, np.ndarray]]) -> None:
        patches = [p for p in patches if p is not None]
        if not patches:
            return
        self.undo_stack.append(patches)
        del self.undo_stack[:-60]
        self.redo_stack.clear()
        self.dirty = True

    def commit_stroke(self, eng: StrokeEngine) -> None:
        if eng.dirty is not None:
            self.push([self._patch(eng.layer_name, eng.dirty, eng.base)])

    def _apply(self, patches, index: int) -> Rect | None:
        rect = None
        for patch in reversed(patches) if index == 0 else patches:
            layer, r, before, after = patch
            x0, y0, x1, y1 = r
            self.layer(layer)[y0:y1, x0:x1] = (before, after)[index]
            if isinstance(patch, ItemPatch):
                item, old_state, new_state = patch.item_state
                item.footprint, item.enabled = (old_state, new_state)[index]
            rect = union(rect, r)
        if rect:
            self.compose(rect)
        self.dirty = True
        return rect

    def undo(self) -> Rect | None:
        if not self.undo_stack:
            return None
        patches = self.undo_stack.pop()
        self.redo_stack.append(patches)
        return self._apply(patches, 0)

    def redo(self) -> Rect | None:
        if not self.redo_stack:
            return None
        patches = self.redo_stack.pop()
        self.undo_stack.append(patches)
        return self._apply(patches, 1)

    # ---- 영역 단위 작업 -------------------------------------------------
    def paint_region(self, layer: str, region: np.ndarray, offset: tuple[int, int],
                     preset: BrushPreset, diameter: float):
        """영역을 브러시로 칠한다. 실행취소 패치를 돌려준다."""
        eng = fill_region(self.layer(layer), region, offset, preset, diameter)
        if eng.dirty is None:
            return None
        self.compose(eng.dirty)
        return self._patch(layer, eng.dirty, eng.base)

    def paint_mask(self, layer: str, mask: np.ndarray, preset: BrushPreset):
        """누끼 마스크(전체 크기 불리언)를 현재 브러시 설정 그대로 칠한다.

        안쪽은 빈틈없이 채우고, 가장자리는 브러시 성격을 따른다:
        단단한 브러시면 누끼 윤곽 그대로 또렷하게, 부드럽거나 흩뿌리는 브러시면 그만큼만 밖으로 번진다.
        """
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return None
        x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
        diameter = min(preset.size, float(max(min(x1 - x0, y1 - y0), 12)))
        lay = self.layer(layer)
        eng = fill_region(lay, mask[y0:y1, x0:x1], (x0, y0), preset, diameter)
        rect = union(eng.dirty, (x0, y0, x1, y1))
        rx0, ry0, rx1, ry1 = rect
        sl = (slice(ry0, ry1), slice(rx0, rx1))
        m = mask[sl].astype(np.float32)
        bleed = diameter * (0.5 * (1.0 - preset.hardness) + 0.5 * preset.scatter)
        if bleed >= 1.0:
            k = int(bleed) * 2 + 1
            allow = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
            allow = cv2.GaussianBlur(allow, (0, 0), max(bleed / 2.0, 0.8))
        else:
            allow = cv2.GaussianBlur(m, (0, 0), 0.8)
        core = cv2.GaussianBlur(m, (0, 0), 0.8) * preset.opacity   # 가는 부분도 빈틈없이 덮이게 한다
        acc = np.maximum(eng.acc[sl] * allow, core)
        base = eng.base[sl]
        lay[sl] = base + (1.0 - base) * acc
        self.compose(rect)
        return self._patch(layer, rect, eng.base)

    def _feather_mask(self, mask: np.ndarray, feather: float):
        ys, xs = np.where(mask)
        if len(xs) == 0:
            return None
        pad = 4
        rect = (max(int(xs.min()) - pad, 0), max(int(ys.min()) - pad, 0),
                min(int(xs.max()) + 1 + pad, self.w), min(int(ys.max()) + 1 + pad, self.h))
        x0, y0, x1, y1 = rect
        m = mask[y0:y1, x0:x1].astype(np.float32)
        if feather > 0:
            m = cv2.GaussianBlur(m, (0, 0), feather)
        return rect, m

    def set_mask(self, layer: str, mask: np.ndarray, value: float, feather: float = 1.2):
        """불리언 마스크(전체 크기)를 레이어에 또렷하게 더하거나(1) 뺀다(0)."""
        region = self._feather_mask(mask, feather)
        if region is None:
            return None
        rect, m = region
        x0, y0, x1, y1 = rect
        lay = self.layer(layer)
        before = lay.copy()
        if value >= 0.5:
            lay[y0:y1, x0:x1] = np.maximum(lay[y0:y1, x0:x1], m)
        else:
            lay[y0:y1, x0:x1] *= (1.0 - m)
        self.compose(rect)
        return self._patch(layer, rect, before)

    def paint_item(self, item: Item, preset: BrushPreset):
        if item.region is None:
            return None
        hand = item.kind == "hand"
        layer = "protect" if hand else "censor"
        coverage = np.zeros_like(self.censor)
        if hand:
            x, y = item.offset
            h, w = item.region.shape
            coverage[y:y + h, x:x + w] = item.region
            footprint = self._feather_mask(coverage, 1.2)
        else:
            engine = fill_region(coverage, item.region, item.offset, preset, item.diameter)
            footprint = None
            if engine.dirty is not None:
                x0, y0, x1, y1 = engine.dirty
                footprint = (engine.dirty, coverage[y0:y1, x0:x1].copy())
        if footprint is None:
            return None
        rect, alpha = footprint
        x0, y0, x1, y1 = rect
        pixels = self.layer(layer)[y0:y1, x0:x1]
        before = pixels.copy()
        state = (item.footprint, item.enabled)
        pixels[:] = np.maximum(pixels, alpha) if hand else pixels + (1.0 - pixels) * alpha
        item.footprint, item.enabled = footprint, True
        self.compose(rect)
        return ItemPatch((layer, rect, before, pixels.copy()), item, state, (footprint, True))

    def clear_item(self, item: Item):
        if not item.enabled or item.footprint is None:
            return None
        layer = "protect" if item.kind == "hand" else "censor"
        rect, alpha = item.footprint
        x0, y0, x1, y1 = rect
        erase = alpha > 0
        for other in self.items:
            if other is item or other.kind != item.kind or not other.enabled or other.footprint is None:
                continue
            (ox0, oy0, ox1, oy1), other_alpha = other.footprint
            left, top, right, bottom = max(x0, ox0), max(y0, oy0), min(x1, ox1), min(y1, oy1)
            if left < right and top < bottom:
                overlap = other_alpha[top - oy0:bottom - oy0, left - ox0:right - ox0]
                erase[top - y0:bottom - y0, left - x0:right - x0] &= overlap <= 0
        pixels = self.layer(layer)[y0:y1, x0:x1]
        before = pixels.copy()
        pixels[erase] = 0
        item.enabled = False
        self.compose(rect)
        return ItemPatch((layer, rect, before, pixels.copy()), item,
                         (item.footprint, True), (item.footprint, False))

    def clear_rect(self, layer: str, rect: Rect):
        x0, y0, x1, y1 = (max(rect[0], 0), max(rect[1], 0), min(rect[2], self.w), min(rect[3], self.h))
        if x0 >= x1 or y0 >= y1:
            return None
        lay = self.layer(layer)
        if not lay[y0:y1, x0:x1].any():
            return None
        before = lay.copy()
        lay[y0:y1, x0:x1] = 0
        self.compose((x0, y0, x1, y1))
        return self._patch(layer, (x0, y0, x1, y1), before)
