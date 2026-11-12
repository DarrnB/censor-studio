"""캔버스: 확대/이동, 마우스·펜(필압) 입력, 브러시 획."""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPen, QPointingDevice
from PySide6.QtWidgets import QWidget

from .brush import BrushPreset
from .document import Document


class Canvas(QWidget):
    samClicked = Signal(float, float, bool)   # 이미지 좌표, 추가(True)/제거(False)
    itemEraseClicked = Signal(float, float)
    zoomChanged = Signal(float)
    sizeChanged = Signal(float)
    edited = Signal()
    penInfo = Signal(float, str)             # 획을 긋는 동안의 필압과 입력 방식

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setTabletTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.doc: Document | None = None
        self.brush_preset = BrushPreset()
        self.eraser_preset = BrushPreset()
        self.tool = "brush"        # brush | eraser | sam | pan
        self.target = "censor"     # censor | protect
        self.zoom = 1.0
        self.offset = QPointF(0, 0)
        self.show_protect = True
        self.show_boxes = True
        self.current_item = -1
        self._engine = None
        self._panning = False
        self._zooming = False                # Ctrl+Space+드래그로 확대·축소 중
        self._zoom_anchor = QPointF()
        self._space = False
        self._last_pos = QPointF()
        self._cursor = QPointF(-1000, -1000)
        self._tablet_down = False
        self.pressure_fn = None              # Wintab 필압을 돌려주는 함수 (없으면 마우스로 취급)

    def _pen(self) -> tuple[float, bool, str]:
        """마우스 이벤트로 들어온 입력의 (필압, 지우개 쪽 여부, 입력 방식)."""
        if self.pressure_fn is not None:
            r = self.pressure_fn()
            if r is not None:
                return r[0], r[1], "Wintab"
        return 1.0, False, "마우스"

    @property
    def preset(self) -> BrushPreset:
        """지금 도구가 쓰는 브러시 (지우개 도구면 지우개 브러시)."""
        return self.eraser_preset if self.tool == "eraser" else self.brush_preset

    def set_space(self, down: bool) -> None:
        self._space = down
        self._update_cursor_shape()

    # ---- 문서 / 보기 ----------------------------------------------------
    def set_document(self, doc: Document | None) -> None:
        self.finish_stroke()
        self.doc = doc
        self.current_item = -1
        self._update_cursor_shape()
        self.fit()

    def fit(self) -> None:
        if not self.doc:
            self.update()
            return
        z = min(self.width() / self.doc.w, self.height() / self.doc.h) * 0.96
        self.zoom = max(z, 0.02)
        self.offset = QPointF((self.width() - self.doc.w * self.zoom) / 2,
                              (self.height() - self.doc.h * self.zoom) / 2)
        self.zoomChanged.emit(self.zoom)
        self.update()

    def focus_rect(self, rect) -> None:
        """화면 맞춤보다 아주 조금만 확대하고, 항목이 화면 가운데에 오게 한다."""
        if not self.doc:
            return
        x0, y0, x1, y1 = rect
        fit = min(self.width() / self.doc.w, self.height() / self.doc.h) * 0.96
        self.zoom = max(fit * 1.25, 0.02)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.offset = QPointF(self.width() / 2 - cx * self.zoom, self.height() / 2 - cy * self.zoom)
        self.zoomChanged.emit(self.zoom)
        self.update()

    def to_image(self, pos: QPointF) -> tuple[float, float]:
        return ((pos.x() - self.offset.x()) / self.zoom, (pos.y() - self.offset.y()) / self.zoom)

    def update_image_rect(self, rect) -> None:
        if rect is None:
            return
        x0, y0, x1, y1 = rect
        r = QRectF(x0 * self.zoom + self.offset.x(), y0 * self.zoom + self.offset.y(),
                   (x1 - x0) * self.zoom, (y1 - y0) * self.zoom)
        self.update(r.toAlignedRect().adjusted(-3, -3, 3, 3))

    def _zoom_at(self, pos: QPointF, factor: float) -> None:
        ix, iy = self.to_image(pos)
        self.zoom = min(max(self.zoom * factor, 0.02), 32.0)
        self.offset = QPointF(pos.x() - ix * self.zoom, pos.y() - iy * self.zoom)
        self.zoomChanged.emit(self.zoom)
        self.update()

    # ---- 그리기 ---------------------------------------------------------
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(ev.rect(), QColor(38, 38, 42))
        if not self.doc:
            p.setPen(QColor(150, 150, 155))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "이미지를 여기에 끌어다 놓으세요")
            return
        p.save()
        p.translate(self.offset)
        p.scale(self.zoom, self.zoom)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(0, 0, self.doc.qimage)
        if self.show_protect:
            p.drawImage(0, 0, self.doc.qoverlay)
        p.restore()

        if self.show_boxes:
            for i, it in enumerate(self.doc.items):
                x0, y0, x1, y1 = it.box
                r = QRectF(x0 * self.zoom + self.offset.x(), y0 * self.zoom + self.offset.y(),
                           (x1 - x0) * self.zoom, (y1 - y0) * self.zoom)
                col = QColor(60, 220, 140) if it.kind == "hand" else QColor(255, 90, 90)
                pen = QPen(col, 2 if i == self.current_item else 1)
                pen.setStyle(Qt.PenStyle.SolidLine if i == self.current_item else Qt.PenStyle.DashLine)
                p.setPen(pen)
                p.drawPolyline([r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft(), r.topLeft()])
                if i == self.current_item:
                    p.drawText(r.topLeft() + QPointF(2, -4), it.title)

        if self.tool in ("brush", "eraser") and not (self._space or self._panning or self._zooming) and self.underMouse():
            rad = self.preset.size * self.zoom / 2
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 160), 2.5))
            p.drawEllipse(self._cursor, rad, rad)
            p.setPen(QPen(QColor(255, 255, 255, 230), 1))
            p.drawEllipse(self._cursor, rad, rad)

    def _refresh_cursor(self, pos: QPointF) -> None:
        rad = self.preset.size * self.zoom / 2 + 6
        for c in (self._cursor, pos):
            self.update(QRectF(c.x() - rad, c.y() - rad, rad * 2, rad * 2).toAlignedRect())
        self._cursor = QPointF(pos)

    # ---- 획 -------------------------------------------------------------
    def _stroke_begin(self, pos: QPointF, pressure: float, erase: bool) -> None:
        if not self.doc:
            return
        preset = self.eraser_preset if erase else self.brush_preset
        self._engine = self.doc.begin_stroke(self.target, preset, erase)
        self._stroke_move(pos, pressure)

    def _stroke_move(self, pos: QPointF, pressure: float) -> None:
        if self._engine is None:
            return
        x, y = self.to_image(pos)
        rect = self._engine.move(x, y, max(pressure, 0.0))
        if rect:
            self.doc.compose(rect)
            self.update_image_rect(rect)

    def finish_stroke(self) -> None:
        if self._engine is not None:
            self.doc.commit_stroke(self._engine)
            self._engine = None
            self.edited.emit()

    def _press(self, pos: QPointF, pressure: float, button, mods, pen_eraser: bool = False) -> None:
        self._last_pos = QPointF(pos)
        if self._space and (mods & Qt.KeyboardModifier.ControlModifier):
            # Ctrl+Space+드래그: 누른 지점을 중심으로, 오른쪽으로 끌면 확대 · 왼쪽으로 끌면 축소
            self._zooming = True
            self._zoom_anchor = QPointF(pos)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            return
        if button == Qt.MouseButton.MiddleButton or self._space or self.tool == "pan":
            self._panning = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if not self.doc:
            return
        if self.tool == "sam":
            x, y = self.to_image(pos)
            if 0 <= x < self.doc.w and 0 <= y < self.doc.h:
                remove = button == Qt.MouseButton.RightButton or bool(mods & Qt.KeyboardModifier.AltModifier)
                self.samClicked.emit(x, y, not remove)
            return
        if self.tool == "brush" and button == Qt.MouseButton.RightButton:
            self.itemEraseClicked.emit(*self.to_image(pos))
            return
        if button == Qt.MouseButton.LeftButton:
            self._stroke_begin(pos, pressure, self.tool == "eraser" or pen_eraser)

    def _move(self, pos: QPointF, pressure: float) -> None:
        if self._zooming:
            dx = pos.x() - self._last_pos.x()
            self._last_pos = QPointF(pos)
            if dx:
                self._zoom_at(self._zoom_anchor, math.exp(dx * 0.008))
            return
        if self._panning:
            self.offset += pos - self._last_pos
            self._last_pos = QPointF(pos)
            self.update()
            return
        self._stroke_move(pos, pressure)
        self._refresh_cursor(pos)

    def _release(self) -> None:
        if self._zooming:
            self._zooming = False
            self._update_cursor_shape()
            return
        if self._panning:
            self._panning = False
            self._update_cursor_shape()
        self.finish_stroke()

    def _update_cursor_shape(self) -> None:
        if self._space or self.tool == "pan":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif self.tool == "sam":
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif self.doc is not None:
            self.setCursor(Qt.CursorShape.BlankCursor)
        else:
            self.unsetCursor()
        self.update()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._update_cursor_shape()
        self.update()

    # ---- 입력 이벤트 ----------------------------------------------------
    def tabletEvent(self, ev) -> None:
        t = ev.type()
        pos = ev.position()
        if t == QEvent.Type.TabletPress:
            self._tablet_down = True
            eraser = ev.pointerType() == QPointingDevice.PointerType.Eraser
            self._press(pos, ev.pressure(), ev.button(), ev.modifiers(), eraser)
            self.penInfo.emit(ev.pressure(), "Windows Ink")
        elif t == QEvent.Type.TabletMove:
            self._move(pos, ev.pressure())
            if self._engine is not None:
                self.penInfo.emit(ev.pressure(), "Windows Ink")
        elif t == QEvent.Type.TabletRelease:
            self._tablet_down = False
            self._release()
        ev.accept()

    def mousePressEvent(self, ev) -> None:
        if not self._tablet_down:
            pressure, eraser, src = self._pen()
            self._press(ev.position(), pressure, ev.button(), ev.modifiers(), eraser)
            self.penInfo.emit(pressure, src)

    def mouseMoveEvent(self, ev) -> None:
        if not self._tablet_down:
            if self._engine is not None:
                pressure, _, src = self._pen()
                self._move(ev.position(), pressure)
                self.penInfo.emit(pressure, src)
            else:
                self._move(ev.position(), 1.0)

    def mouseReleaseEvent(self, ev) -> None:
        if not self._tablet_down:
            self._release()

    def wheelEvent(self, ev) -> None:
        dy = ev.angleDelta().y()
        if dy == 0:
            return
        if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.sizeChanged.emit(self.preset.size * (1.1 if dy > 0 else 1 / 1.1))
        else:
            self._zoom_at(ev.position(), 1.2 if dy > 0 else 1 / 1.2)

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_BracketLeft:
            self.sizeChanged.emit(self.preset.size / 1.15)
        elif ev.key() == Qt.Key.Key_BracketRight:
            self.sizeChanged.emit(self.preset.size * 1.15)
        else:
            super().keyPressEvent(ev)

    def leaveEvent(self, ev) -> None:
        self.update()

    def enterEvent(self, ev) -> None:
        self._refresh_cursor(ev.position())

    def resizeEvent(self, ev) -> None:
        if self.doc and ev.oldSize().isEmpty():
            self.fit()
        elif self.doc:
            change = ev.size() - ev.oldSize()
            self.offset += QPointF(change.width() / 2, change.height() / 2)
        if self.underMouse():
            self._refresh_cursor(QPointF(self.mapFromGlobal(QCursor.pos())))
