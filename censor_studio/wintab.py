"""Wintab으로 펜 필압 읽기 (ctypes).

Qt 6은 기본적으로 Windows Ink로만 펜 입력을 받는다. 태블릿 드라이버에서 Windows Ink를 꺼 둔 경우
(그림 프로그램용으로 흔한 설정) 펜은 그냥 마우스로 보여서 필압이 오지 않는다. 그럴 때를 위해
Wintab 컨텍스트를 직접 열고, 마우스 이벤트가 올 때 가장 최근 패킷의 필압을 꺼내 쓴다.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

WTI_DEFSYSCTX = 4
WTI_DEVICES = 100
DVC_NAME = 1
DVC_NPRESSURE = 15
CXO_SYSTEM = 0x0001
CXO_MESSAGES = 0x0004
PK_CURSOR = 0x0020
PK_BUTTONS = 0x0040
PK_NORMAL_PRESSURE = 0x0400


class LOGCONTEXTW(ctypes.Structure):
    _fields_ = [
        ("lcName", wintypes.WCHAR * 40), ("lcOptions", wintypes.UINT), ("lcStatus", wintypes.UINT),
        ("lcLocks", wintypes.UINT), ("lcMsgBase", wintypes.UINT), ("lcDevice", wintypes.UINT),
        ("lcPktRate", wintypes.UINT), ("lcPktData", wintypes.DWORD), ("lcPktMode", wintypes.DWORD),
        ("lcMoveMask", wintypes.DWORD), ("lcBtnDnMask", wintypes.DWORD), ("lcBtnUpMask", wintypes.DWORD),
        ("lcInOrgX", wintypes.LONG), ("lcInOrgY", wintypes.LONG), ("lcInOrgZ", wintypes.LONG),
        ("lcInExtX", wintypes.LONG), ("lcInExtY", wintypes.LONG), ("lcInExtZ", wintypes.LONG),
        ("lcOutOrgX", wintypes.LONG), ("lcOutOrgY", wintypes.LONG), ("lcOutOrgZ", wintypes.LONG),
        ("lcOutExtX", wintypes.LONG), ("lcOutExtY", wintypes.LONG), ("lcOutExtZ", wintypes.LONG),
        ("lcSensX", wintypes.DWORD), ("lcSensY", wintypes.DWORD), ("lcSensZ", wintypes.DWORD),
        ("lcSysMode", wintypes.BOOL), ("lcSysOrgX", ctypes.c_int), ("lcSysOrgY", ctypes.c_int),
        ("lcSysExtX", ctypes.c_int), ("lcSysExtY", ctypes.c_int),
        ("lcSysSensX", wintypes.DWORD), ("lcSysSensY", wintypes.DWORD),
    ]


class AXIS(ctypes.Structure):
    _fields_ = [("axMin", wintypes.LONG), ("axMax", wintypes.LONG),
                ("axUnits", wintypes.UINT), ("axResolution", wintypes.DWORD)]


class PACKET(ctypes.Structure):
    # lcPktData에 켠 항목이 Wintab 표준 순서대로 들어온다: 커서 → 버튼 → 필압
    _fields_ = [("pkCursor", wintypes.UINT), ("pkButtons", wintypes.DWORD), ("pkNormalPressure", wintypes.UINT)]


class Wintab:
    """창 하나에 붙는 Wintab 컨텍스트. 드라이버가 없거나 실패하면 ok가 False다."""

    def __init__(self, hwnd: int):
        self.ok = False
        self.device = ""
        self.max_pressure = 1
        self._ctx = None
        self._last_time = 0.0
        self._last = (0.0, False)
        self._buf = (PACKET * 128)()
        try:
            dll = ctypes.WinDLL("wintab32.dll")
        except OSError:
            return
        self._dll = dll
        dll.WTInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
        dll.WTInfoW.restype = wintypes.UINT
        dll.WTOpenW.argtypes = [wintypes.HWND, ctypes.POINTER(LOGCONTEXTW), wintypes.BOOL]
        dll.WTOpenW.restype = ctypes.c_void_p
        dll.WTPacketsGet.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        dll.WTPacketsGet.restype = ctypes.c_int
        dll.WTClose.argtypes = [ctypes.c_void_p]
        dll.WTOverlap.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        dll.WTEnable.argtypes = [ctypes.c_void_p, wintypes.BOOL]
        dll.WTQueueSizeSet.argtypes = [ctypes.c_void_p, ctypes.c_int]
        try:
            if not dll.WTInfoW(0, 0, None):
                return   # 드라이버는 있는데 태블릿이 없다
            name = ctypes.create_unicode_buffer(256)
            dll.WTInfoW(WTI_DEVICES, DVC_NAME, name)
            self.device = name.value
            axis = AXIS()
            if dll.WTInfoW(WTI_DEVICES, DVC_NPRESSURE, ctypes.byref(axis)) and axis.axMax > 0:
                self.max_pressure = axis.axMax
            lc = LOGCONTEXTW()
            if not dll.WTInfoW(WTI_DEFSYSCTX, 0, ctypes.byref(lc)):
                return
            lc.lcName = "CensorStudio"
            lc.lcOptions = (lc.lcOptions | CXO_SYSTEM) & ~CXO_MESSAGES   # 커서는 드라이버가 움직이고, 패킷은 직접 꺼낸다
            lc.lcPktData = PK_CURSOR | PK_BUTTONS | PK_NORMAL_PRESSURE
            lc.lcPktMode = 0
            lc.lcMoveMask = lc.lcPktData
            ctx = dll.WTOpenW(hwnd, ctypes.byref(lc), True)
            if not ctx:
                return
            self._ctx = ctx
            dll.WTQueueSizeSet(ctx, 128)
            self.ok = True
        except Exception:
            self.ok = False

    def activate(self, active: bool = True) -> None:
        if self.ok:
            self._dll.WTEnable(self._ctx, bool(active))
            if active:
                self._dll.WTOverlap(self._ctx, True)

    def read(self) -> tuple[float, bool] | None:
        """(필압 0~1, 지우개 쪽 여부). 펜이 최근에 움직이지 않았으면 None — 마우스로 보면 된다."""
        if not self.ok:
            return None
        n = self._dll.WTPacketsGet(self._ctx, 128, self._buf)
        now = time.monotonic()
        if n > 0:
            pk = self._buf[n - 1]
            self._last = (min(pk.pkNormalPressure / self.max_pressure, 1.0), pk.pkCursor % 3 == 2)
            self._last_time = now
        if now - self._last_time > 0.25:
            return None
        return self._last

    def close(self) -> None:
        if self.ok:
            self._dll.WTClose(self._ctx)
            self.ok = False
