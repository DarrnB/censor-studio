"""허깅페이스 모델 내려받기와 로컬 추론 (ONNX Runtime, DirectML → CPU 폴백)."""
from __future__ import annotations

import ast
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import cv2
import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download, list_repo_files, snapshot_download

SEG_REPO = "01miku/anime-nsfw-segm-yolo26"       # 최신: 항문·음모까지 잡고 윤곽(마스크)도 준다
SEG_FILES = {"nano": "nsfw-anime-nano-x640.onnx", "medium": "nsfw-anime-medium-x1280.onnx",
             "xl": "nsfw-anime-xl-x1280.onnx"}
CENSOR_REPO = "deepghs/anime_censor_detection"   # 구형: 유두·음경·음부 박스만
HAND_REPO = "deepghs/anime_hand_detection"
SAM_REPO = "onnx-community/sam2.1-hiera-small-ONNX"

_lock = threading.Lock()


def _fetch(repo: str, filename: str) -> str:
    """최신 리비전을 받되, 오프라인이면 캐시에 있는 파일을 쓴다."""
    try:
        return hf_hub_download(repo, filename)
    except Exception as online_error:
        try:
            return hf_hub_download(repo, filename, local_files_only=True)
        except Exception:
            raise RuntimeError(f"모델을 내려받지 못했습니다 ({repo}/{filename}). 인터넷 연결을 확인해 주세요.\n"
                               f"원인: {type(online_error).__name__}: {online_error}") from online_error


def _repo_files(repo: str) -> list[str]:
    try:
        return list(list_repo_files(repo))
    except Exception:
        root = Path(snapshot_download(repo, local_files_only=True))
        return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


def latest_variant(repo: str, size: str = "s") -> str:
    """저장소 안에서 버전 번호가 가장 높은 모델 폴더 이름을 고른다. (예: censor_detect_v1.0_s)"""
    best: tuple[tuple[int, ...], int, str] | None = None
    for f in _repo_files(repo):
        m = re.fullmatch(r"(.+_v(\d+(?:\.\d+)*)_([a-z]))/model\.onnx", f)
        if not m:
            continue
        key = (tuple(int(x) for x in m.group(2).split(".")), int(m.group(3) == size), m.group(1))
        if best is None or key > best:
            best = key
    if best is None:
        raise RuntimeError(f"{repo}에서 모델을 찾지 못했습니다.")
    return best[2]


def _session(path: str, use_gpu: bool) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    if use_gpu and "DmlExecutionProvider" in ort.get_available_providers():
        try:
            return ort.InferenceSession(path, opts, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
        except Exception:
            pass
    return ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])


# 모델마다 다른 클래스 이름을 프로그램 공통 이름으로 맞춘다. 여기 없는 클래스(얼굴 등)는 버린다.
LABEL_MAP = {"nipple_f": "nipple", "nipple": "nipple", "penis": "penis", "pussy": "vagina", "vagina": "vagina",
             "anus": "anus", "pubic hair": "pubic", "hand": "hand"}


@dataclass
class Detection:
    label: str
    score: float
    box: tuple[int, int, int, int]  # x0, y0, x1, y1
    mask: np.ndarray | None = None   # 박스 크기의 불리언 윤곽 (세그멘테이션 모델일 때만)


class Yolo:
    def __init__(self, repo: str, use_gpu: bool = True):
        self.variant = latest_variant(repo)
        self.path = _fetch(repo, f"{self.variant}/model.onnx")
        self.labels = json.loads(Path(_fetch(repo, f"{self.variant}/labels.json")).read_text("utf-8"))
        try:
            thr = json.loads(Path(_fetch(repo, f"{self.variant}/threshold.json")).read_text("utf-8"))
            self.threshold = float(thr.get("threshold", 0.3))
        except Exception:
            self.threshold = 0.3
        self.use_gpu = use_gpu
        self.sess = _session(self.path, use_gpu)
        self.input = self.sess.get_inputs()[0].name

    def _run(self, x: np.ndarray) -> np.ndarray:
        try:
            return self.sess.run(None, {self.input: x})[0]
        except Exception:
            if self.sess.get_providers()[0] == "CPUExecutionProvider":
                raise
            self.sess = _session(self.path, False)
            return self.sess.run(None, {self.input: x})[0]

    def detect(self, bgr: np.ndarray, conf: float | None = None, iou: float = 0.6,
               size: int = 640) -> list[Detection]:
        conf = self.threshold if conf is None else conf
        h, w = bgr.shape[:2]
        scale = size / max(h, w)
        nw, nh = max(int(round(w * scale)), 1), max(int(round(h * scale)), 1)
        img = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        pw, ph = (nw + 31) // 32 * 32, (nh + 31) // 32 * 32
        canvas = np.full((ph, pw, 3), 114, np.uint8)
        canvas[:nh, :nw] = img
        x = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
        with _lock:
            out = self._run(x)[0]
        if out.shape[0] < out.shape[1]:
            out = out.T  # (anchors, 4 + classes)
        scores = out[:, 4:]
        cls = scores.argmax(1)
        sc = scores.max(1)
        keep = sc >= conf
        out, cls, sc = out[keep], cls[keep], sc[keep]
        if len(out) == 0:
            return []
        xywh = out[:, :4].copy()
        xywh[:, 0] -= xywh[:, 2] / 2
        xywh[:, 1] -= xywh[:, 3] / 2
        result: list[Detection] = []
        for c in np.unique(cls):
            idx = np.where(cls == c)[0]
            sel = cv2.dnn.NMSBoxes(xywh[idx].tolist(), sc[idx].tolist(), conf, iou)
            for j in np.array(sel).reshape(-1):
                bx, by, bw, bh = xywh[idx[j]] / scale
                box = (int(max(bx, 0)), int(max(by, 0)),
                       int(min(bx + bw, w)), int(min(by + bh, h)))
                if box[2] > box[0] and box[3] > box[1]:
                    name = self.labels[int(c)]
                    result.append(Detection(LABEL_MAP.get(name, name), float(sc[idx[j]]), box))
        result.sort(key=lambda d: (d.box[1], d.box[0]))
        return result


class YoloSeg:
    """YOLO 세그멘테이션 ONNX — 박스와 함께 부위 윤곽 마스크를 낸다."""

    def __init__(self, size: str = "medium", use_gpu: bool = True):
        fname = SEG_FILES.get(size, SEG_FILES["medium"])
        self.variant = fname.rsplit(".", 1)[0]
        self.path = _fetch(SEG_REPO, fname)
        self.sess = _session(self.path, use_gpu)
        inp = self.sess.get_inputs()[0]
        self.input = inp.name
        self.dtype = np.float16 if "float16" in inp.type else np.float32
        meta = self.sess.get_modelmeta().custom_metadata_map
        self.names = ast.literal_eval(meta["names"])
        m = re.search(r"x(\d+)", fname)
        self.size = int(m.group(1)) if m else 640
        self.threshold = 0.3

    def _run(self, x: np.ndarray):
        try:
            return self.sess.run(None, {self.input: x})
        except Exception:
            if self.sess.get_providers()[0] == "CPUExecutionProvider":
                raise
            self.sess = _session(self.path, False)
            return self.sess.run(None, {self.input: x})

    def detect(self, bgr: np.ndarray, conf: float | None = None, iou: float = 0.6,
               size: int | None = None) -> list[Detection]:
        conf = self.threshold if conf is None else conf
        size = self.size
        h, w = bgr.shape[:2]
        scale = size / max(h, w)
        nw, nh = max(int(round(w * scale)), 1), max(int(round(h * scale)), 1)
        img = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        pw, ph = (nw + 31) // 32 * 32, (nh + 31) // 32 * 32
        canvas = np.full((ph, pw, 3), 114, np.uint8)
        canvas[:nh, :nw] = img
        x = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None]).astype(self.dtype)
        with _lock:
            out, protos = self._run(x)
        out = out[0].astype(np.float32)
        protos = protos[0].astype(np.float32)          # (32, mh, mw)
        if out.shape[0] < out.shape[1]:
            out = out.T                                 # (anchors, 4 + classes + 32)
        nc = len(self.names)
        scores = out[:, 4:4 + nc]
        cls = scores.argmax(1)
        sc = scores.max(1)
        keep = sc >= conf
        out, cls, sc = out[keep], cls[keep], sc[keep]
        if len(out) == 0:
            return []
        xywh = out[:, :4].copy()
        xywh[:, 0] -= xywh[:, 2] / 2
        xywh[:, 1] -= xywh[:, 3] / 2
        mh, mw = protos.shape[1:]
        ratio = mw / pw                                 # 입력 좌표 → 마스크 좌표
        flat = protos.reshape(protos.shape[0], -1)
        result: list[Detection] = []
        for c in np.unique(cls):
            label = LABEL_MAP.get(self.names[int(c)])
            if label is None:
                continue
            idx = np.where(cls == c)[0]
            sel = cv2.dnn.NMSBoxes(xywh[idx].tolist(), sc[idx].tolist(), conf, iou)
            for j in np.array(sel).reshape(-1):
                k = idx[j]
                bx, by, bw, bh = xywh[k]
                box = (int(max(bx / scale, 0)), int(max(by / scale, 0)),
                       int(min((bx + bw) / scale + 0.5, w)), int(min((by + bh) / scale + 0.5, h)))
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                mx0, my0 = max(int(np.floor(bx * ratio)), 0), max(int(np.floor(by * ratio)), 0)
                mx1, my1 = min(int(np.ceil((bx + bw) * ratio)), mw), min(int(np.ceil((by + bh) * ratio)), mh)
                mask = None
                if mx1 > mx0 and my1 > my0:
                    logit = (out[k, 4 + nc:] @ flat).reshape(mh, mw)[my0:my1, mx0:mx1]
                    logit = cv2.resize(logit, (box[2] - box[0], box[3] - box[1]), interpolation=cv2.INTER_LINEAR)
                    mask = logit > 0
                    if not mask.any():
                        mask = None
                result.append(Detection(label, float(sc[k]), box, mask))
        result.sort(key=lambda d: (d.box[1], d.box[0]))
        return result


class Sam2:
    """SAM 2.1 — 박스나 클릭 지점으로 누끼(마스크)를 딴다."""

    MEAN = np.array([0.485, 0.456, 0.406], np.float32)
    STD = np.array([0.229, 0.224, 0.225], np.float32)

    def __init__(self, use_gpu: bool = True):
        _fetch(SAM_REPO, "onnx/vision_encoder.onnx_data")
        _fetch(SAM_REPO, "onnx/prompt_encoder_mask_decoder.onnx_data")
        self.enc_path = _fetch(SAM_REPO, "onnx/vision_encoder.onnx")
        self.dec_path = _fetch(SAM_REPO, "onnx/prompt_encoder_mask_decoder.onnx")
        self.enc = _session(self.enc_path, use_gpu)
        self.dec = _session(self.dec_path, False)  # 디코더는 작아서 CPU가 더 빠르고 안정적이다

    def encode(self, bgr: np.ndarray) -> dict:
        h, w = bgr.shape[:2]
        img = cv2.resize(bgr, (1024, 1024), interpolation=cv2.INTER_LINEAR)
        x = (cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0 - self.MEAN) / self.STD
        x = np.ascontiguousarray(x.transpose(2, 0, 1)[None])
        with _lock:
            try:
                outs = self.enc.run(None, {"pixel_values": x})
            except Exception:
                if self.enc.get_providers()[0] == "CPUExecutionProvider":
                    raise
                self.enc = _session(self.enc_path, False)
                outs = self.enc.run(None, {"pixel_values": x})
        return {"emb": outs, "size": (h, w)}

    def predict(self, emb: dict, box: tuple[int, int, int, int] | None = None,
                points: list[tuple[float, float, int]] | None = None) -> np.ndarray:
        """원본 크기의 불리언 마스크. points는 (x, y, 1=포함 / 0=제외)."""
        h, w = emb["size"]
        sx, sy = 1024.0 / w, 1024.0 / h
        pts = [(x * sx, y * sy) for x, y, _ in (points or [])]
        lbl = [int(l) for _, _, l in (points or [])]
        if box is not None:
            # 박스는 왼쪽 위(라벨 2)·오른쪽 아래(라벨 3) 꼭짓점 프롬프트와 같다.
            pts += [(box[0] * sx, box[1] * sy), (box[2] * sx, box[3] * sy)]
            lbl += [2, 3]
        if not pts:
            return np.zeros((h, w), bool)
        feed = {
            "input_points": np.array(pts, np.float32)[None, None],
            "input_labels": np.array(lbl, np.int64)[None, None],
            "input_boxes": np.zeros((1, 0, 4), np.float32),
            "image_embeddings.0": emb["emb"][0],
            "image_embeddings.1": emb["emb"][1],
            "image_embeddings.2": emb["emb"][2],
        }
        with _lock:
            iou, masks, _ = self.dec.run(None, feed)
        cands = [cv2.resize(masks[0, 0, k].astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR) > 0
                 for k in range(masks.shape[2])]
        if box is None:
            return cands[int(np.argmax(iou[0, 0]))]
        # 박스 프롬프트: 박스를 잘 채우면서 박스와 외곽이 맞는 후보를 고른다.
        box_area = max((box[2] - box[0]) * (box[3] - box[1]), 1)
        best, best_score = cands[int(np.argmax(iou[0, 0]))], -1.0
        for k, mk in enumerate(cands):
            ys, xs = np.where(mk)
            if len(xs) < 0.25 * box_area:
                continue
            mb = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
            ix = max(0, min(mb[2], box[2]) - max(mb[0], box[0]))
            iy = max(0, min(mb[3], box[3]) - max(mb[1], box[1]))
            inter = ix * iy
            uni = (mb[2] - mb[0]) * (mb[3] - mb[1]) + box_area - inter
            score = inter / max(uni, 1) * float(iou[0, 0, k])
            if score > best_score:
                best, best_score = mk, score
        return best


class Models:
    """필요할 때 한 번만 불러오는 모델 묶음."""

    def __init__(self, use_gpu: bool = True):
        self.use_gpu = use_gpu
        self._detectors: dict[str, object] = {}
        self._censor: Yolo | None = None
        self._hand: Yolo | None = None
        self._sam: Sam2 | None = None
        self._load_lock = threading.Lock()

    def detector(self, name: str):
        """name: "seg-nano" | "seg-medium" | "seg-xl" (최신 세그멘테이션) 또는 "deepghs" (구형)."""
        if name == "deepghs":
            return self.censor
        with self._load_lock:
            if name not in self._detectors:
                self._detectors[name] = YoloSeg(name.split("-", 1)[-1], self.use_gpu)
            return self._detectors[name]

    @property
    def censor(self) -> Yolo:
        with self._load_lock:
            if self._censor is None:
                self._censor = Yolo(CENSOR_REPO, self.use_gpu)
            return self._censor

    @property
    def hand(self) -> Yolo:
        with self._load_lock:
            if self._hand is None:
                self._hand = Yolo(HAND_REPO, self.use_gpu)
            return self._hand

    @property
    def sam(self) -> Sam2:
        with self._load_lock:
            if self._sam is None:
                self._sam = Sam2(self.use_gpu)
            return self._sam
