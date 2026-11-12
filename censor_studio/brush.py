"""브러시 프리셋과 스트로크 엔진.

레이어는 0~1 범위의 float32 알파 마스크(H, W)다. 브러시는 그 마스크에 점(dab)을
찍어 올리고, 지우개는 같은 점으로 깎아낸다.
"""
from __future__ import annotations

import base64
import json
import math
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import cv2
import numpy as np

Rect = tuple[int, int, int, int]  # x0, y0, x1, y1 (x1, y1 제외)


def union(a: Rect | None, b: Rect | None) -> Rect | None:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


@dataclass
class BrushPreset:
    name: str = "구름 에어브러시"
    size: float = 120.0          # 지름(px)
    hardness: float = 0.10       # 경도 0~1
    opacity: float = 1.0         # 한 획이 도달할 수 있는 최대 불투명도
    flow: float = 0.35           # 점 하나의 농도
    spacing: float = 0.18        # 간격(지름 대비)
    scatter: float = 0.22        # 분산(지름 대비)
    angle: float = 0.0           # 각도(도)
    roundness: float = 1.0       # 원형률 0.05~1
    # 모양 (Shape dynamics)
    size_jitter: float = 0.45
    hardness_jitter: float = 0.50
    angle_jitter: float = 1.0
    roundness_jitter: float = 0.30
    min_size: float = 0.25       # 필압 최소 크기 비율
    pressure_size: bool = True
    # 전송 (Transfer)
    opacity_jitter: float = 0.0
    flow_jitter: float = 0.30
    pressure_opacity: bool = False
    pressure_flow: bool = True
    # 사용자 지정 브러시 모양 (회색조 PNG, base64). 비어 있으면 원형.
    tip_png_b64: str = ""

    # ---- 저장 / 불러오기 -------------------------------------------------
    def to_json(self) -> str:
        return json.dumps({"format": "censor-studio-brush", "version": 1, **asdict(self)},
                          ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "BrushPreset":
        data = json.loads(text)
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    def save(self, path: Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BrushPreset":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    # ---- 사용자 지정 모양 -------------------------------------------------
    def tip_array(self) -> np.ndarray | None:
        if not self.tip_png_b64:
            return None
        buf = np.frombuffer(base64.b64decode(self.tip_png_b64), np.uint8)
        img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        return img.astype(np.float32) / 255.0

    def set_tip_from_image(self, path: Path) -> None:
        """이미지를 브러시 모양으로 쓴다. 알파가 있으면 알파, 없으면 어두운 곳이 진한 곳."""
        img = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError("이미지를 읽을 수 없습니다.")
        if img.dtype != np.uint8:
            img = (img.astype(np.float32) / np.iinfo(img.dtype).max * 255).astype(np.uint8)
        if img.ndim == 3 and img.shape[2] == 4:
            alpha = img[..., 3]
        else:
            gray = img if img.ndim == 2 else cv2.cvtColor(img[..., :3], cv2.COLOR_BGR2GRAY)
            alpha = 255 - gray
        alpha = alpha.astype(np.float32)
        if alpha.max() <= 0:
            raise ValueError("모양으로 쓸 수 있는 내용이 없습니다.")
        alpha = alpha / alpha.max()
        h, w = alpha.shape
        side = max(h, w)
        sq = np.zeros((side, side), np.float32)
        sq[(side - h) // 2:(side - h) // 2 + h, (side - w) // 2:(side - w) // 2 + w] = alpha
        if side > 256:
            sq = cv2.resize(sq, (256, 256), interpolation=cv2.INTER_AREA)
        ok, png = cv2.imencode(".png", (sq * 255).astype(np.uint8))
        self.tip_png_b64 = base64.b64encode(png.tobytes()).decode("ascii")


def round_dab(cx: float, cy: float, diameter: float, hardness: float,
              angle_deg: float, roundness: float) -> tuple[np.ndarray, int, int]:
    r = max(diameter / 2.0, 0.5)
    ext = r + 1.5
    x0, x1 = int(math.floor(cx - ext)), int(math.ceil(cx + ext)) + 1
    y0, y1 = int(math.floor(cy - ext)), int(math.ceil(cy + ext)) + 1
    xs = (np.arange(x0, x1, dtype=np.float32) + 0.5 - cx)[None, :]
    ys = (np.arange(y0, y1, dtype=np.float32) + 0.5 - cy)[:, None]
    if roundness < 0.999:
        c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
        u = xs * c + ys * s
        v = (-xs * s + ys * c) / max(roundness, 0.05)
        d = np.sqrt(u * u + v * v) / r
    else:
        d = np.sqrt(xs * xs + ys * ys) / r
    if hardness >= 0.99:
        a = np.clip((1.0 - d) * r + 0.5, 0.0, 1.0)
    else:
        t = np.clip((1.0 - d) / (1.0 - hardness), 0.0, 1.0)
        a = t * t * (3.0 - 2.0 * t)
    return a.astype(np.float32, copy=False), x0, y0


def tip_dab(tip: np.ndarray, cx: float, cy: float, diameter: float,
            angle_deg: float, roundness: float) -> tuple[np.ndarray, int, int]:
    w = max(int(round(diameter)), 1)
    h = max(int(round(diameter * roundness)), 1)
    interp = cv2.INTER_AREA if w < tip.shape[1] else cv2.INTER_LINEAR
    img = cv2.resize(tip, (w, h), interpolation=interp)
    if abs(angle_deg) % 360 > 0.5:
        side = int(math.ceil(math.hypot(w, h))) + 2
        m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), -angle_deg, 1.0)
        m[0, 2] += side / 2.0 - w / 2.0
        m[1, 2] += side / 2.0 - h / 2.0
        img = cv2.warpAffine(img, m, (side, side), flags=cv2.INTER_LINEAR)
    hh, ww = img.shape
    return img, int(round(cx - ww / 2.0)), int(round(cy - hh / 2.0))


class StrokeEngine:
    """한 획을 그리는 동안의 상태. 획이 끝나면 dirty 영역으로 실행취소 패치를 만든다."""

    def __init__(self, layer: np.ndarray, preset: BrushPreset, erase: bool = False,
                 rng: np.random.Generator | None = None, buildup: bool = False):
        self.buildup = buildup   # True: 점이 겹칠수록 쌓이는 방식 (자동 검열의 구름 칠)
        self.layer = layer
        self.p = preset
        self.erase = erase
        self.rng = rng or np.random.default_rng()
        self.base = layer.copy()
        self.acc = np.zeros_like(layer)
        self.tip = preset.tip_array()
        self.dirty: Rect | None = None
        self.last: tuple[float, float, float] | None = None
        self.carry = 0.0

    def _diameter(self, pressure: float) -> float:
        d = self.p.size
        if self.p.pressure_size:
            d *= self.p.min_size + (1.0 - self.p.min_size) * pressure
        return max(d, 1.0)

    def dab(self, x: float, y: float, pressure: float = 1.0) -> Rect | None:
        p, r = self.p, self.rng
        d = max(self._diameter(pressure) * (1.0 - p.size_jitter * r.random()), 1.0)
        if p.scatter > 0:
            x += (r.random() * 2 - 1) * p.scatter * d
            y += (r.random() * 2 - 1) * p.scatter * d
        hard = min(max(p.hardness + p.hardness_jitter * (r.random() - 0.5), 0.0), 1.0)
        ang = p.angle + p.angle_jitter * 180.0 * (r.random() * 2 - 1)
        rnd = min(max(p.roundness * (1.0 - p.roundness_jitter * r.random()), 0.05), 1.0)
        flow = p.flow * (1.0 - p.flow_jitter * r.random())
        cap = p.opacity * (1.0 - p.opacity_jitter * r.random())
        if p.pressure_flow:
            flow *= pressure
        if p.pressure_opacity:
            cap *= pressure
        if flow <= 0 or cap <= 0:
            return None

        if self.tip is not None:
            mask, x0, y0 = tip_dab(self.tip, x, y, d, ang, rnd)
        else:
            mask, x0, y0 = round_dab(x, y, d, hard, ang, rnd)
        h, w = self.layer.shape
        mh, mw = mask.shape
        cx0, cy0 = max(x0, 0), max(y0, 0)
        cx1, cy1 = min(x0 + mw, w), min(y0 + mh, h)
        if cx0 >= cx1 or cy0 >= cy1:
            return None
        sl = (slice(cy0, cy1), slice(cx0, cx1))
        acc = self.acc[sl]
        if self.buildup:
            # 자동 칠: 점이 겹칠수록 농도가 쌓여 뭉게뭉게한 구름이 된다.
            m = mask[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0] * flow
            np.maximum(acc, np.minimum(acc + m * (1.0 - acc), cap), out=acc)
            base = self.base[sl]
            self.layer[sl] = base * (1.0 - acc) if self.erase else base + (1.0 - base) * acc
            rect = (cx0, cy0, cx1, cy1)
            self.dirty = union(self.dirty, rect)
            return rect
        # 손으로 긋는 획: 점이 겹치면 농도가 자연스럽게 쌓인다(선이 교차해도 골이 생기지 않는다).
        # 다만 간격이 촘촘할수록 점 하나의 농도를 줄여서, 간격과 상관없이 간격 18%로 그은 것과 같은
        # 농도가 되게 한다. 그래야 촘촘한 간격에서도 부드러운 가장자리가 굳지 않고 경도가 그대로 보인다.
        k = min(1.0, max(self.p.spacing, 0.01) / 0.18)
        a = mask[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0] * min(flow, 1.0)
        if k < 1.0:
            a = 1.0 - np.power(1.0 - a, k)
        np.maximum(acc, np.minimum(acc + a * (1.0 - acc), cap), out=acc)
        base = self.base[sl]
        if self.erase:
            self.layer[sl] = base * (1.0 - acc)
        else:
            self.layer[sl] = base + (1.0 - base) * acc
        rect = (cx0, cy0, cx1, cy1)
        self.dirty = union(self.dirty, rect)
        return rect

    def move(self, x: float, y: float, pressure: float = 1.0) -> Rect | None:
        if self.last is None:
            self.last = (x, y, pressure)
            return self.dab(x, y, pressure)
        lx, ly, lp = self.last
        dist = math.hypot(x - lx, y - ly)
        rect = None
        if dist > 0:
            pos = 0.0
            while True:
                f = pos / dist
                step = max(1.0, self.p.spacing * self._diameter(lp + (pressure - lp) * f))
                need = step - self.carry
                if pos + need > dist:
                    self.carry += dist - pos
                    break
                pos += need
                self.carry = 0.0
                f = pos / dist
                rect = union(rect, self.dab(lx + (x - lx) * f, ly + (y - ly) * f,
                                            lp + (pressure - lp) * f))
        self.last = (x, y, pressure)
        return rect


def fill_region(layer: np.ndarray, region: np.ndarray, offset: tuple[int, int],
                preset: BrushPreset, diameter: float,
                rng: np.random.Generator | None = None) -> StrokeEngine:
    """region(불리언, offset 위치) 안을 브러시 점으로 채운다. 가장자리는 브러시 모양대로 번진다."""
    rng = rng or np.random.default_rng()
    diameter = max(float(diameter), 4.0)
    eng = StrokeEngine(layer, replace(preset, size=diameter), rng=rng, buildup=True)
    ox, oy = offset
    rh, rw = region.shape
    if not region.any():
        return eng
    step = max(diameter * 0.30, 2.0)
    k = max(int(diameter * 0.15), 1)
    core = cv2.erode(region.astype(np.uint8), np.ones((k, k), np.uint8)).astype(bool)
    if not core.any():
        core = region
    h, w = layer.shape
    cy0, cx0 = max(oy, 0), max(ox, 0)
    cy1, cx1 = min(oy + rh, h), min(ox + rw, w)
    core_c = core[cy0 - oy:cy1 - oy, cx0 - ox:cx1 - ox]
    for _ in range(12):
        gx = np.arange(step / 2, rw, step)
        gy = np.arange(step / 2, rh, step)
        pts = [(x + (rng.random() - 0.5) * step, y + (rng.random() - 0.5) * step)
               for y in gy for x in gx]
        rng.shuffle(pts)
        for x, y in pts:
            xi, yi = min(max(int(x), 0), rw - 1), min(max(int(y), 0), rh - 1)
            if region[yi, xi]:
                eng.dab(ox + x, oy + y, 1.0)
        if core_c.size == 0 or float(layer[cy0:cy1, cx0:cx1][core_c].min()) >= 0.995 * preset.opacity:
            break
    return eng


def render_preview(preset: BrushPreset, width: int = 280, height: int = 64) -> np.ndarray:
    """프리셋 미리보기용 S자 획. 0~1 float32 (H, W)."""
    layer = np.zeros((height, width), np.float32)
    k = min(1.0, (height * 0.55) / max(preset.size, 1.0))
    eng = StrokeEngine(layer, replace(preset, size=max(preset.size * k, 2.0)),
                       rng=np.random.default_rng(7))
    n = 60
    for i in range(n + 1):
        t = i / n
        x = width * (0.08 + 0.84 * t)
        y = height * (0.5 + 0.22 * math.sin(t * math.pi * 2))
        eng.move(x, y, 0.15 + 0.85 * math.sin(t * math.pi))
    return layer
