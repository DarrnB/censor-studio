"""Censor Studio — 아니메 이미지 자동 검열 + 검수 + 수동 검열."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import asdict, fields, replace
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QEvent, QLockFile, QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QCursor, QIcon, QImage, QKeySequence, QPalette, QPixmap
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QCheckBox, QColorDialog, QComboBox, QDockWidget,
                               QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QTabBar, QTabWidget,
                               QToolButton, QVBoxLayout, QWidget)

from . import __version__
from .brush import BrushPreset, render_preview
from .canvas import Canvas
from .document import Document, Item
from .models import Models
from .pipeline import AutoConfig, run_auto
from .session import Session
from .wintab import Wintab

ROOT = Path(__file__).resolve().parent.parent
# 설정과 프리셋은 사용자 폴더에 둔다 (설치된 프로그램 폴더에는 쓸 수 없기 때문).
DATA_DIR = Path(os.environ.get("APPDATA") or ROOT) / "CensorStudio"
PRESET_DIR = DATA_DIR / "presets"
SETTINGS = DATA_DIR / "settings.json"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

NO_JITTER = dict(scatter=0.0, size_jitter=0.0, hardness_jitter=0.0, angle_jitter=0.0,
                 roundness_jitter=0.0, flow_jitter=0.0)
NO_PRESSURE = dict(pressure_size=False, pressure_opacity=False, pressure_flow=False)
# 기본 브러시 5종
CLOUD = BrushPreset(name="구름 브러시")                              # 자동 검열용 (이전 버전의 구름 에어브러시 값 그대로)
ERASER_DEFAULT = BrushPreset(name="지우개", size=60.0, hardness=1.0, flow=1.0, spacing=0.06,
                             **NO_PRESSURE, **NO_JITTER)
SOFT_ERASER = replace(ERASER_DEFAULT, name="부드러운 지우개", pressure_opacity=True, pressure_flow=True)
AIRBRUSH = BrushPreset(name="에어브러시", hardness=0.0, flow=0.85, spacing=0.10, **NO_JITTER)
SHARP = BrushPreset(name="선명한 원형", hardness=1.0, flow=1.0, spacing=0.06, pressure_flow=False, **NO_JITTER)
DEFAULT_PRESETS = [CLOUD, ERASER_DEFAULT, SOFT_ERASER, AIRBRUSH, SHARP]
PRESETS_VERSION = 3
OLD_DEFAULT_NAMES = ["구름 에어브러시", "단단한 지우개", "부드러운 에어브러시"]   # 이전 버전 기본 프리셋

BRUSH_COLOR = QColor(64, 112, 205)    # 브러시로 쓰는 프리셋 표시색
ERASER_COLOR = QColor(205, 118, 40)   # 지우개로 쓰는 프리셋 표시색
SLOT_KO = {"brush": "브러시", "eraser": "지우개"}

# (속성, 이름, 최소, 최대, 배율, 단위)
BASIC = [("size", "크기", 1, 1000, 1, "px"), ("hardness", "경도", 0, 100, 100, "%"),
         ("opacity", "불투명도", 1, 100, 100, "%"), ("flow", "플로우", 1, 100, 100, "%"),
         ("spacing", "간격", 1, 200, 100, "%"), ("scatter", "분산", 0, 200, 100, "%"),
         ("angle", "각도", 0, 360, 1, "°"), ("roundness", "원형률", 5, 100, 100, "%")]
SHAPE = [("size_jitter", "크기 지터", 0, 100, 100, "%"), ("hardness_jitter", "경도 지터", 0, 100, 100, "%"),
         ("angle_jitter", "각도 지터", 0, 100, 100, "%"), ("roundness_jitter", "원형률 지터", 0, 100, 100, "%"),
         ("min_size", "필압 최소 크기", 1, 100, 100, "%")]
TRANSFER = [("opacity_jitter", "불투명도 지터", 0, 100, 100, "%"), ("flow_jitter", "플로우 지터", 0, 100, 100, "%")]


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "brush"


# ---- 마우스 휠로는 값이 바뀌지 않는 입력 위젯 (휠은 패널 스크롤로 넘긴다) ----
class NoWheelSlider(QSlider):
    def wheelEvent(self, ev) -> None:
        ev.ignore()


class NoWheelSpin(QSpinBox):
    def wheelEvent(self, ev) -> None:
        ev.ignore()


class NoWheelCombo(QComboBox):
    def wheelEvent(self, ev) -> None:
        ev.ignore()


class Param(QWidget):
    """이름 + 슬라이더 + 숫자 입력."""
    changed = Signal(float)

    def __init__(self, label: str, lo: int, hi: int, scale: float = 1, suffix: str = ""):
        super().__init__()
        self.scale = scale
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setFixedWidth(88)
        self.slider = NoWheelSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setMinimumWidth(40)
        self.spin = NoWheelSpin()
        self.spin.setRange(lo, hi)
        self.spin.setSuffix(suffix)
        self.spin.setFixedWidth(70)
        lay.addWidget(name)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def _from_slider(self, v: int) -> None:
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self.changed.emit(v / self.scale)

    def _from_spin(self, v: int) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self.changed.emit(v / self.scale)

    def set(self, value: float) -> None:
        v = int(round(value * self.scale))
        for wdg in (self.slider, self.spin):
            wdg.blockSignals(True)
            wdg.setValue(v)
            wdg.blockSignals(False)

    def value(self) -> float:
        return self.slider.value() / self.scale


class Worker(QThread):
    finished_ok = Signal(object, str, object)   # doc, mode, 결과
    failed = Signal(object, str)
    status = Signal(str)

    def __init__(self, models: Models, doc: Document, cfg: AutoConfig, mode: str):
        super().__init__()
        self.models, self.doc, self.cfg, self.mode = models, doc, cfg, mode

    def run(self) -> None:
        try:
            if self.mode == "auto":
                res = run_auto(self.models, self.doc.base, self.cfg, self.status.emit)
            else:
                self.status.emit("누끼 모델(SAM 2.1) 준비 중… (첫 실행은 모델을 내려받습니다)")
                res = self.models.sam.encode(self.doc.base)
            self.finished_ok.emit(self.doc, self.mode, res)
        except Exception as e:  # noqa: BLE001 — 사용자에게 그대로 보여 주고, 로그에도 남긴다
            try:
                import traceback
                with open(DATA_DIR / "error.log", "a", encoding="utf-8") as f:
                    f.write(traceback.format_exc() + chr(10))
            except OSError:
                pass
            self.failed.emit(self.doc, f"{type(e).__name__}: {e}")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Censor Studio")
        self.resize(1500, 950)
        self.setAcceptDrops(True)

        self.cfg = AutoConfig()
        self.effect = {"mode": "color", "color": [255, 255, 255], "strength": 16}
        self.view = {"start_original": False, "opacity": 0.85}
        self.auto_brush = ""                 # 자동 검열 전용 브러시 이름 ("" = 현재 브러시)
        self._saved: dict = {}
        self._load_settings()
        self.models = Models(self.cfg.use_gpu)
        self.docs: dict[str, Document] = {}
        self.doc: Document | None = None
        self.worker: Worker | None = None
        self._closing = False
        self._session: Session | None = None
        self._session_lock: QLockFile | None = None
        self.jobs: list[tuple[Document, str]] = []
        self.pending_click: tuple[Document, float, float, bool] | None = None
        self.params: dict[str, Param] = {}
        self.checks: dict[str, QCheckBox] = {}

        self.canvas = Canvas()
        self.setCentralWidget(self.canvas)
        self.canvas.samClicked.connect(self.on_sam_click)
        self.canvas.itemEraseClicked.connect(self.clear_item_at)
        self.canvas.zoomChanged.connect(lambda z: self.zoom_label.setText(f"{z * 100:.0f}%"))
        self.canvas.sizeChanged.connect(self.set_size)
        self.canvas.edited.connect(self._update_title)
        self.canvas.penInfo.connect(self.on_pen)
        self.wintab: Wintab | None = None

        self._ensure_presets()
        self._init_slots()
        self._build_actions()
        self._build_left()
        self._build_right()
        self._edge_timer = QTimer(self, interval=40)
        self._edge_timer.timeout.connect(self._reveal_fullscreen_tools)
        self.zoom_label = QLabel("100%")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(120)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.pen_label = QLabel("펜: 마우스")
        self.pen_label.setToolTip("획을 그을 때의 입력 방식과 필압. 펜으로 그어도 '마우스'로 나오면 필압이 들어오지 않는 것입니다.")
        self.statusBar().addPermanentWidget(self.pen_label)
        self.statusBar().addPermanentWidget(self.zoom_label)

        self._reload_presets()
        self._sync_brush_ui()
        self._release_focus()
        QApplication.instance().installEventFilter(self)
        QTimer.singleShot(0, self._refresh_preview)
        self.statusBar().showMessage("이미지를 끌어다 놓으면 자동 검열이 시작됩니다.")
        self._start_session()

    # ================= 설정 =================
    def _load_settings(self) -> None:
        if not DATA_DIR.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # 예전에 프로그램 폴더에 두던 설정·프리셋이 있으면 옮겨 온다.
            if (ROOT / "presets").is_dir():
                shutil.copytree(ROOT / "presets", PRESET_DIR, dirs_exist_ok=True)
            if (ROOT / "settings.json").is_file():
                shutil.copy(ROOT / "settings.json", SETTINGS)
        try:
            data = json.loads(SETTINGS.read_text("utf-8"))
        except Exception:
            return
        self._saved = data
        names = {f.name for f in fields(AutoConfig)}
        self.cfg = AutoConfig(**{k: v for k, v in data.get("auto", {}).items() if k in names})
        self.cfg.detector = "seg-medium"   # 탐지 모델은 '최신 · 균형' 하나만 쓴다
        if "detector" not in data.get("auto", {}):
            self.cfg.shape = "mask"   # 탐지 모델이 바뀌기 전 설정: 새 모델의 윤곽 칠을 기본으로 쓴다
        self.effect.update(data.get("effect", {}))
        self.view.update(data.get("view", {}))
        self.auto_brush = data.get("auto_brush", "")

    def _save_settings(self) -> None:
        data = {"presets_version": PRESETS_VERSION,
                "auto": asdict(self.cfg), "effect": self.effect, "view": self.view,
                "auto_brush": self.auto_brush,
                "brush": asdict(self.canvas.brush_preset), "eraser": asdict(self.canvas.eraser_preset)}
        SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")

    def _init_slots(self) -> None:
        """브러시와 지우개는 서로 다른 브러시를 쓰고, 마지막 상태를 각각 기억한다."""
        names = {f.name for f in fields(BrushPreset)}

        def restore(key: str, default: BrushPreset) -> BrushPreset:
            data = None if self._fresh_presets else self._saved.get(key)
            if isinstance(data, dict):
                try:
                    return BrushPreset(**{k: v for k, v in data.items() if k in names})
                except Exception:
                    pass
            return replace(default)

        self.canvas.brush_preset = restore("brush", CLOUD)
        if self._reset_cloud and self.canvas.brush_preset.name == CLOUD.name:
            self.canvas.brush_preset = replace(CLOUD)
        self.canvas.eraser_preset = restore("eraser", ERASER_DEFAULT)

    # ---- 지금 편집·선택 대상인 쪽 (지우개 도구면 지우개, 아니면 브러시) ----
    @property
    def slot(self) -> str:
        return "eraser" if self.canvas.tool == "eraser" else "brush"

    @property
    def preset(self) -> BrushPreset:
        return self.canvas.eraser_preset if self.slot == "eraser" else self.canvas.brush_preset

    def _set_slot_preset(self, preset: BrushPreset) -> None:
        if self.slot == "eraser":
            self.canvas.eraser_preset = preset
        else:
            self.canvas.brush_preset = preset
        self._sync_brush_ui()

    # ================= 앱 전역 키 처리 =================
    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:
        t = ev.type()
        if (t == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Escape
                and self.isFullScreen() and self.isActiveWindow() and QApplication.activeModalWidget() is None):
            self.toggle_fullscreen()
            return True
        if t in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and ev.key() == Qt.Key.Key_Space:
            # 어느 패널을 만지고 있었든 Space는 항상 작업영역의 '이동'으로 쓴다. (이름 입력 중일 때만 예외)
            if not self.isActiveWindow() or QApplication.activeModalWidget() is not None:
                return False
            focus = QApplication.focusWidget()
            if isinstance(focus, QLineEdit) and not isinstance(focus.parent(), QAbstractSpinBox):
                return False
            if not ev.isAutoRepeat():
                self.canvas.set_space(t == QEvent.Type.KeyPress)
            return True
        if t == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            # Tab = 다음 이미지, Shift+Tab = 이전 이미지
            if not self.isActiveWindow() or QApplication.activeModalWidget() is not None:
                return False
            focus = QApplication.focusWidget()
            if isinstance(focus, QLineEdit) and not isinstance(focus.parent(), QAbstractSpinBox):
                return False
            back = ev.key() == Qt.Key.Key_Backtab or bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self.step_file(-1 if back else 1)
            return True
        if t == QEvent.Type.ApplicationDeactivate:
            self.canvas.set_space(False)
        elif t == QEvent.Type.ApplicationActivate and self.wintab is not None:
            self.wintab.activate(True)
        return False

    def step_file(self, delta: int) -> None:
        n = self.file_list.count()
        row = self.file_list.currentRow() + delta
        if n == 0:
            return
        if not 0 <= row < n:
            self.statusBar().showMessage("마지막 이미지입니다." if delta > 0 else "첫 이미지입니다.")
            return
        self.file_list.clearSelection()
        self.file_list.setCurrentRow(row)
        self.canvas.setFocus()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self.wintab is None:
            # 드라이버에서 Windows Ink를 꺼 둔 태블릿도 필압이 먹도록 Wintab을 직접 연다.
            self.wintab = Wintab(int(self.winId()))
            if self.wintab.ok:
                self.canvas.pressure_fn = self.wintab.read
                self.pen_label.setText(f"펜: {self.wintab.device or 'Wintab'}")

    def on_pen(self, pressure: float, source: str) -> None:
        self.pen_label.setText(f"{source} · 필압 {pressure * 100:.0f}%")

    def _release_focus(self) -> None:
        """패널 위젯이 키보드 포커스를 붙잡지 않게 해서 단축키가 늘 작업영역으로 가게 한다."""
        for cls in (QPushButton, QSlider, QCheckBox, QListWidget, QComboBox, QTabBar):
            for w in self.findChildren(cls):
                w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for w in self.findChildren(QSpinBox):
            w.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
            w.editingFinished.connect(self.canvas.setFocus)
        self.canvas.setFocus()

    # ================= 도구 모음 =================
    def _act(self, text, slot, shortcut=None, checkable=False, tip=None) -> QAction:
        a = QAction(text, self)
        if shortcut:
            a.setShortcut(QKeySequence(shortcut))
        a.setToolTip(f"{tip or text} ({shortcut})" if shortcut else (tip or text))
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        if shortcut:
            self.addAction(a)
        return a

    def _build_actions(self) -> None:
        tb = self.toolbar = self.addToolBar("도구")
        tb.setMovable(False)
        tb.addAction(self._act("열기", self.open_dialog, "Ctrl+O"))
        tb.addAction(self._act("저장", self.save, "Ctrl+S", tip="원본 폴더 안 censored 폴더에 같은 이름으로 저장"))
        tb.addAction(self._act("일괄 저장", self.save_all, "Ctrl+Alt+S",
                               tip="작업한 이미지를 모두 각자의 censored 폴더에 저장"))
        tb.addAction(self._act("원본에 일괄 덮어쓰기", self.save_all_originals,
                               tip="작업한 이미지를 원본 파일에 덮어씁니다. 실행 전 확인합니다."))
        tb.addAction(self._act("다른 이름으로", self.save_as, "Ctrl+Shift+S"))
        tb.addSeparator()
        tb.addAction(self._act("실행취소", self.undo, "Ctrl+Z"))
        tb.addAction(self._act("다시실행", self.redo, "Ctrl+Shift+Z"))
        tb.addSeparator()

        self.tool_group = QActionGroup(self)
        self.tool_actions = {}
        for key, text, sc, tip in [("brush", "브러시", "B", "브러시로 칠하기"),
                                   ("eraser", "지우개", "E", "지우개 브러시로 지우기"),
                                   ("sam", "누끼 클릭", "W", "클릭한 대상을 누끼 따서 추가 · Alt+클릭/우클릭은 제거"),
                                   ("pan", "이동", "H", "화면 이동 (Space+드래그도 가능 · Ctrl+Space+드래그는 확대/축소)")]:
            a = self._act(text, lambda _=False, k=key: self.set_tool(k), sc, True, tip)
            self.tool_group.addAction(a)
            self.tool_actions[key] = a
            tb.addAction(a)
        self.tool_actions["brush"].setChecked(True)
        self.canvas.set_tool("brush")
        self.addAction(self._act("브러시 작게", lambda: self.set_size(self.preset.size / 1.12), "Z"))
        self.addAction(self._act("브러시 크게", lambda: self.set_size(self.preset.size * 1.12), "X"))
        tb.addSeparator()

        self.target_group = QActionGroup(self)
        self.target_actions = {}
        for key, text, tip in [("censor", "검열 레이어", "브러시·지우개·누끼 클릭이 검열 레이어에 적용됩니다"),
                               ("protect", "보호 레이어", "보호 레이어(누끼)에 칠한 곳은 검열이 뚫립니다")]:
            a = self._act(text, lambda _=False, k=key: self.set_target(k), None, True, tip)
            self.target_group.addAction(a)
            self.target_actions[key] = a
            tb.addAction(a)
        self.target_actions["censor"].setChecked(True)
        self.addAction(self._act("레이어 전환", self.toggle_target, "L"))
        tb.addSeparator()

        tb.addAction(self._act("자동 검열", self.rerun_auto, "Ctrl+R", tip="현재 이미지를 지우고 자동 검열 다시 실행"))
        tb.addSeparator()
        self.act_original = self._act("원본 보기", self.apply_view, "O", True,
                                      "검열을 반투명하게 해서 밑의 원본이 비쳐 보이게 합니다 (저장에는 영향 없음)")
        self.act_protect = self._act("보호 영역 표시", self.toggle_protect, "P", True)
        self.act_boxes = self._act("탐지 박스", self.toggle_boxes, "G", True)
        self.act_original.setChecked(bool(self.view["start_original"]))
        self.act_protect.setChecked(True)
        self.act_boxes.setChecked(True)
        for a in (self.act_original, self.act_protect, self.act_boxes):
            tb.addAction(a)
        tb.addAction(self._act("화면 맞춤", self.canvas.fit, "Ctrl+0"))
        tb.addSeparator()
        tb.addAction(self._act("◀ 이전 이미지", lambda: self.step_file(-1), None, tip="이전 이미지 (Shift+Tab)"))
        tb.addAction(self._act("다음 이미지 ▶", lambda: self.step_file(1), None, tip="다음 이미지 (Tab)"))
        tb.addAction(self._act("전체화면", self.toggle_fullscreen, "F11",
                               tip="전체화면에서 화면 가장자리로 마우스를 옮기면 도구가 나타납니다."))
        self.addAction(self._act("다음 항목", lambda: self.step_item(1), "PgDown"))
        self.addAction(self._act("이전 항목", lambda: self.step_item(-1), "PgUp"))

    def set_tool(self, key: str) -> None:
        self.canvas.set_tool(key)
        self.tool_actions[key].setChecked(True)
        self._sync_brush_ui()

    def toggle_fullscreen(self) -> None:
        panels = (self.left_dock, self.right_dock, self.toolbar, self.statusBar())
        if self.isFullScreen():
            self._edge_timer.stop()
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)
            for panel, visible in zip(panels, self._window_panels):
                panel.setVisible(visible)
            self.canvas.setFocus()
        else:
            self._window_panels = tuple(not panel.isHidden() for panel in panels)
            for panel in panels:
                panel.hide()
            self.showFullScreen()
            self._edge_timer.start()

    def _reveal_fullscreen_tools(self) -> None:
        pos = self.mapFromGlobal(QCursor.pos())
        inside = self.rect().contains(pos)
        for panel, edge in ((self.left_dock, pos.x() < 24),
                            (self.right_dock, pos.x() >= self.width() - 4),
                            (self.toolbar, pos.y() <= 3)):
            visible = inside and (edge or (panel.isVisible() and panel.geometry().contains(pos)))
            if visible != panel.isVisible():
                panel.setVisible(visible)

    # ================= 왼쪽: 파일 / 검수 / 자동 설정 =================
    def _build_left(self) -> None:
        box = QWidget()
        lay = QVBoxLayout(box)

        lay.addWidget(QLabel("파일"))
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(120)
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setToolTip("드래그하거나 Shift/Ctrl+클릭으로 여러 장을 선택할 수 있습니다")
        self.file_list.currentRowChanged.connect(self.on_file_row)
        lay.addWidget(self.file_list)
        row = QHBoxLayout()
        for text, slot, tip in [("선택 제거", self.remove_selected, "선택한 이미지를 작업 목록에서 뺍니다 (원본 파일은 지우지 않습니다)"),
                                ("선택 재검열", self.rerun_selected, "선택한 이미지의 칠을 지우고 자동 검열을 다시 돌립니다"),
                                ("목록 비우기", lambda: self.remove_files(self._file_paths()), "작업 목록의 모든 이미지를 뺍니다")]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)

        lay.addWidget(QLabel("검수 목록 (PgUp / PgDown)"))
        self.item_list = QListWidget()
        self.item_list.currentRowChanged.connect(self.on_item_row)
        lay.addWidget(self.item_list, 1)
        row = QHBoxLayout()
        for text, slot in [("◀ 이전", lambda: self.step_item(-1)), ("다음 ▶", lambda: self.step_item(1))]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        for text, slot, tip in [("다시 칠하기", self.repaint_item, "이 항목을 지우고 자동 검열 브러시로 다시 칠합니다"),
                                ("지우기", self.clear_item, "이 항목 영역의 칠을 지웁니다")]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)

        g = QGroupBox("자동 검열 설정")
        f = QVBoxLayout(g)
        self.chk_auto = QCheckBox("이미지를 놓으면 자동 검열")
        self.chk_auto.setChecked(self.cfg.auto_on_drop)
        self.chk_auto.toggled.connect(lambda v: setattr(self.cfg, "auto_on_drop", v))
        f.addWidget(self.chk_auto)
        form = QFormLayout()
        self.auto_brush_box = NoWheelCombo()
        self.auto_brush_box.setToolTip("자동 검열이 칠할 때 쓰는 브러시. 손으로 쓰는 브러시와 따로 정해 둘 수 있습니다.")
        self.auto_brush_box.activated.connect(self.on_auto_brush)
        form.addRow("자동 브러시", self.auto_brush_box)
        f.addLayout(form)
        # 부위별: 켜기 / 탐지 하한 / 영역 여유 / 브러시 크기
        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(3)
        cols = [("conf", "하한", 5, 90, "탐지 하한: 이 신뢰도 이상만 검열합니다. 낮출수록 더 많이 잡지만 오탐이 늘어납니다"),
                ("margin", "여유", 0, 60, "영역 여유: 탐지된 영역을 이만큼 넓혀서 칠합니다"),
                ("brush_pct", "브러시", 20, 150, "브러시 크기: 자동으로 칠할 때의 브러시 지름 (영역 짧은 변 대비)")]
        for ci, (_, title, _, _, tip) in enumerate(cols):
            head = QLabel(title)
            head.setToolTip(tip)
            head.setAlignment(Qt.AlignmentFlag.AlignCenter)
            grid.addWidget(head, 0, ci + 1)
        for ri, (key, text, default) in enumerate([("nipple", "유두", True), ("penis", "음경", True),
                                                   ("vagina", "음부", True), ("anus", "항문", True),
                                                   ("pubic", "음모", False)], start=1):
            c = QCheckBox(text)
            c.setChecked(self.cfg.classes.get(key, default))
            self.cfg.classes[key] = c.isChecked()
            c.toggled.connect(lambda v, k=key: self.cfg.classes.__setitem__(k, v))
            grid.addWidget(c, ri, 0)
            part = self.cfg.per_class.setdefault(key, {})
            for ci, (attr, _, lo, hi, tip) in enumerate(cols):
                part.setdefault(attr, getattr(self.cfg, attr))   # 처음엔 기존 공통값을 그대로 물려받는다
                sp = NoWheelSpin()
                sp.setRange(lo, hi)
                sp.setSuffix("%")
                sp.setToolTip(f"{text} · {tip}")
                sp.setValue(int(round(part[attr] * 100)))
                sp.valueChanged.connect(lambda v, k=key, a=attr: self.cfg.per_class[k].__setitem__(a, v / 100))
                grid.addWidget(sp, ri, ci + 1)
        grid.setColumnStretch(0, 1)
        f.addLayout(grid)
        form = QFormLayout()
        self.shape_box = NoWheelCombo()
        self.shape_box.addItem("부위 윤곽 (탐지 모델)", "mask")
        self.shape_box.addItem("타원 (탐지 박스 기준)", "ellipse")
        self.shape_box.addItem("부위 윤곽 (SAM 누끼)", "sam")
        self.shape_box.setToolTip("구형 탐지 모델은 윤곽을 주지 않아서, '부위 윤곽 (탐지 모델)'을 골라도 타원으로 칠합니다.")
        self.shape_box.setCurrentIndex(max(self.shape_box.findData(self.cfg.shape), 0))
        self.shape_box.currentIndexChanged.connect(lambda _: setattr(self.cfg, "shape", self.shape_box.currentData()))
        form.addRow("칠할 모양", self.shape_box)
        f.addLayout(form)
        self.chk_hands = QCheckBox("겹치는 손은 누끼 따서 남기기")
        self.chk_hands.setChecked(self.cfg.protect_hands)
        self.chk_hands.toggled.connect(lambda v: setattr(self.cfg, "protect_hands", v))
        f.addWidget(self.chk_hands)
        lay.addWidget(g)

        g = QGroupBox("원본 보기")
        f = QVBoxLayout(g)
        c = QCheckBox("시작할 때 원본 보기 켜기")
        c.setChecked(bool(self.view["start_original"]))
        c.toggled.connect(lambda v: self.view.__setitem__("start_original", v))
        f.addWidget(c)
        p = Param("검열 농도", 10, 100, 100, "%")
        p.setToolTip("원본 보기를 켰을 때 검열이 얼마나 진하게 보일지 (낮을수록 원본이 더 비칩니다)")
        p.set(self.view["opacity"])
        p.changed.connect(self.on_view_opacity)
        f.addWidget(p)
        lay.addWidget(g)

        dock = self.left_dock = QDockWidget("검수", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        scroll = self._fluid_scroll(box)
        scroll.setMinimumWidth(300)
        dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    # ================= 오른쪽: 브러시 목록 + 상세 설정 =================
    def _fluid_scroll(self, inner: QWidget) -> QScrollArea:
        """패널 폭에 맞춰 내용이 줄고 늘어나는 세로 스크롤 영역 (가로 스크롤·잘림 없음)."""
        inner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(inner)
        return scroll

    def _build_right(self) -> None:
        tabs = QTabWidget()

        # ---- 탭 1: 브러시 고르기 ----
        page = QWidget()
        lay = QVBoxLayout(page)
        self.slot_label = QLabel()
        self.slot_label.setWordWrap(True)
        lay.addWidget(self.slot_label)
        self.preset_list = QListWidget()
        self.preset_list.setToolTip("누르면 지금 도구(브러시 B / 지우개 E)가 쓸 브러시로 지정됩니다")
        self.preset_list.itemClicked.connect(self.on_preset_clicked)
        lay.addWidget(self.preset_list, 1)
        self.preview = QLabel()
        self.preview.setFixedHeight(64)
        self.preview.setMinimumWidth(40)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.preview)
        self.quick_size = Param("크기 (Z / X)", 1, 1000, 1, "px")
        self.quick_size.changed.connect(self.set_size)
        lay.addWidget(self.quick_size)

        g = QGroupBox("검열 표현")
        v = QVBoxLayout(g)
        row = QHBoxLayout()
        self.effect_box = NoWheelCombo()
        for text, key in [("단색", "color"), ("모자이크", "mosaic"), ("블러", "blur")]:
            self.effect_box.addItem(text, key)
        self.effect_box.setCurrentIndex(["color", "mosaic", "blur"].index(self.effect["mode"]))
        self.effect_box.currentIndexChanged.connect(self.on_effect)
        self.color_btn = QPushButton()
        self.color_btn.setFixedWidth(48)
        self.color_btn.clicked.connect(self.pick_color)
        row.addWidget(self.effect_box, 1)
        row.addWidget(self.color_btn)
        v.addLayout(row)
        self.strength = Param("강도", 2, 80, 1, "px")
        self.strength.set(self.effect["strength"])
        self.strength.changed.connect(self.on_effect)
        v.addWidget(self.strength)
        lay.addWidget(g)
        self._sync_effect_ui()
        tabs.addTab(page, "브러시")

        # ---- 탭 2: 상세 설정 ----
        box = QWidget()
        lay = QVBoxLayout(box)
        self.edit_label = QLabel()
        self.edit_label.setWordWrap(True)
        lay.addWidget(self.edit_label)
        row = QHBoxLayout()
        for text, slot, tip in [("저장", self.preset_save, "지금 값을 이 프리셋에 덮어씁니다"),
                                ("새 이름으로", self.preset_save_as, "지금 값을 새 프리셋으로 저장합니다"),
                                ("삭제", self.preset_delete, "이 프리셋을 삭제합니다")]:
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        row = QHBoxLayout()
        for text, slot in [("가져오기", self.preset_import), ("내보내기", self.preset_export)]:
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)

        def group(title: str, spec, checks=()):
            gb = QGroupBox(title)
            gl = QVBoxLayout(gb)
            for attr, label, lo, hi, scale, suffix in spec:
                p = Param(label, lo, hi, scale, suffix)
                p.changed.connect(lambda val, a=attr: self.on_param(a, val))
                self.params[attr] = p
                gl.addWidget(p)
            for attr, label in checks:
                c = QCheckBox(label)
                c.toggled.connect(lambda val, a=attr: self.on_param(a, val))
                self.checks[attr] = c
                gl.addWidget(c)
            lay.addWidget(gb)

        group("기본", BASIC)
        group("모양", SHAPE, [("pressure_size", "필압으로 크기 조절")])
        group("전송", TRANSFER, [("pressure_opacity", "필압으로 불투명도 조절"),
                                ("pressure_flow", "필압으로 플로우 조절")])
        g = QGroupBox("브러시 모양")
        row = QHBoxLayout(g)
        self.tip_label = QLabel("원형")
        b1 = QPushButton("이미지로 지정")
        b1.setToolTip("이미지를 브러시 모양으로 씁니다 (투명도가 있으면 투명도, 없으면 어두운 부분이 진한 부분)")
        b1.clicked.connect(self.tip_load)
        b2 = QPushButton("원형으로")
        b2.clicked.connect(self.tip_clear)
        row.addWidget(self.tip_label, 1)
        row.addWidget(b1)
        row.addWidget(b2)
        lay.addWidget(g)
        lay.addStretch(1)
        tabs.addTab(self._fluid_scroll(box), "상세 설정")

        tabs.setMinimumWidth(250)
        dock = self.right_dock = QDockWidget("브러시", self)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable)
        dock.setWidget(tabs)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self.resizeDocks([dock], [340], Qt.Orientation.Horizontal)

    # ================= 프리셋 =================
    def _ensure_presets(self) -> None:
        PRESET_DIR.mkdir(parents=True, exist_ok=True)
        version = int(self._saved.get("presets_version", 0))
        self._fresh_presets = version < 2
        # 버전 2 → 3: 구름 브러시만 이전 값으로 되돌린다. 나머지 프리셋과 설정은 건드리지 않는다.
        self._reset_cloud = version == 2
        if self._reset_cloud:
            CLOUD.save(PRESET_DIR / f"{safe_name(CLOUD.name)}.json")
        if self._fresh_presets:
            # 기본 브러시 구성이 바뀌었다: 예전 기본 프리셋은 치우고 새 5종을 써 넣는다. (직접 만든 것은 그대로 둔다)
            for name in OLD_DEFAULT_NAMES:
                (PRESET_DIR / f"{name}.json").unlink(missing_ok=True)
            for p in DEFAULT_PRESETS:
                p.save(PRESET_DIR / f"{safe_name(p.name)}.json")
            self.auto_brush = CLOUD.name
        elif not any(PRESET_DIR.glob("*.json")):
            for p in DEFAULT_PRESETS:
                p.save(PRESET_DIR / f"{safe_name(p.name)}.json")

    def _preset_names(self) -> list[str]:
        return sorted(p.stem for p in PRESET_DIR.glob("*.json"))

    def _reload_presets(self) -> None:
        names = self._preset_names()
        self.preset_list.clear()
        for n in names:
            self.preset_list.addItem(QListWidgetItem(n))
        self.auto_brush_box.clear()
        self.auto_brush_box.addItem("현재 브러시와 같게", "")
        for n in names:
            self.auto_brush_box.addItem(n, n)
        if self.auto_brush not in names:
            self.auto_brush = ""
        self.auto_brush_box.setCurrentIndex(max(self.auto_brush_box.findData(self.auto_brush), 0))
        self._mark_presets()

    def _mark_presets(self) -> None:
        """브러시로 쓰는 프리셋과 지우개로 쓰는 프리셋을 서로 다른 색으로 표시한다."""
        b, e = self.canvas.brush_preset.name, self.canvas.eraser_preset.name
        for i in range(self.preset_list.count()):
            it = self.preset_list.item(i)
            name = it.data(Qt.ItemDataRole.UserRole) or it.text()
            it.setData(Qt.ItemDataRole.UserRole, name)
            tags = [SLOT_KO[k] for k, n in (("brush", b), ("eraser", e)) if n == name]
            it.setText(f"{name}    ← {' · '.join(tags)}" if tags else name)
            if name == b:
                it.setBackground(QBrush(BRUSH_COLOR))
            elif name == e:
                it.setBackground(QBrush(ERASER_COLOR))
            else:
                it.setBackground(QBrush(Qt.BrushStyle.NoBrush))
            it.setForeground(QBrush(QColor(255, 255, 255) if tags else QColor(225, 225, 228)))
            f = it.font()
            f.setBold(name == self.preset.name)
            it.setFont(f)

    def _sync_brush_ui(self) -> None:
        """지금 도구(브러시/지우개)의 브러시를 패널에 반영한다."""
        if not hasattr(self, "tip_label"):
            return
        preset, slot = self.preset, self.slot
        col = (ERASER_COLOR if slot == "eraser" else BRUSH_COLOR).name()
        other = "eraser" if slot == "brush" else "brush"
        other_name = (self.canvas.eraser_preset if other == "eraser" else self.canvas.brush_preset).name
        self.slot_label.setText(
            f"<b style='color:{col}'>■ {SLOT_KO[slot]}</b> 선택 중: <b>{preset.name}</b><br>"
            f"<span style='color:#999'>{SLOT_KO[other]}: {other_name} · B/E로 전환</span>")
        self.edit_label.setText(f"<b style='color:{col}'>■ {SLOT_KO[slot]}</b> 편집 중: <b>{preset.name}</b>")
        for attr, p in self.params.items():
            p.set(getattr(preset, attr))
        for attr, c in self.checks.items():
            c.blockSignals(True)
            c.setChecked(getattr(preset, attr))
            c.blockSignals(False)
        self.quick_size.set(preset.size)
        self.tip_label.setText("사용자 지정 이미지" if preset.tip_png_b64 else "원형")
        self._mark_presets()
        self._refresh_preview()
        self.canvas.update()

    def on_preset_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        try:
            preset = BrushPreset.load(PRESET_DIR / f"{name}.json")
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"프리셋을 읽지 못했습니다: {e}")
            return
        preset.name = name
        self._set_slot_preset(preset)
        self.preset_list.clearSelection()
        self.statusBar().showMessage(f"{SLOT_KO[self.slot]} → '{name}'")

    def on_auto_brush(self, _=None) -> None:
        self.auto_brush = self.auto_brush_box.currentData() or ""

    def auto_preset(self) -> BrushPreset:
        if self.auto_brush:
            try:
                return BrushPreset.load(PRESET_DIR / f"{self.auto_brush}.json")
            except Exception:
                pass
        return self.canvas.brush_preset

    def on_param(self, attr: str, value) -> None:
        setattr(self.preset, attr, value)
        if attr == "size":
            self.quick_size.set(value)
        self._refresh_preview()
        self.canvas.update()

    def set_size(self, size: float) -> None:
        size = min(max(float(size), 1.0), 1000.0)
        self.preset.size = size
        self.params["size"].set(size)
        self.quick_size.set(size)
        self._refresh_preview()
        self.canvas.update()

    def _refresh_preview(self) -> None:
        w = max(self.preview.width(), 120)
        layer = render_preview(self.preset, w, 64)
        img = np.ascontiguousarray((45 + layer * 210).astype(np.uint8))
        q = QImage(img.data, w, 64, w, QImage.Format.Format_Grayscale8)
        self.preview.setPixmap(QPixmap.fromImage(q.copy()))

    def preset_save(self) -> None:
        name = self.preset.name
        self.preset.save(PRESET_DIR / f"{safe_name(name)}.json")
        self._reload_presets()
        self.statusBar().showMessage(f"프리셋 '{name}' 저장됨")

    def preset_save_as(self) -> None:
        name, ok = QInputDialog.getText(self, "새 프리셋", "프리셋 이름:")
        name = safe_name(name) if ok and name.strip() else ""
        if not name:
            return
        preset = replace(self.preset, name=name)
        preset.save(PRESET_DIR / f"{name}.json")
        self._reload_presets()
        self._set_slot_preset(preset)

    def preset_delete(self) -> None:
        name = self.preset.name
        path = PRESET_DIR / f"{safe_name(name)}.json"
        if not path.exists():
            self.statusBar().showMessage("저장된 프리셋이 아닙니다.")
            return
        if QMessageBox.question(self, "프리셋 삭제", f"'{name}' 프리셋 파일을 삭제할까요?\n"
                                "(지금 쓰고 있는 값은 그대로 남습니다)") != QMessageBox.StandardButton.Yes:
            return
        path.unlink(missing_ok=True)
        self._reload_presets()

    def preset_import(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "프리셋 가져오기", "", "브러시 프리셋 (*.json)")
        count = 0
        for f in files:
            try:
                preset = BrushPreset.load(Path(f))
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "가져오기 실패", f"{Path(f).name}: {e}")
                continue
            preset.save(PRESET_DIR / f"{safe_name(preset.name)}.json")
            count += 1
        if count:
            self._reload_presets()
            self.statusBar().showMessage(f"프리셋 {count}개 가져옴")

    def preset_export(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "프리셋 내보내기", f"{safe_name(self.preset.name)}.json",
                                           "브러시 프리셋 (*.json)")
        if f:
            self.preset.save(Path(f))
            self.statusBar().showMessage(f"내보냄: {f}")

    def tip_load(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "브러시 모양 이미지", "", "이미지 (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not f:
            return
        try:
            self.preset.set_tip_from_image(Path(f))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "브러시 모양", str(e))
            return
        self._sync_brush_ui()

    def tip_clear(self) -> None:
        self.preset.tip_png_b64 = ""
        self._sync_brush_ui()

    # ================= 검열 표현 / 보기 =================
    def _sync_effect_ui(self) -> None:
        c = self.effect["color"]
        self.color_btn.setStyleSheet(f"background: rgb({c[0]},{c[1]},{c[2]}); border: 1px solid #888;")
        self.color_btn.setEnabled(self.effect["mode"] == "color")
        self.strength.setEnabled(self.effect["mode"] != "color")

    def _apply_effect(self) -> None:
        if self.doc:
            self.doc.set_effect(self.effect["mode"], tuple(self.effect["color"]), int(self.effect["strength"]))
            self.canvas.update()

    def on_effect(self, *_) -> None:
        self.effect["mode"] = self.effect_box.currentData()
        self.effect["strength"] = int(self.strength.value())
        self._sync_effect_ui()
        self._apply_effect()

    def pick_color(self) -> None:
        c = QColorDialog.getColor(QColor(*self.effect["color"]), self, "검열 색")
        if c.isValid():
            self.effect["color"] = [c.red(), c.green(), c.blue()]
            self._sync_effect_ui()
            self._apply_effect()

    def apply_view(self, *_) -> None:
        """원본 보기: 검열을 반투명하게 보여 밑그림을 보면서 다듬을 수 있게 한다."""
        if self.doc:
            self.doc.set_view_opacity(float(self.view["opacity"]) if self.act_original.isChecked() else 1.0)
            self.canvas.update()

    def on_view_opacity(self, value: float) -> None:
        self.view["opacity"] = value
        self.apply_view()

    def set_target(self, key: str) -> None:
        self.canvas.target = key
        self.target_actions[key].setChecked(True)
        if key == "protect" and not self.act_protect.isChecked():
            self.act_protect.setChecked(True)
            self.toggle_protect()

    def toggle_target(self) -> None:
        self.set_target("protect" if self.canvas.target == "censor" else "censor")

    def toggle_protect(self) -> None:
        self.canvas.show_protect = self.act_protect.isChecked()
        self.canvas.update()

    def toggle_boxes(self) -> None:
        self.canvas.show_boxes = self.act_boxes.isChecked()
        self.canvas.update()

    # ================= 파일 =================
    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = []
        for u in ev.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_dir():
                paths += sorted(str(x) for x in p.iterdir() if x.suffix.lower() in IMAGE_EXT)
            elif p.suffix.lower() in IMAGE_EXT:
                paths.append(str(p))
        self.add_files(paths)

    def open_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "이미지 열기", "", "이미지 (*.png *.jpg *.jpeg *.webp *.bmp)")
        self.add_files(files)

    def _file_paths(self) -> list[str]:
        return [self.file_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.file_list.count())]

    def _add_file_row(self, path: str) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(6, 0, 2, 0)
        lay.setSpacing(4)
        name = QLabel(Path(path).name)
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        name.setMinimumWidth(10)
        name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        btn = QToolButton()
        btn.setText("✕")
        btn.setAutoRaise(True)
        btn.setFixedSize(22, 20)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setToolTip("작업 목록에서 빼기 (원본 파일은 지우지 않습니다)")
        btn.clicked.connect(lambda _=False, p=path: self.remove_files([p]))
        lay.addWidget(name, 1)
        lay.addWidget(btn)
        row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        item.setSizeHint(QSize(0, 24))
        self.file_list.addItem(item)
        self.file_list.setItemWidget(item, row)

    def add_files(self, paths: list[str]) -> None:
        existing = self._file_paths()
        first = None
        for p in paths:
            if p not in existing:
                self._add_file_row(p)
                existing.append(p)
            if first is None:
                first = existing.index(p)
        if first is not None:
            self.file_list.clearSelection()
            self.file_list.setCurrentRow(first)

    def _get_doc(self, path: str) -> Document | None:
        doc = self.docs.get(path)
        if doc is None:
            try:
                doc = Document(path)
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "열기 실패", str(e))
                return None
            self.docs[path] = doc
        return doc

    def on_file_row(self, row: int) -> None:
        if row < 0:
            self.doc = None
            self.canvas.set_document(None)
            self._refresh_items()
            self.setWindowTitle("Censor Studio")
            return
        doc = self._get_doc(self.file_list.item(row).data(Qt.ItemDataRole.UserRole))
        if doc is None:
            return
        self.doc = doc
        self._apply_effect()
        self.apply_view()
        self.canvas.set_document(doc)   # 새 이미지는 항상 화면 맞춤으로 보여 준다
        self._refresh_items()
        self._update_title()
        if self.cfg.auto_on_drop and not doc.auto_done:
            self.enqueue(doc, "auto")

    def _selected_paths(self) -> list[str]:
        paths = [it.data(Qt.ItemDataRole.UserRole) for it in self.file_list.selectedItems()]
        if not paths and self.file_list.currentItem():
            paths = [self.file_list.currentItem().data(Qt.ItemDataRole.UserRole)]
        return paths

    def remove_selected(self) -> None:
        self.remove_files(self._selected_paths())

    def remove_files(self, paths: list[str]) -> None:
        """작업 목록에서만 뺀다. 디스크의 원본 파일은 건드리지 않는다."""
        if not paths:
            return
        unsaved = [p for p in paths if p in self.docs and self.docs[p].dirty]
        if unsaved:
            r = QMessageBox.question(self, "목록에서 제거",
                                     f"저장하지 않은 작업이 있는 이미지가 {len(unsaved)}장 포함되어 있습니다.\n"
                                     "작업 내용을 버리고 목록에서 뺄까요? (원본 파일은 그대로 남습니다)")
            if r != QMessageBox.StandardButton.Yes:
                return
        gone = set(paths)
        removed_docs = [self.docs.pop(p) for p in paths if p in self.docs]
        self.jobs = [(d, m) for d, m in self.jobs if all(d is not r for r in removed_docs)]
        self.file_list.blockSignals(True)
        cur = self.file_list.currentRow()
        for i in range(self.file_list.count() - 1, -1, -1):
            if self.file_list.item(i).data(Qt.ItemDataRole.UserRole) in gone:
                self.file_list.takeItem(i)
                if i < cur:
                    cur -= 1
        n = self.file_list.count()
        self.file_list.clearSelection()
        self.file_list.setCurrentRow(-1)
        self.file_list.blockSignals(False)
        if n:
            self.file_list.setCurrentRow(min(max(cur, 0), n - 1))
        else:
            self.on_file_row(-1)
        self.statusBar().showMessage(f"{len(gone)}장을 작업 목록에서 뺐습니다. (원본 파일은 그대로입니다)")

    def rerun_selected(self) -> None:
        count = 0
        for p in self._selected_paths():
            doc = self._get_doc(p)
            if doc is not None:
                doc.auto_done = False
                self.enqueue(doc, "auto")
                count += 1
        if count:
            self.statusBar().showMessage(f"{count}장 자동 검열 대기 중…")

    def _update_title(self) -> None:
        if self.doc:
            mark = " *" if self.doc.dirty else ""
            self.setWindowTitle(f"{Path(self.doc.path).name}{mark} — Censor Studio")

    @staticmethod
    def _default_out(doc: Document) -> Path:
        """원본이 있는 폴더 안의 censored 폴더에, 원본과 똑같은 파일 이름으로."""
        p = Path(doc.path)
        return p.parent / "censored" / p.name

    def save(self) -> None:
        if self.doc:
            out = self._default_out(self.doc)
            self._save_to(str(out), advance=True)

    def save_all(self) -> None:
        self._save_batch(overwrite=False)

    def save_all_originals(self) -> None:
        self._save_batch(overwrite=True)

    def _save_batch(self, overwrite: bool) -> None:
        title = "원본에 일괄 덮어쓰기" if overwrite else "일괄 저장"
        if self.worker is not None or self.jobs:
            QMessageBox.information(self, title, "자동 검열이 아직 진행 중입니다. 끝난 뒤 다시 눌러 주세요.")
            return
        paths = self._file_paths()
        docs = [self.docs[p] for p in paths
                if p in self.docs and (self.docs[p].auto_done or self.docs[p].undo_stack or self.docs[p].dirty)]
        skipped = len(paths) - len(docs)
        if overwrite:
            if not docs:
                QMessageBox.information(self, title, "덮어쓸 작업 이미지가 없습니다.")
                return
            buttons = QMessageBox.StandardButton
            result = QMessageBox.warning(self, title,
                                         f"이미지 {len(docs)}장의 원본을 덮어씁니다. 원본 백업은 생성하지 않습니다.\n"
                                         f"미작업 이미지 {skipped}장은 건너뜁니다. 계속할까요?",
                                         buttons.Yes | buttons.No, buttons.No)
            if result != buttons.Yes:
                return
        saved, failed = 0, []
        for doc in docs:
            try:
                doc.set_effect(self.effect["mode"], tuple(self.effect["color"]), int(self.effect["strength"]))
                if overwrite:
                    out = Path(doc.path)
                    if not out.is_file():
                        raise FileNotFoundError("원본 파일을 찾을 수 없습니다.")
                else:
                    out = self._default_out(doc)
                    out.parent.mkdir(exist_ok=True)
                doc.save(str(out))
                saved += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f"{Path(doc.path).name}: {e}")
        self._update_title()
        msg = f"{title}: {saved}장 저장"
        if skipped:
            msg += f", {skipped}장 건너뜀"
        self.statusBar().showMessage(msg)
        if failed:
            QMessageBox.warning(self, title, f"{msg}\n\n저장하지 못한 이미지:\n" + "\n".join(failed))

    def save_as(self) -> None:
        if not self.doc:
            return
        f, _ = QFileDialog.getSaveFileName(self, "다른 이름으로 저장", str(self._default_out(self.doc)),
                                           "PNG (*.png);;JPEG (*.jpg);;WebP (*.webp)")
        if f:
            self._save_to(f, advance=False)

    def _save_to(self, path: str, advance: bool) -> None:
        try:
            if advance:
                Path(path).parent.mkdir(exist_ok=True)
            self.doc.save(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "저장 실패", str(e))
            return
        self._update_title()
        self.statusBar().showMessage(f"저장됨: {path}")
        row = self.file_list.currentRow()
        if advance and row + 1 < self.file_list.count():
            self.file_list.clearSelection()
            self.file_list.setCurrentRow(row + 1)

    def _start_session(self) -> None:
        directory = Path(os.environ.get("CENSOR_SESSION_DIR") or DATA_DIR / "session")
        try:
            directory.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(directory / "session.lock"))
            if not lock.tryLock(0):
                self.statusBar().showMessage("다른 창이 작업 보존 파일을 사용 중입니다.")
                return
            self._session_lock = lock
            session = Session(directory)
            data = session.load()
            self._session = session
        except Exception as exc:
            self.statusBar().showMessage(f"작업 상태를 복원하지 못했습니다. {exc}")
            return
        if data is None:
            return
        self.docs = data["docs"]
        self.effect.update(data.get("effect", {}))
        self.view.update(data.get("view", {}))
        self._sync_effect_ui()
        paths = data["paths"]
        self.file_list.blockSignals(True)
        for path in paths:
            self._add_file_row(path)
        self.file_list.blockSignals(False)
        current = data.get("current")
        if current in paths:
            auto = self.cfg.auto_on_drop
            self.cfg.auto_on_drop = False
            try:
                self.file_list.setCurrentRow(paths.index(current))
            finally:
                self.cfg.auto_on_drop = auto
        self.set_tool(data.get("tool", "brush"))
        self.set_target(data.get("target", "censor"))
        self.item_list.setCurrentRow(data.get("item_row", -1))
        self.jobs = [(self.docs[path], mode) for path, mode in data.get("jobs", [])
                     if path in self.docs and mode == "auto"]
        self.statusBar().showMessage(f"작업 이미지 {len(paths)}장, 자동 검열 대기 {len(self.jobs)}장 복원")
        QTimer.singleShot(0, self._next_job)

    def closeEvent(self, ev) -> None:
        if self.worker is not None:
            self._closing = True
            self.setEnabled(False)
            self.statusBar().showMessage("현재 이미지 처리를 마친 뒤 작업 상태를 보존하고 종료합니다.")
            ev.ignore()
            return
        try:
            if self._session is not None:
                self.canvas.finish_stroke()
                paths = self._file_paths()
                state = {"paths": paths, "current": self.doc.path if self.doc else None,
                         "item_row": self.item_list.currentRow(), "effect": self.effect, "view": self.view,
                         "tool": self.canvas.tool, "target": self.canvas.target,
                         "jobs": [(doc.path, mode) for doc, mode in self.jobs if mode == "auto"]}
                self._session.save(state, (self.docs[p] for p in paths if p in self.docs))
            elif any(doc.dirty for doc in self.docs.values()):
                self.setEnabled(True)
                if QMessageBox.question(self, "종료", "작업 상태를 보존할 수 없습니다. 그래도 종료할까요?") != QMessageBox.StandardButton.Yes:
                    self._closing = False
                    QTimer.singleShot(0, self._next_job)
                    ev.ignore()
                    return
            if self._session_lock is not None:
                self._save_settings()
        except Exception as exc:
            self._closing = False
            self.setEnabled(True)
            QMessageBox.warning(self, "작업 보존 실패", f"작업 상태를 저장하지 못했습니다.\n{exc}")
            QTimer.singleShot(0, self._next_job)
            ev.ignore()
            return
        if self.wintab is not None:
            self.wintab.close()
        if self._session_lock is not None:
            self._session_lock.unlock()
        ev.accept()

    # ================= 실행취소 =================
    def undo(self) -> None:
        if self.doc:
            self.canvas.update_image_rect(self.doc.undo())
            self._update_title()

    def redo(self) -> None:
        if self.doc:
            self.canvas.update_image_rect(self.doc.redo())
            self._update_title()

    # ================= 자동 검열 =================
    def enqueue(self, doc: Document, mode: str) -> None:
        if any(d is doc and m == mode for d, m in self.jobs):
            return
        if self.worker and self.worker.doc is doc and self.worker.mode == mode:
            return
        self.jobs.append((doc, mode))
        self._next_job()

    def _next_job(self) -> None:
        if self._closing or self.worker is not None or not self.jobs:
            return
        doc, mode = self.jobs.pop(0)
        self.worker = Worker(self.models, doc, replace(self.cfg, classes=dict(self.cfg.classes)), mode)
        self.worker.status.connect(self.statusBar().showMessage)
        self.worker.finished_ok.connect(self.on_job_done)
        self.worker.failed.connect(self.on_job_failed)
        self.worker.finished.connect(self._job_cleanup)
        self.progress.show()
        self.worker.start()

    def _job_cleanup(self) -> None:
        self.worker.deleteLater()
        self.worker = None
        self.progress.setVisible(bool(self.jobs))
        QTimer.singleShot(0, self.close if self._closing else self._next_job)

    def on_job_failed(self, doc: Document, msg: str) -> None:
        self.pending_click = None
        self.statusBar().showMessage(f"실패: {msg}")
        if not self._closing and self.docs.get(doc.path) is doc:
            QMessageBox.warning(self, "자동 처리 실패", msg)

    def on_job_done(self, doc: Document, mode: str, res) -> None:
        if mode == "encode":
            doc.sam_emb = res
            self.statusBar().showMessage("누끼 모델 준비 완료")
            if self.pending_click and self.pending_click[0] is doc:
                _, x, y, add = self.pending_click
                self.pending_click = None
                if self.doc is doc:
                    self.on_sam_click(x, y, add)
            return
        if all(d is not doc for d in self.docs.values()):
            return   # 그 사이 목록에서 빠진 이미지
        items, emb = res
        if emb is not None:
            doc.sam_emb = emb
        patches = [doc.clear_rect("censor", (0, 0, doc.w, doc.h)), doc.clear_rect("protect", (0, 0, doc.w, doc.h))]
        preset = self.auto_preset()
        for it in items:
            patches.append(doc.paint_item(it, preset))
        doc.push(patches)
        doc.items = items
        doc.auto_done = True
        n_c = sum(1 for it in items if it.kind == "censor")
        n_h = len(items) - n_c
        if doc is self.doc:
            self._refresh_items()
            self.canvas.update()
            self._update_title()
        name = Path(doc.path).name
        self.statusBar().showMessage(f"{name}: 검열 {n_c}곳, 손 누끼 {n_h}곳" if items
                                     else f"{name}: 탐지된 부위가 없습니다. 필요하면 직접 칠하세요.")

    def rerun_auto(self) -> None:
        if self.doc:
            self.doc.auto_done = False
            self.enqueue(self.doc, "auto")

    # ================= 검수 목록 =================
    def _refresh_items(self) -> None:
        self.item_list.blockSignals(True)
        self.item_list.clear()
        if self.doc:
            for i, it in enumerate(self.doc.items):
                self.item_list.addItem(f"{i + 1}. {it.title}")
        self.item_list.blockSignals(False)
        self.canvas.current_item = -1

    def on_item_row(self, row: int) -> None:
        self.canvas.current_item = row
        if self.doc and 0 <= row < len(self.doc.items):
            self.canvas.focus_rect(self.doc.items[row].box)

    def step_item(self, delta: int) -> None:
        n = self.item_list.count()
        if n:
            self.item_list.setCurrentRow((self.item_list.currentRow() + delta) % n)

    def _current_item(self) -> Item | None:
        row = self.item_list.currentRow()
        if self.doc and 0 <= row < len(self.doc.items):
            return self.doc.items[row]
        return None

    def clear_item(self) -> None:
        it = self._current_item()
        if it:
            self.doc.push([self.doc.clear_item(it)])
            self.canvas.update()
            self._update_title()

    def clear_item_at(self, x: float, y: float) -> None:
        doc = self.doc
        if doc is None or not (0 <= x < doc.w and 0 <= y < doc.h):
            return
        x, y = int(x), int(y)
        if doc.censor[y, x] * (1.0 - doc.protect[y, x]) <= 0:
            return
        hits = []
        for index, item in enumerate(doc.items):
            if item.kind != "censor" or not item.enabled or item.footprint is None:
                continue
            (x0, y0, x1, y1), alpha = item.footprint
            if x0 <= x < x1 and y0 <= y < y1 and alpha[y - y0, x - x0] > 0:
                ox, oy = item.offset
                rh, rw = item.region.shape
                core = ox <= x < ox + rw and oy <= y < oy + rh and item.region[y - oy, x - ox]
                hits.append((not core, (x1 - x0) * (y1 - y0), index))
        if hits:
            item = doc.items[min(hits)[2]]
            doc.push([doc.clear_item(item)])
            self.canvas.update()
            self._update_title()
            self.statusBar().showMessage(f"{item.title} 검열을 지웠습니다.")

    def repaint_item(self) -> None:
        it = self._current_item()
        if it:
            self.doc.push([self.doc.clear_item(it), self.doc.paint_item(it, self.auto_preset())])
            self.canvas.update()
            self._update_title()

    # ================= 누끼 클릭 =================
    def on_sam_click(self, x: float, y: float, add: bool) -> None:
        doc = self.doc
        if not doc:
            return
        if doc.sam_emb is None:
            self.pending_click = (doc, x, y, add)
            self.enqueue(doc, "encode")
            return
        mask = self.models.sam.predict(doc.sam_emb, points=[(x, y, 1)])
        # 누끼에 섞인 자잘한 점·구멍을 정리한다.
        m8 = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        mask = cv2.morphologyEx(m8, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8)).astype(bool)
        if not mask.any():
            self.statusBar().showMessage("이 지점에서는 누끼를 따지 못했습니다.")
            return
        layer = self.canvas.target
        if not add:
            patch = doc.set_mask(layer, mask, 0.0)
        elif layer == "protect":
            patch = doc.set_mask(layer, mask, 1.0)
        else:
            patch = doc.paint_mask(layer, mask, self.canvas.brush_preset)
        doc.push([patch])
        self.canvas.update()
        self._update_title()
        what = "보호 레이어" if layer == "protect" else "검열 레이어"
        self.statusBar().showMessage(f"누끼 → {what}{'에 추가' if add else '에서 제거'}")


def dark_palette() -> QPalette:
    p = QPalette()
    base, panel, text = QColor(32, 32, 36), QColor(46, 46, 52), QColor(225, 225, 228)
    for role, col in [(QPalette.ColorRole.Window, panel), (QPalette.ColorRole.WindowText, text),
                      (QPalette.ColorRole.Base, base), (QPalette.ColorRole.AlternateBase, panel),
                      (QPalette.ColorRole.Text, text), (QPalette.ColorRole.Button, panel),
                      (QPalette.ColorRole.ButtonText, text), (QPalette.ColorRole.ToolTipBase, base),
                      (QPalette.ColorRole.ToolTipText, text), (QPalette.ColorRole.Highlight, QColor(70, 120, 210)),
                      (QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))]:
        p.setColor(role, col)
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(120, 120, 125))
    return p


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Censor Studio")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    icon = Path(getattr(sys, "_MEIPASS", ROOT)) / "icon.ico"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    win.add_files([a for a in sys.argv[1:] if Path(a).suffix.lower() in IMAGE_EXT])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
