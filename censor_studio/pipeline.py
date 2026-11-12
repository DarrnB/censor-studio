"""자동 검열 파이프라인: 부위 탐지 → 칠할 영역 계산 → 겹치는 손 누끼."""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .document import Item
from .models import Models


@dataclass
class AutoConfig:
    auto_on_drop: bool = True
    conf: float = 0.30            # 부위 탐지 신뢰도 하한
    margin: float = 0.10          # 탐지 박스를 얼마나 넓혀 칠할지
    brush_pct: float = 0.40       # 자동 칠하기 브러시 지름 (영역 짧은 변 대비)
    detector: str = "seg-medium"  # seg-nano | seg-medium | seg-xl | deepghs
    shape: str = "mask"           # mask(탐지 모델 윤곽) | ellipse | sam
    protect_hands: bool = True
    hand_conf: float = 0.45
    infer_size: int = 640
    use_gpu: bool = True
    classes: dict = field(default_factory=lambda: {"nipple": True, "penis": True, "vagina": True,
                                                   "anus": True, "pubic": False})
    # 부위별 설정: {"penis": {"conf": 0.3, "margin": 0.1, "brush_pct": 0.4}, ...}. 없는 값은 위의 공통값을 쓴다.
    per_class: dict = field(default_factory=dict)

    def part(self, label: str, key: str) -> float:
        """부위(label)의 탐지 하한(conf)·영역 여유(margin)·브러시 크기(brush_pct)."""
        return float(self.per_class.get(label, {}).get(key, getattr(self, key)))


def _expand(box, ratio: float, w: int, h: int, extra: float = 0.0):
    x0, y0, x1, y1 = box
    mx, my = (x1 - x0) * ratio + extra, (y1 - y0) * ratio + extra
    return (max(int(x0 - mx), 0), max(int(y0 - my), 0), min(int(x1 + mx + 0.5), w), min(int(y1 + my + 0.5), h))


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _dedupe(dets):
    """같은 손을 두 번 잡은 박스는 점수 높은 쪽만 남긴다."""
    kept = []
    for d in sorted(dets, key=lambda d: -d.score):
        a = (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])
        dup = False
        for k in kept:
            ix = min(d.box[2], k.box[2]) - max(d.box[0], k.box[0])
            iy = min(d.box[3], k.box[3]) - max(d.box[1], k.box[1])
            b = (k.box[2] - k.box[0]) * (k.box[3] - k.box[1])
            if ix > 0 and iy > 0 and ix * iy > 0.6 * min(a, b):
                dup = True
                break
        if not dup:
            kept.append(d)
    return kept


def _negative_points(hand_box, det_boxes):
    """손이 쥐고 있는 부위가 손 누끼에 딸려 오지 않도록, 부위 쪽에 '제외' 지점을 찍는다."""
    hx0, hy0, hx1, hy1 = hand_box
    pts = []
    for dx0, dy0, dx1, dy1 in det_boxes:
        if not _overlap(hand_box, (dx0, dy0, dx1, dy1)):
            continue
        cx, cy = (dx0 + dx1) / 2, (dy0 + dy1) / 2
        if not (hx0 <= cx < hx1 and hy0 <= cy < hy1):
            pts.append((cx, cy, 0))
            continue
        # 부위 중심이 손 박스 안이면, 손 박스 밖으로 삐져나온 가장 큰 띠의 중심을 쓴다.
        strips = [(dx0, dy0, min(hx0, dx1), dy1), (max(hx1, dx0), dy0, dx1, dy1),
                  (dx0, dy0, dx1, min(hy0, dy1)), (dx0, max(hy1, dy0), dx1, dy1)]
        strips = [s for s in strips if s[2] - s[0] > 4 and s[3] - s[1] > 4]
        if strips:
            s = max(strips, key=lambda s: (s[2] - s[0]) * (s[3] - s[1]))
            pts.append(((s[0] + s[2]) / 2, (s[1] + s[3]) / 2, 0))
    return pts


def _clean(mask: np.ndarray) -> np.ndarray:
    """자잘한 조각을 버리고 큰 덩어리만 남긴다."""
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    if n <= 1:
        return m.astype(bool)
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep = np.where(areas >= 0.15 * areas.max())[0] + 1
    return np.isin(lab, keep)


def full_mask(item: Item, h: int, w: int) -> np.ndarray:
    m = np.zeros((h, w), bool)
    if item.region is not None:
        ox, oy = item.offset
        rh, rw = item.region.shape
        m[oy:oy + rh, ox:ox + rw] = item.region
    return m


def run_auto(models: Models, bgr: np.ndarray, cfg: AutoConfig, status=lambda s: None):
    """(검수 항목 목록, SAM 임베딩 또는 None)을 돌려준다. 레이어는 건드리지 않는다."""
    h, w = bgr.shape[:2]
    status("검열 부위 탐지 중… (첫 실행은 모델을 내려받습니다)")
    enabled = [k for k, on in cfg.classes.items() if on]
    lowest = min([cfg.part(k, "conf") for k in enabled] or [cfg.conf])
    # 가장 낮은 하한으로 한 번 탐지한 뒤, 부위마다 자기 하한으로 거른다.
    dets = [d for d in models.detector(cfg.detector).detect(bgr, conf=lowest, size=cfg.infer_size)
            if cfg.classes.get(d.label, d.label != "pubic") and d.score >= cfg.part(d.label, "conf")]
    hands = []
    if cfg.protect_hands and dets:
        status("손 탐지 중…")
        hands = _dedupe(models.hand.detect(bgr, conf=cfg.hand_conf, size=cfg.infer_size))

    emb = None
    if cfg.shape == "sam" or hands:
        status("누끼 모델(SAM 2.1) 준비 중…")
        emb = models.sam.encode(bgr)

    items: list[Item] = []
    raw_boxes = [d.box for d in dets]
    for d in dets:
        margin = cfg.part(d.label, "margin")
        ex = _expand(d.box, margin, w, h)
        ew, eh = ex[2] - ex[0], ex[3] - ex[1]
        region = None
        k = max(int(min(ew, eh) * margin), 1) * 2 + 1
        grow = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        if cfg.shape == "mask" and d.mask is not None:
            # 탐지 모델이 준 윤곽을 여유만큼 넓혀서 칠한다.
            full = np.zeros((eh, ew), np.uint8)
            ox, oy = d.box[0] - ex[0], d.box[1] - ex[1]
            mh_, mw_ = d.mask.shape
            full[oy:oy + mh_, ox:ox + mw_] = d.mask[:eh - oy, :ew - ox]
            region = cv2.dilate(full, grow).astype(bool)
        elif cfg.shape == "sam" and emb is not None:
            mk = models.sam.predict(emb, box=d.box)[ex[1]:ex[3], ex[0]:ex[2]]
            box_area = (d.box[2] - d.box[0]) * (d.box[3] - d.box[1])
            if mk.sum() >= 0.2 * box_area:
                region = cv2.dilate(mk.astype(np.uint8), grow).astype(bool)
        if region is None:
            canvas = np.zeros((eh, ew), np.uint8)
            cv2.ellipse(canvas, (ew // 2, eh // 2), (max(ew // 2, 1), max(eh // 2, 1)), 0, 0, 360, 1, -1)
            region = canvas.astype(bool)
        diameter = max(min(ew, eh) * cfg.part(d.label, "brush_pct"), 12.0)
        items.append(Item("censor", d.label, d.score, ex, region, (ex[0], ex[1]), diameter))

    if hands and emb is not None:
        status("겹치는 손 누끼 따는 중…")
        reach = [_expand(it.box, 0.0, w, h, extra=it.diameter * 0.5) for it in items]
        for hd in hands:
            if not any(_overlap(hd.box, r) for r in reach):
                continue
            hx = _expand(hd.box, 0.04, w, h)
            mk = models.sam.predict(emb, box=hd.box, points=_negative_points(hd.box, raw_boxes))
            mk = _clean(mk[hx[1]:hx[3], hx[0]:hx[2]])
            if not mk.any():
                continue
            items.append(Item("hand", "hand", hd.score, hd.box, mk, (hx[0], hx[1])))
    return items, emb
