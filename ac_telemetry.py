# -*- coding: utf-8 -*-
# ac_telemetry.py
# Assetto Corsa telemetry viewer:
# - Main window: pygame graphs + info; "Advanced" button toggles a separate process with Tk GUI
# - Advanced window (separate process): scrollable cards + map with pan/zoom, side_l/side_r, trajectory overlay
# - Works even if AC not running (lazy-attach loop)
# - Steering angle taken from AC physics (p.steerAngle)

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import csv
import json
import math
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from datetime import datetime

# ----------------------------------------------------------------------
# Optional deps: pygame (main window)
# ----------------------------------------------------------------------
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
import pygame  # type: ignore

# ----------------------------------------------------------------------
# Shared Memory (Windows)
# ----------------------------------------------------------------------
FILE_MAP_READ = 0x0004
_k32 = ctypes.windll.kernel32

OpenFileMappingW = _k32.OpenFileMappingW
OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
OpenFileMappingW.restype = wt.HANDLE

MapViewOfFile = _k32.MapViewOfFile
MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
MapViewOfFile.restype = wt.LPVOID

UnmapViewOfFile = _k32.UnmapViewOfFile
UnmapViewOfFile.argtypes = [wt.LPCVOID]
UnmapViewOfFile.restype = wt.BOOL

CloseHandle = _k32.CloseHandle
CloseHandle.argtypes = [wt.HANDLE]
CloseHandle.restype = wt.BOOL


def wstr(warr) -> str:
    try:
        s = "".join(warr)
        i = s.find("\x00")
        return s if i < 0 else s[:i]
    except Exception:
        return ""


class SHMReader:
    def __init__(self, name: str):
        self.name = name
        self.hMap = None
        self.pView = None
        self._open()

    def _try_open(self, nm: str) -> bool:
        h = OpenFileMappingW(FILE_MAP_READ, False, nm)
        if h:
            v = MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
            if v:
                self.hMap, self.pView = h, v
                return True
            CloseHandle(h)
        return False

    def _open(self):
        for nm in (self.name, "Local\\" + self.name, "Global\\" + self.name):
            if self._try_open(nm):
                return
        raise RuntimeError(f"Не удалось открыть Shared Memory '{self.name}'.")

    def close(self):
        if self.pView:
            UnmapViewOfFile(self.pView)
            self.pView = None
        if self.hMap:
            CloseHandle(self.hMap)
            self.hMap = None

    def copy_into(self, ctype_struct):
        if not self.pView:
            raise RuntimeError("SHM not mapped")
        obj = ctype_struct()
        ctypes.memmove(ctypes.addressof(obj), self.pView, ctypes.sizeof(ctype_struct))
        return obj


# Structures (subset)
c_float = ctypes.c_float
c_int = ctypes.c_int


class SPageFilePhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int),
        ("gas", c_float),
        ("brake", c_float),
        ("fuel", c_float),
        ("gear", c_int),  # 0=R,1=N,2=1...
        ("rpms", c_int),
        ("steerAngle", c_float),  # degrees
        ("speedKmh", c_float),
        ("velocity", c_float * 3),  # world m/s
        ("accG", c_float * 3),

        ("wheelSlip", c_float * 4),
        ("wheelLoad", c_float * 4),            # N
        ("wheelsPressure", c_float * 4),       # psi
        ("wheelAngularSpeed", c_float * 4),    # rad/s
        ("tyreWear", c_float * 4),
        ("tyreDirtyLevel", c_float * 4),
        ("tyreCoreTemperature", c_float * 4),  # °C (core)
        ("camberRAD", c_float * 4),

        ("suspensionTravel", c_float * 4),     # m
        ("drs", c_float),
        ("tc", c_float),                       # 0..1
        ("heading", c_float),
        ("pitch", c_float),
        ("roll", c_float),
        ("cgHeight", c_float),                 # m
        ("carDamage", c_float * 5),
        ("numberOfTyresOut", c_int),
        ("pitLimiterOn", c_int),
        ("abs", c_float),                      # 0..1

        ("rideHeight", c_float * 2),           # m [front, rear]
        ("turboBoost", c_float),
        ("ballast", c_float),
        ("airDensity", c_float),               # kg/m^3
    ]


class SPageFileGraphics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", c_int),
        ("status", c_int),  # 0=OFF,1=REPLAY,2=LIVE,3=PAUSE
        ("session", c_int),
        ("currentTime", wt.WCHAR * 15),
        ("lastTime", wt.WCHAR * 15),
        ("bestTime", wt.WCHAR * 15),
        ("split", wt.WCHAR * 15),
        ("completedLaps", c_int),
        ("position", c_int),
        ("iCurrentTime", c_int),
        ("iLastTime", c_int),
        ("iBestTime", c_int),
        ("sessionTimeLeft", c_float),
        ("distanceTraveled", c_float),
        ("isInPit", c_int),
        ("currentSectorIndex", c_int),
        ("lastSectorTime", c_int),
        ("numberOfLaps", c_int),
        ("tyreCompound", wt.WCHAR * 33),
        ("replayTimeMultiplier", c_float),
        ("normalizedCarPosition", c_float),
        ("carCoordinates", c_float * 3),  # world X,Y,Z
        ("penaltyTime", c_float),
        ("flag", c_int),
        ("idealLineOn", c_int),
        ("isInPitLane", c_int),
        ("surfaceGrip", c_float),
    ]


class SPageFileStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", wt.WCHAR * 15),
        ("acVersion", wt.WCHAR * 15),
        ("numberOfSessions", c_int),
        ("numCars", c_int),
        ("carModel", wt.WCHAR * 33),
        ("track", wt.WCHAR * 33),
        ("playerName", wt.WCHAR * 33),
        ("playerSurname", wt.WCHAR * 33),
        ("playerNick", wt.WCHAR * 33),
        ("sectorCount", c_int),

        ("maxTorque", c_float),
        ("maxPower", c_float),
        ("maxRpm", c_int),
        ("maxFuel", c_float),
        ("suspensionMaxTravel", c_float * 4),
        ("tyreRadius", c_float * 4),
        ("maxTurboBoost", c_float),
        ("airTemp", c_float),
        ("roadTemp", c_float),
        ("penaltiesEnabled", wt.BOOL),

        ("aidFuelRate", c_float),
        ("aidTireRate", c_float),
        ("aidMechanicalDamage", c_float),
        ("aidAllowTyreBlankets", wt.BOOL),
        ("aidStability", c_float),
        ("aidAutoClutch", wt.BOOL),
        ("aidAutoBlip", wt.BOOL),

        ("trackConfig", wt.WCHAR * 33),
    ]


AC_STATUS = {0: "OFF", 1: "REPLAY", 2: "LIVE", 3: "PAUSE"}


def psi_to_bar(psi: float) -> float:
    return psi * 0.0689475729


def gear_text_offset(raw: int) -> str:
    if raw <= 0:
        return "R"
    if raw == 1:
        return "N"
    return str(raw - 1)


# ----------------------------------------------------------------------
# Plot helpers (pygame)
# ----------------------------------------------------------------------
class Series:
    def __init__(self, name: str, color: Tuple[int, int, int], y_min=None, y_max=None, autoscale=True):
        self.name = name
        self.color = color
        self.y_min = y_min
        self.y_max = y_max
        self.autoscale = autoscale
        self.buf: deque[float] = deque()


class Plot:
    def __init__(self, title: str, capacity=600, bg=(18, 18, 18), grid=(40, 40, 40)):
        self.title = title
        self.capacity = capacity
        self.bg = bg
        self.grid = grid
        self.series: List[Series] = []

    def set_title(self, title): self.title = title
    def add_series(self, s: Series): self.series.append(s)

    def push(self, idx: int, value: float):
        s = self.series[idx]
        s.buf.append(float(value))
        while len(s.buf) > self.capacity:
            s.buf.popleft()

    def _calc_scale(self) -> Tuple[float, float]:
        vmins: List[float] = []
        vmaxs: List[float] = []
        for s in self.series:
            if not s.buf:
                continue
            if s.autoscale:
                vmins.append(min(s.buf))
                vmaxs.append(max(s.buf))
            else:
                vmins.append(s.y_min if s.y_min is not None else min(s.buf))
                vmaxs.append(s.y_max if s.y_max is not None else max(s.buf))
        if not vmins:
            return 0.0, 1.0
        y_min, y_max = min(vmins), max(vmaxs)
        if y_min == y_max:
            y_min -= 1.0
            y_max += 1.0
        return y_min, y_max

    def draw(self, surf, rect, font, title_color=(200, 200, 200), axis_color=(70, 70, 70)):
        pygame.draw.rect(surf, self.bg, rect, 0)
        for i in range(1, 4):
            y = rect.top + int(rect.height * i / 4)
            pygame.draw.line(surf, self.grid, (rect.left, y), (rect.right, y), 1)
        title_surf = font.render(self.title, True, title_color)
        surf.blit(title_surf, (rect.left + 8, rect.top + 6))
        pygame.draw.rect(surf, axis_color, rect, 1)
        y_min, y_max = self._calc_scale()
        y_rng = (y_max - y_min) or 1.0
        pad_top = 22
        w = rect.width
        h = rect.height - pad_top
        ox = rect.left
        oy = rect.top + pad_top
        for s in self.series:
            if len(s.buf) < 2: continue
            pts = []
            for i, val in enumerate(list(s.buf)[-w:]):
                x = ox + i
                y_norm = (val - y_min) / y_rng
                y = oy + (h - 1) - int(y_norm * (h - 1))
                pts.append((x, y))
            if len(pts) >= 2:
                pygame.draw.aalines(surf, s.color, False, pts)
        mini = f"{y_min:.2f}"; maxi = f"{y_max:.2f}"
        surf.blit(font.render(maxi, True, (150, 150, 150)), (rect.right - 60, rect.top + 4))
        surf.blit(font.render(mini, True, (150, 150, 150)), (rect.right - 60, rect.bottom - 20))


def ellipsize(text: str, font: pygame.font.Font, max_width: int) -> str:
    if font.size(text)[0] <= max_width:
        return text
    ell = "…"
    w_ell = font.size(ell)[0]
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if font.size(text[:mid])[0] + w_ell <= max_width:
            lo = mid + 1
        else:
            hi = mid
    return text[:max(0, lo - 1)] + ell


def wrap_text(text: str, font: pygame.font.Font, max_width: int) -> List[str]:
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        if font.size(test)[0] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w if font.size(w)[0] <= max_width else ellipsize(w, font, max_width)
    if cur:
        lines.append(cur)
    return lines


def set_topmost_for_pygame_window(is_topmost=True):
    try:
        hwnd = pygame.display.get_wm_info().get("window")
        if not hwnd:
            return
        HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
        SWP_NOMOVE, SWP_NOSIZE, SWP_SHOWWINDOW = 0x2, 0x1, 0x0040
        ctypes.windll.user32.SetWindowPos(
            int(hwnd),
            HWND_TOPMOST if is_topmost else HWND_NOTOPMOST,
            0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        )
    except Exception:
        pass


# ----------------------------------------------------------------------
# Track assets (map.ini / map.png / side_l.csv / side_r.csv)
# ----------------------------------------------------------------------
def _int(s: str, default: Union[int, float]) -> int:
    try:
        return int(s)
    except Exception:
        try:
            return int(round(float(s)))
        except Exception:
            try:
                return int(round(float(default)))
            except Exception:
                return 0


def _float(s: str, default: float) -> float:
    try:
        return float(s)
    except Exception:
        return float(default)


def load_map_ini(path: Path) -> Dict[str, float]:
    defaults = dict(WIDTH=1024, HEIGHT=1024, SCALE_FACTOR=1.0, X_OFFSET=512.0, Z_OFFSET=512.0)
    vals = defaults.copy()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                ln = line.strip()
                if not ln or ln.startswith(";") or "=" not in ln:
                    continue
                k, v = [x.strip() for x in ln.split("=", 1)]
                U = k.upper()
                if U == "WIDTH":
                    vals["WIDTH"] = _int(v, vals["WIDTH"])
                elif U == "HEIGHT":
                    vals["HEIGHT"] = _int(v, vals["HEIGHT"])
                elif U == "SCALE_FACTOR":
                    vals["SCALE_FACTOR"] = _float(v, vals["SCALE_FACTOR"])
                elif U in ("X_OFFSET", "XOFF", "X"):
                    vals["X_OFFSET"] = _float(v, vals["X_OFFSET"])
                elif U in ("Z_OFFSET", "Y_OFFSET", "Z", "Y"):
                    vals["Z_OFFSET"] = _float(v, vals["Z_OFFSET"])
    except Exception:
        pass
    return dict(w=float(vals["WIDTH"]), h=float(vals["HEIGHT"]), sx=float(vals["SCALE_FACTOR"]),
                ox=float(vals["X_OFFSET"]), oz=float(vals["Z_OFFSET"]), invert_y=False)


def guess_ac_roots(cli_root: Optional[str]) -> List[Path]:
    roots: List[Path] = []
    if cli_root:
        roots.append(Path(cli_root))
    env = os.environ.get("AC_ROOT")
    if env:
        roots.append(Path(env))
    roots += [
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\assettocorsa"),
        Path(r"C:\Program Files\Steam\steamapps\common\assettocorsa"),
    ]
    out: List[Path] = []
    seen = set()
    for p in roots:
        try:
            rp = p.resolve()
            if rp in seen: continue
            if rp.exists():
                out.append(rp)
                seen.add(rp)
        except Exception:
            pass
    return out


class TrackAssets:
    def __init__(self):
        self.base: Optional[Path] = None
        self.map_png: Optional[Path] = None
        self.outline_png: Optional[Path] = None
        self.map_ini: Optional[Path] = None
        self.side_l: Optional[Path] = None
        self.side_r: Optional[Path] = None
        self.transform: Optional[Dict[str, float]] = None  # sx, ox, oz, w, h, invert_y


def find_track_assets(track: Optional[str], track_cfg: Optional[str], ac_roots: List[Path]) -> Optional[TrackAssets]:
    if not track:
        return None

    def scan_base(base: Path) -> Optional[TrackAssets]:
        if not base.exists():
            return None
        a = TrackAssets()
        a.base = base
        cand_inis = [base / "data" / "map.ini"] + list(base.glob("*/data/map.ini")) + list(base.glob("layout_*/*/data/map.ini"))
        for c in cand_inis:
            if c.exists():
                a.map_ini = c
                a.transform = load_map_ini(c)
                break
        cand_maps: List[Path] = []
        if track_cfg:
            for lk in [track_cfg, f"layout_{track_cfg}"]:
                cand_maps += list((base / lk).glob("map.png"))
                cand_maps += list((base / lk / "ui").glob("map.png"))
                cand_maps += list((base / "ui" / lk).glob("map.png"))
                cand_maps += list((base / "ui" / lk).glob("outline.png"))
        cand_maps += [base / "map.png", base / "ui" / "map.png", base / "ui" / "outline.png"]
        cand_maps += list(base.glob("*/map.png")) + list(base.glob("*/ui/outline.png"))
        for c in cand_maps:
            if c.exists():
                if c.name.lower() == "map.png": a.map_png = c; break
                if a.outline_png is None and c.name.lower() == "outline.png": a.outline_png = c
        if a.map_ini:
            dat = a.map_ini.parent
            L = dat / "side_l.csv"
            R = dat / "side_r.csv"
            a.side_l = L if L.exists() else None
            a.side_r = R if R.exists() else None
        return a

    for root in ac_roots:
        base = root / "content" / "tracks" / track
        if base.exists():
            res = scan_base(base)
            if res and (res.map_png or res.outline_png or res.map_ini):
                return res

    for root in ac_roots:
        troot = root / "content" / "tracks"
        if not troot.exists(): continue
        for p in troot.glob(track + "*"):
            if p.is_dir():
                res = scan_base(p)
                if res and (res.map_png or res.outline_png or res.map_ini):
                    return res
    return None


def read_side_csv_points(path: Path) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    try:
        with open(path, "r", newline="", encoding="utf-8", errors="ignore") as f:
            rd = csv.reader(f)
            for row in rd:
                floats: List[float] = []
                for cell in row:
                    try:
                        floats.append(float(cell))
                    except Exception:
                        pass
                if len(floats) >= 2:
                    x, z = floats[0], floats[1]
                    pts.append((x, z))
    except Exception:
        pass
    return pts


# ----------------------------------------------------------------------
# Advanced window process (Tk in its own process)
# ----------------------------------------------------------------------
def advanced_process_main(state_queue: "queue.Queue", ac_roots_list: List[str], manual_map_str: Optional[str], poll_ms: int):
    # Local imports here to keep Tk only in this process
    try:
        import tkinter as tk  # type: ignore
        from tkinter import ttk  # type: ignore
        TK_OK = True
    except Exception:
        TK_OK = False
        return

    # Optional Pillow for high-quality resize
    try:
        from PIL import Image, ImageTk  # type: ignore
        PIL_OK = True
    except Exception:
        Image = None  # type: ignore
        ImageTk = None  # type: ignore
        PIL_OK = False

    ac_roots = [Path(p) for p in ac_roots_list]
    manual_map = Path(manual_map_str).resolve() if manual_map_str else None

    class MapPanel:
        def __init__(self, canvas: "tk.Canvas"):
            self.cv = canvas
            self.map_img_tk = None
            self.map_img_pil = None
            self._view_img = None
            self.map_w = 1.0
            self.map_h = 1.0
            self.view_scale = 1.0
            self.view_dx = 0.0
            self.view_dy = 0.0
            self.pan_start = None
            self.sideL_img: List[Tuple[float, float]] = []
            self.sideR_img: List[Tuple[float, float]] = []
            self.trail_img: List[Tuple[float, float]] = []
            self.track_assets: Optional[TrackAssets] = None
            self.transform: Optional[Dict[str, float]] = None
            self.last_track = None
            self.last_cfg = None
            self.base_w = 1.0
            self.base_h = 1.0
            self.scale_factor = 1.0
            self.offset_x = 0.0
            self.offset_z = 0.0
            self.invert_y = False
            self.center_on_car = False
            self.center_offset: Tuple[float, float] = (0.5, 0.5)
            self.last_car_pos: Optional[Tuple[float, float]] = None
            self.min_scale = 0.05
            self.max_scale = 12.0

            self.cv.bind("<Configure>", lambda e: self.fit_to_view(force=True))
            self.cv.bind("<ButtonPress-1>", self._start_pan)
            self.cv.bind("<B1-Motion>", self._do_pan)
            self.cv.bind("<ButtonRelease-1>", self._stop_pan)
            self.cv.bind("<ButtonPress-2>", self._start_pan)
            self.cv.bind("<B2-Motion>", self._do_pan)
            self.cv.bind("<ButtonRelease-2>", self._stop_pan)
            self.cv.bind("<MouseWheel>", self._zoom)
            self.cv.bind("<Button-4>", self._zoom)
            self.cv.bind("<Button-5>", self._zoom)

        def _start_pan(self, e):
            if self.center_on_car:
                return 'break'
            self.pan_start = (e.x, e.y)
            return 'break'

        def _do_pan(self, e):
            if self.center_on_car or not self.pan_start:
                return 'break'
            sx, sy = self.pan_start
            self.view_dx += (e.x - sx)
            self.view_dy += (e.y - sy)
            self.pan_start = (e.x, e.y)
            self.redraw()
            return 'break'

        def _stop_pan(self, _e):
            self.pan_start = None
            return 'break'

        def _zoom(self, e):
            delta = getattr(e, 'delta', 0)
            if not delta and hasattr(e, 'num') and e.num in (4, 5):
                delta = 120 if e.num == 4 else -120
            if not delta:
                return 'break'
            factor = 1.1 if delta > 0 else 0.9
            new_scale = max(self.min_scale, min(self.max_scale, self.view_scale * factor))
            if self.center_on_car and self.last_car_pos:
                self.view_scale = new_scale
                self.recenter_on_car()
                self.redraw()
                return 'break'
            mx, my = e.x, e.y
            ix = (mx - self.view_dx) / (self.view_scale or 1.0)
            iy = (my - self.view_dy) / (self.view_scale or 1.0)
            self.view_scale = new_scale
            self.view_dx = mx - ix * self.view_scale
            self.view_dy = my - iy * self.view_scale
            self.redraw()
            return 'break'

        def _update_transform_cache(self):
            if not self.transform:
                self.scale_factor = 1.0
                self.offset_x = 0.0
                self.offset_z = 0.0
                self.invert_y = False
                self.base_w = float(self.map_w or 1.0)
                self.base_h = float(self.map_h or 1.0)
                return
            try:
                self.scale_factor = float(self.transform.get("sx", 1.0) or 1.0)
            except Exception:
                self.scale_factor = 1.0
            if not math.isfinite(self.scale_factor) or abs(self.scale_factor) < 1e-6:
                self.scale_factor = 1.0
            try:
                self.offset_x = float(self.transform.get("ox", 0.0) or 0.0)
            except Exception:
                self.offset_x = 0.0
            try:
                self.offset_z = float(self.transform.get("oz", 0.0) or 0.0)
            except Exception:
                self.offset_z = 0.0
            inv_raw = self.transform.get("invert_y")
            if isinstance(inv_raw, str):
                inv_val = inv_raw.strip().lower()
                self.invert_y = inv_val in {"1", "true", "yes", "on", "y"}
            elif inv_raw is None:
                self.invert_y = False
            else:
                self.invert_y = bool(inv_raw)
            try:
                self.base_w = float(self.transform.get("w", self.map_w) or self.map_w or 1.0)
            except Exception:
                self.base_w = float(self.map_w or 1.0)
            try:
                self.base_h = float(self.transform.get("h", self.map_h) or self.map_h or 1.0)
            except Exception:
                self.base_h = float(self.map_h or 1.0)

        def fit_to_view(self, force=False):
            cw = self.cv.winfo_width() or 0
            ch = self.cv.winfo_height() or 0
            if cw <= 2 or ch <= 2:
                return
            s = min(cw / max(1.0, self.map_w), ch / max(1.0, self.map_h))
            s = max(self.min_scale, min(self.max_scale, s))
            self.view_scale = s
            if self.center_on_car and self.last_car_pos:
                self.recenter_on_car()
                if force:
                    self.redraw()
            else:
                self.view_dx = (cw - self.map_w * s) / 2
                self.view_dy = (ch - self.map_h * s) / 2
                if force:
                    self.redraw()

        def world_to_img(self, x: float, z: float) -> Tuple[float, float]:
            factor = self.scale_factor or 1.0
            px = (x + self.offset_x) / factor
            py = (z + self.offset_z) / factor
            if self.invert_y:
                base_h = self.base_h or self.map_h or 1.0
                py = base_h - py
            return px, py

        def set_center_mode(self, enabled: bool):
            self.center_on_car = bool(enabled)
            if self.center_on_car:
                self.pan_start = None
                self.recenter_on_car()

        def set_center_offset(self, x: float, y: float):
            self.center_offset = (float(x), float(y))
            if self.center_on_car:
                self.recenter_on_car()

        def set_car_position(self, pos: Optional[Tuple[float, float]]):
            self.last_car_pos = pos
            if self.center_on_car:
                self.recenter_on_car()

        def reset_view(self):
            self.fit_to_view(force=True)

        def recenter_on_car(self):
            if not self.center_on_car or not self.last_car_pos:
                return
            px, py = self.world_to_img(self.last_car_pos[0], self.last_car_pos[1])
            cw = self.cv.winfo_width() or self.cv.winfo_reqwidth() or 0
            ch = self.cv.winfo_height() or self.cv.winfo_reqheight() or 0
            if cw <= 2 or ch <= 2:
                return
            self.view_dx = cw * self.center_offset[0] - px * self.view_scale
            self.view_dy = ch * self.center_offset[1] - py * self.view_scale

        def load_assets_if_needed(self, track_name: Optional[str], track_cfg: Optional[str]) -> Optional[str]:
            changed = (track_name != self.last_track) or (track_cfg != self.last_cfg)
            if not changed:
                return None
            self.last_track, self.last_cfg = track_name, track_cfg

            self.map_img_tk = None
            self.map_img_pil = None
            self._view_img = None
            self.map_w = self.map_h = 1.0
            self.sideL_img = []
            self.sideR_img = []
            self.trail_img = []
            self.last_car_pos = None

            if manual_map and manual_map.exists():
                self.track_assets = TrackAssets()
                self.track_assets.base = manual_map.parent
                self.track_assets.map_png = manual_map
                self.track_assets.transform = dict(w=1024.0, h=1024.0, sx=1.0, ox=512.0, oz=512.0, invert_y=True)
            else:
                self.track_assets = find_track_assets(track_name, track_cfg, ac_roots)
            self.transform = (self.track_assets.transform if self.track_assets and self.track_assets.transform else None)
            self._update_transform_cache()

            status = "����: с������⭠"
            img_path = None
            if self.track_assets:
                img_path = self.track_assets.map_png or self.track_assets.outline_png

            if img_path and img_path.exists():
                try:
                    if PIL_OK and Image is not None and ImageTk is not None:
                        self.map_img_pil = Image.open(img_path)
                        self.map_w, self.map_h = [float(v) for v in self.map_img_pil.size]
                        self.map_img_tk = ImageTk.PhotoImage(self.map_img_pil)
                    else:
                        self.map_img_tk = tk.PhotoImage(file=str(img_path))
                        self.map_w = float(self.map_img_tk.width())
                        self.map_h = float(self.map_img_tk.height())
                    status = f"����: {self.track_assets.base.name if self.track_assets and self.track_assets.base else '?'}"
                except Exception:
                    status = "����: с訡�� с���㧪�"
            else:
                if self.track_assets:
                    status = "����: с� с������ (���� сࠥ����)"
                else:
                    status = "����: с������⭠"

            self._update_transform_cache()

            if self.track_assets and self.track_assets.side_l and self.track_assets.side_r:
                Lw = read_side_csv_points(self.track_assets.side_l)
                Rw = read_side_csv_points(self.track_assets.side_r)
                self.sideL_img = [self.world_to_img(x, z) for (x, z) in Lw]
                self.sideR_img = [self.world_to_img(x, z) for (x, z) in Rw]

            self.fit_to_view(force=True)
            return status

        def redraw(self):
            self.cv.delete("all")
            if self.map_img_tk:
                if PIL_OK and self.map_img_pil is not None and Image is not None and ImageTk is not None:
                    iw = int(max(1, min(8192, self.map_w * self.view_scale)))
                    ih = int(max(1, min(8192, self.map_h * self.view_scale)))
                    img = self.map_img_pil.resize((iw, ih), Image.BILINEAR)
                    self._view_img = ImageTk.PhotoImage(img)
                    self.cv.create_image(self.view_dx, self.view_dy, anchor="nw", image=self._view_img)
                else:
                    self.cv.create_image(self.view_dx, self.view_dy, anchor="nw", image=self.map_img_tk)
            else:
                w = self.cv.winfo_width() or 0
                h = self.cv.winfo_height() or 0
                self.cv.create_rectangle(10, 10, max(11, w - 10), max(11, h - 10), outline="#333")

            def draw_poly(pts, color="#ffcc00", width=2):
                if not pts:
                    return
                coords: List[float] = []
                s = self.view_scale
                dx = self.view_dx
                dy = self.view_dy
                for x, y in pts:
                    coords.extend([dx + x * s, dy + y * s])
                if len(coords) >= 4:
                    self.cv.create_line(*coords, fill=color, width=width, capstyle="round", joinstyle="round")

            if self.sideL_img:
                draw_poly(self.sideL_img, "#ffcc00", 2)
            if self.sideR_img:
                draw_poly(self.sideR_img, "#ffcc00", 2)
            if self.trail_img:
                draw_poly(self.trail_img, "#00e5ff", 2)
                x, y = self.trail_img[-1]
                cx = self.view_dx + x * self.view_scale
                cy = self.view_dy + y * self.view_scale
                self.cv.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="#fff", fill="#ff0", width=2)
                self.cv.create_text(cx + 10, cy - 10, text="YOU", fill="#fff", anchor="w")
    class Snapshot:
        __slots__ = ("ts", "data")

        def __init__(self, ts: float, data: Dict[str, Any]):
            self.ts = ts
            self.data = data


    class GraphCanvas:
        def __init__(self, parent: Any, title: str, series: List[Dict[str, Any]], height: int = 160):
            self.title = title
            self.series = series
            self.canvas = tk.Canvas(parent, height=height, bg="#111115", highlightthickness=0)
            self.canvas.pack(fill="x", pady=(4, 4))
            self.canvas.bind("<Configure>", lambda _e: self._render())
            self._data: List[Snapshot] = []
            self._highlight_ts: Optional[float] = None

        def update(self, snapshots: List[Snapshot], highlight_ts: Optional[float] = None):
            self._data = list(snapshots)
            self._highlight_ts = highlight_ts
            self._render()

        def _render(self):
            canvas = self.canvas
            canvas.delete("all")
            width = max(60, int(canvas.winfo_width() or canvas.winfo_reqwidth() or 320))
            height = max(60, int(canvas.winfo_height() or canvas.winfo_reqheight() or 160))
            pad_left, pad_right = 48, 16
            pad_top, pad_bottom = 30, 30
            x0 = pad_left
            y0 = pad_top
            x1 = width - pad_right
            y1 = height - pad_bottom
            if x1 <= x0:
                x1 = x0 + 10
            if y1 <= y0:
                y1 = y0 + 10
            canvas.create_rectangle(x0, y0, x1, y1, outline="#2b2b2b", width=1)
            canvas.create_text(x0, 12, text=self.title, anchor="w", fill="#f0f0f0", font=("Segoe UI", 10, "bold"))
            for i in range(1, 4):
                gy = y0 + (y1 - y0) * i / 4
                canvas.create_line(x0, gy, x1, gy, fill="#1d1d1d")
            data = self._data
            if len(data) < 2:
                canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="No data", fill="#666666", font=("Segoe UI", 9))
                return
            times = [snap.ts for snap in data]
            t0 = times[0]
            t1 = times[-1]
            if t1 - t0 < 1e-6:
                t1 = t0 + 1.0
            series_points: List[Tuple[str, str, List[Tuple[float, float]]]] = []
            y_values: List[float] = []
            for series in self.series:
                color = series["color"]
                name = series.get("name", "")
                extractor = series["extract"]
                pts: List[Tuple[float, float]] = []
                for snap in data:
                    val = extractor(snap.data)
                    if val is None:
                        continue
                    try:
                        val_f = float(val)
                    except Exception:
                        continue
                    pts.append((snap.ts, val_f))
                    y_values.append(val_f)
                series_points.append((color, name, pts))
            if not y_values:
                canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text="No values", fill="#666666", font=("Segoe UI", 9))
                return
            y_min = min(y_values)
            y_max = max(y_values)
            if math.isclose(y_min, y_max, rel_tol=1e-9):
                delta = abs(y_min) * 0.1 or 1.0
                y_min -= delta
                y_max += delta
            scale_x = (x1 - x0) / (t1 - t0)
            scale_y = (y1 - y0) / (y_max - y_min)
            legend_x = x0
            legend_y = y0 - 14
            for color, name, pts in series_points:
                if not pts:
                    continue
                canvas.create_rectangle(legend_x, legend_y, legend_x + 10, legend_y + 10, outline=color, fill=color)
                canvas.create_text(legend_x + 14, legend_y + 5, text=name, anchor="w", fill="#d8d8d8", font=("Segoe UI", 8))
                legend_x += max(60, len(name) * 8)
            fmt = "{:.2f}" if abs(y_max - y_min) < 100 else "{:.0f}"
            canvas.create_text(x1 + 6, y0, text=fmt.format(y_max), anchor="nw", fill="#b0b0b0", font=("Segoe UI", 8))
            canvas.create_text(x1 + 6, y1, text=fmt.format(y_min), anchor="sw", fill="#b0b0b0", font=("Segoe UI", 8))
            canvas.create_text(x0, y1 + 12, text="0 с", anchor="nw", fill="#7a7a7a", font=("Segoe UI", 8))
            canvas.create_text(x1, y1 + 12, text=f"{(t1 - t0):.1f} с", anchor="ne", fill="#7a7a7a", font=("Segoe UI", 8))
            for color, _name, pts in series_points:
                if not pts:
                    continue
                coords: List[float] = []
                for ts, val in pts:
                    x = x0 + (ts - t0) * scale_x
                    y = y1 - (val - y_min) * scale_y
                    coords.extend([x, y])
                if len(coords) >= 4:
                    canvas.create_line(*coords, fill=color, width=2, smooth=True)
                elif len(coords) == 2:
                    x, y = coords
                    canvas.create_oval(x - 2, y - 2, x + 2, y + 2, outline=color, fill=color)
            highlight_ts = self._highlight_ts
            if highlight_ts is not None:
                for color, _name, pts in reversed(series_points):
                    if not pts:
                        continue
                    target = None
                    for ts, val in reversed(pts):
                        target = (ts, val)
                        if ts <= highlight_ts:
                            break
                    if target is None:
                        continue
                    ts, val = target
                    x = x0 + (ts - t0) * scale_x
                    y = y1 - (val - y_min) * scale_y
                    canvas.create_oval(x - 4, y - 4, x + 4, y + 4, outline=color, width=2)
                    break



class GraphManager:
    def __init__(self, parent: Any):
        self.parent = parent
        self.graphs: List[GraphCanvas] = []
        self.window_seconds: float = 10.0

    def set_window(self, seconds: float) -> None:
        try:
            self.window_seconds = max(0.1, float(seconds))
        except Exception:
            self.window_seconds = 10.0

    def _trim_snapshots(self, snapshots: List[Snapshot]) -> List[Snapshot]:
        if not snapshots:
            return []
        window = self.window_seconds
        if window <= 0:
            return list(snapshots)
        t_end = snapshots[-1].ts
        t_start = t_end - window
        trimmed = [snap for snap in snapshots if snap.ts >= t_start]
        if trimmed:
            return trimmed
        return [snapshots[-1]]

    def build(self, configs: List[Dict[str, Any]]):
        self.graphs.clear()
        for cfg in configs:
            graph = GraphCanvas(self.parent, cfg["title"], cfg["series"], cfg.get("height", 160))
            self.graphs.append(graph)

    def update(self, snapshots: List[Snapshot], highlight_ts: Optional[float]):
        trimmed = self._trim_snapshots(snapshots)
        for graph in self.graphs:
            graph.update(trimmed, highlight_ts=highlight_ts)


@dataclass
class RecordingRun:
    label: str
    car: str
    track: str
    track_cfg: str
    created_at: float
    snapshots: List[Snapshot] = field(default_factory=list)


class AdvancedStateController:
    def __init__(
        self,
        root: Any,
        mpanel: MapPanel,
        lbl_title: Any,
        lbl_map_status: Any,
        set_label: Any,
        graph_manager: GraphManager,
        view_btn: Any,
        record_btn: Any,
        playback_btn: Any,
        play_btn: Any,
        prev_btn: Any,
        next_btn: Any,
        slider: Any,
        time_label: Any,
        cards_holder: Any,
        graphs_holder: Any,
        scroll_callback: Any,
        records_var: Any,
        records_cb: Any,
        window_var: Any,
        window_cb: Any,
        window_choices: List[int],
    ):
        self.root = root
        self.mpanel = mpanel
        self.lbl_title = lbl_title
        self.lbl_map_status = lbl_map_status
        self.set_label = set_label
        self.graph_manager = graph_manager
        self.view_btn = view_btn
        self.record_btn = record_btn
        self.playback_btn = playback_btn
        self.play_btn = play_btn
        self.prev_btn = prev_btn
        self.next_btn = next_btn
        self.slider = slider
        self.time_label = time_label
        self.cards_holder = cards_holder
        self.graphs_holder = graphs_holder
        self.scroll_callback = scroll_callback
        self.records_var = records_var
        self.records_cb = records_cb
        self.window_var = window_var
        self.window_cb = window_cb
        self.window_choices = sorted({int(v) for v in window_choices if v > 0}) or [10]
        self.window_labels = [f"{sec} с" for sec in self.window_choices]
        self.window_map = {label: float(sec) for label, sec in zip(self.window_labels, self.window_choices)}
        self.graph_manager.set_window(self.window_choices[0])
        self.window_cb.configure(state="readonly", values=self.window_labels)
        default_label = next((label for label in self.window_labels if "10" in label), self.window_labels[0])
        self.window_var.set(default_label)
        try:
            self.window_cb.current(self.window_labels.index(default_label))
        except ValueError:
            pass
        self.window_cb.bind('<<ComboboxSelected>>', self._handle_window_change)

        self.history: deque[Snapshot] = deque()
        self.max_history_seconds = max(self.window_choices)
        self.recordings: List[RecordingRun] = []
        self.active_recording: Optional[RecordingRun] = None
        self.playback_run: Optional[RecordingRun] = None
        self.recording = False
        self.view_mode = "cards"
        self.play_mode = "live"
        self.play_running = False
        self.play_job: Optional[str] = None
        self.slider_adjust = False
        self.play_index = 0
        self.latest_snapshot: Optional[Snapshot] = None

        self.records_cb.configure(state="disabled")
        self.records_cb.bind('<<ComboboxSelected>>', self._handle_recording_select)

        self.view_btn.configure(command=self.toggle_view)
        self.record_btn.configure(command=self.toggle_record)
        self.playback_btn.configure(command=self.toggle_play_mode)
        self.play_btn.configure(command=self.toggle_play)
        self.prev_btn.configure(command=lambda: self.step_playback(-1))
        self.next_btn.configure(command=lambda: self.step_playback(1))
        self.slider.configure(command=self.on_slider_move)

        self._show_cards()
        self._set_play_controls_state(False)
        self.playback_btn.state(["disabled"])
        self._handle_window_change()
        self.time_label.configure(text="LIVE")

    def shutdown(self):
        self.stop_play_loop()

    def toggle_view(self):
        if self.view_mode == "cards":
            self._show_graphs()
        else:
            self._show_cards()

    def _show_cards(self):
        self.graphs_holder.pack_forget()
        self.cards_holder.pack(fill="x", expand=True)
        self.view_mode = "cards"
        self.view_btn.configure(text="Показать графики")
        self._fire_scroll_update()

    def _show_graphs(self):
        self.cards_holder.pack_forget()
        self.graphs_holder.pack(fill="x", expand=True)
        self.view_mode = "graphs"
        self.view_btn.configure(text="Показать значения")
        self._refresh_graphs()
        self._fire_scroll_update()

    def _fire_scroll_update(self):
        try:
            self.scroll_callback()
        except Exception:
            pass

    def toggle_record(self):
        if self.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        base = self.latest_snapshot or (self.history[-1] if self.history else None)
        car = base.data.get("carModel", "-") if base else "-"
        track = base.data.get("track", "-") if base else "-"
        cfg = base.data.get("trackConfig", "") if base else ""
        created = time.time()
        label = self._format_recording_label(created, car, track, cfg, 0.0)
        self.active_recording = RecordingRun(label=label, car=car, track=track, track_cfg=cfg, created_at=created)
        if base:
            self.active_recording.snapshots.append(Snapshot(base.ts, deepcopy(base.data)))
        self.recording = True
        self.play_mode = "live"
        self.stop_play_loop()
        self.record_btn.configure(text="Стоп запись")
        self._set_play_controls_state(False)
        self.playback_btn.configure(text="Режим воспроизведения")
        self.playback_btn.state(["disabled"])

    def stop_recording(self):
        run = self.active_recording
        self.recording = False
        self.active_recording = None
        self.record_btn.configure(text="Начать запись")
        if run and run.snapshots:
            duration = max(0.0, run.snapshots[-1].ts - run.snapshots[0].ts)
            run.label = self._format_recording_label(run.created_at, run.car, run.track, run.track_cfg, duration)
            self.recordings.append(run)
            self._refresh_recordings_combo(select_index=len(self.recordings) - 1)
        self._update_playback_button_state()

    def toggle_play_mode(self):
        if self.play_mode == "live":
            self.set_play_mode("playback")
        else:
            self.set_play_mode("live")

    def set_play_mode(self, mode: str):
        if mode == self.play_mode:
            return
        if mode == "playback":
            if not self.recordings:
                return
            if not self.playback_run:
                self._refresh_recordings_combo(select_index=len(self.recordings) - 1)
            if not self.playback_run or not self.playback_run.snapshots:
                return
            self.play_mode = "playback"
            self.stop_play_loop()
            self._set_play_controls_state(True)
            self.playback_btn.configure(text="К живым данным")
            self._update_slider_range()
            self.slider_adjust = True
            self.play_index = 0
            self.slider.set(0)
            self.slider_adjust = False
            snap = self._snapshot_for_playback(self.play_index)
            self.apply_snapshot(snap, highlight_ts=snap.ts, playback_index=self.play_index)
        else:
            self.play_mode = "live"
            self.stop_play_loop()
            self._set_play_controls_state(False)
            self.playback_btn.configure(text="Режим воспроизведения")
            self.time_label.configure(text="LIVE")
            if self.latest_snapshot:
                self.apply_snapshot(self.latest_snapshot, highlight_ts=self.latest_snapshot.ts)

    def toggle_play(self):
        snaps = self._current_playback_snapshots()
        if self.play_mode != "playback" or not snaps:
            return
        if self.play_running:
            self.stop_play_loop()
        else:
            self.play_running = True
            self.play_btn.configure(text="Пауза")
            self._schedule_next_frame()

    def step_playback(self, step: int):
        snaps = self._current_playback_snapshots()
        if self.play_mode != "playback" or not snaps:
            return
        self.stop_play_loop()
        new_idx = max(0, min(len(snaps) - 1, self.play_index + step))
        if new_idx == self.play_index:
            return
        self.play_index = new_idx
        self.slider_adjust = True
        self.slider.set(self.play_index)
        self.slider_adjust = False
        snap = self._snapshot_for_playback(self.play_index)
        self.apply_snapshot(snap, highlight_ts=snap.ts, playback_index=self.play_index)

    def on_slider_move(self, value: str):
        snaps = self._current_playback_snapshots()
        if self.play_mode != "playback" or not snaps or self.slider_adjust:
            return
        try:
            idx = int(float(value) + 0.5)
        except Exception:
            return
        idx = max(0, min(len(snaps) - 1, idx))
        if idx == self.play_index:
            return
        self.play_index = idx
        snap = self._snapshot_for_playback(self.play_index)
        self.apply_snapshot(snap, highlight_ts=snap.ts, playback_index=self.play_index)

    def stop_play_loop(self):
        if self.play_job is not None:
            try:
                self.root.after_cancel(self.play_job)
            except Exception:
                pass
            self.play_job = None
        if self.play_running:
            self.play_running = False
            self.play_btn.configure(text="Play")

    def _trim_history(self) -> None:
        if not self.history:
            return
        limit = max(0.0, float(self.max_history_seconds))
        if limit <= 0.0:
            return
        cutoff = self.history[-1].ts - limit
        while len(self.history) > 1 and self.history[0].ts < cutoff:
            self.history.popleft()

    def _handle_window_change(self, _event: Optional[Any] = None) -> None:
        label = self.window_var.get()
        seconds = self.window_map.get(label)
        if seconds is None:
            seconds = float(self.window_choices[0]) if self.window_choices else 10.0
        try:
            seconds = max(0.1, float(seconds))
        except Exception:
            seconds = 10.0
        self.graph_manager.set_window(seconds)
        self.max_history_seconds = max(self.max_history_seconds, seconds)
        highlight = self.latest_snapshot.ts if self.latest_snapshot else None
        self._refresh_graphs(highlight_ts=highlight)

    def _refresh_graphs(
        self,
        highlight_ts: Optional[float] = None,
        snapshots: Optional[List[Snapshot]] = None,
    ) -> None:
        snaps = snapshots if snapshots is not None else list(self.history)
        self.graph_manager.update(snaps, highlight_ts)

    def _format_recording_label(
        self,
        created_at: float,
        car: str,
        track: str,
        cfg: str,
        duration: float,
    ) -> str:
        try:
            created_dt = datetime.fromtimestamp(created_at)
            created_str = created_dt.strftime("%d.%m %H:%M")
        except Exception:
            created_str = time.strftime("%d.%m %H:%M", time.localtime(created_at))
        track_label = track or "-"
        if cfg:
            track_label = f"{track_label} [{cfg}]"
        dur = max(0.0, float(duration))
        return f"{created_str} — {car or '-'} — {track_label} ({dur:.1f} с)"

    def _refresh_recordings_combo(self, select_index: Optional[int] = None) -> None:
        labels = [run.label for run in self.recordings]
        state = "readonly" if labels else "disabled"
        self.records_cb.configure(values=labels, state=state)
        if not labels:
            self.records_var.set("")
            self.playback_run = None
            self._update_playback_button_state()
            return
        if select_index is None or not (0 <= select_index < len(labels)):
            select_index = len(labels) - 1
        self.records_cb.current(select_index)
        self.records_var.set(labels[select_index])
        self.playback_run = self.recordings[select_index]
        self._update_slider_range()
        self._update_playback_button_state()

    def _update_playback_button_state(self) -> None:
        if self.recordings:
            self.playback_btn.state(["!disabled"])
        else:
            self.playback_btn.state(["disabled"])
            if self.play_mode == "playback":
                self.play_mode = "live"
                self.stop_play_loop()
                self._set_play_controls_state(False)
                self.playback_btn.configure(text="Режим воспроизведения")
                self.time_label.configure(text="LIVE")

    def _set_play_controls_state(self, enabled: bool) -> None:
        widgets = (self.prev_btn, self.play_btn, self.next_btn)
        states = (["!disabled"] if enabled else ["disabled"])
        for widget in widgets:
            try:
                widget.state(states)
            except Exception:
                pass
        try:
            self.slider.state(states)
        except Exception:
            pass
        if not enabled:
            self.play_btn.configure(text="Play")

    def _update_slider_range(self) -> None:
        snaps = self._current_playback_snapshots()
        total = max(0, len(snaps) - 1)
        self.slider.configure(from_=0, to=total)
        self.play_index = min(self.play_index, total)
        self.slider_adjust = True
        self.slider.set(self.play_index if total > 0 else 0)
        self.slider_adjust = False

    def _schedule_next_frame(self) -> None:
        if not self.play_running:
            return
        snaps = self._current_playback_snapshots()
        if not snaps or self.play_index >= len(snaps) - 1:
            self.stop_play_loop()
            return

        current = snaps[self.play_index]
        nxt = snaps[self.play_index + 1]
        delta = max(0.02, float(nxt.ts - current.ts))
        delay_ms = int(delta * 1000)

        def _advance():
            if not self.play_running:
                return
            snaps_inner = self._current_playback_snapshots()
            if not snaps_inner:
                self.stop_play_loop()
                return
            if self.play_index >= len(snaps_inner) - 1:
                self.stop_play_loop()
                return
            self.play_index += 1
            self.slider_adjust = True
            self.slider.set(self.play_index)
            self.slider_adjust = False
            snap = self._snapshot_for_playback(self.play_index)
            self.apply_snapshot(snap, highlight_ts=snap.ts, playback_index=self.play_index)
            self._schedule_next_frame()

        try:
            self.play_job = self.root.after(max(20, delay_ms), _advance)
        except Exception:
            self.stop_play_loop()

    def _handle_recording_select(self, _event: Optional[Any] = None) -> None:
        idx = self.records_cb.current()
        if idx < 0 or idx >= len(self.recordings):
            return
        self.playback_run = self.recordings[idx]
        self.play_index = 0
        self._update_slider_range()
        if self.play_mode == "playback" and self.playback_run.snapshots:
            snap = self.playback_run.snapshots[0]
            self.apply_snapshot(snap, highlight_ts=snap.ts, playback_index=0)
        else:
            self.time_label.configure(text="LIVE")

    def _current_playback_snapshots(self) -> List[Snapshot]:
        if self.playback_run and self.playback_run.snapshots:
            return list(self.playback_run.snapshots)
        return []

    def _snapshot_for_playback(self, index: int) -> Snapshot:
        snaps = self._current_playback_snapshots()
        if not snaps:
            raise IndexError("Нет сохранённых срезов")
        index = max(0, min(len(snaps) - 1, index))
        return snaps[index]

    def apply_snapshot(
        self,
        snap: Snapshot,
        *,
        highlight_ts: Optional[float] = None,
        playback_index: Optional[int] = None,
    ) -> None:
        data = snap.data
        car = data.get("carModel") or "-"
        track = data.get("track") or "-"
        cfg = data.get("trackConfig") or ""
        title = f"Car: {car}   Track: {track}"
        if cfg:
            title += f" [{cfg}]"
        self.lbl_title.configure(text=title)

        status = self.mpanel.load_assets_if_needed(track, cfg)
        if status:
            self.lbl_map_status.configure(text=status)

        car_pos_raw = data.get("car_pos")
        car_pos: Optional[Tuple[float, float]] = None
        if isinstance(car_pos_raw, (list, tuple)) and len(car_pos_raw) >= 2:
            try:
                car_pos = (float(car_pos_raw[0]), float(car_pos_raw[1]))
            except Exception:
                car_pos = None
        self.mpanel.set_car_position(car_pos)

        trail_img: List[Tuple[float, float]] = []
        trail_raw = data.get("trail") or []
        for item in trail_raw:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                x, z = float(item[0]), float(item[1])
            except Exception:
                continue
            trail_img.append(self.mpanel.world_to_img(x, z))
        self.mpanel.trail_img = trail_img
        self.mpanel.redraw()

        track_label = track if not cfg else f"{track} ({cfg})"
        self.set_label("car", car)
        self.set_label("track", track_label)

        lap = data.get("lap")
        pos = data.get("position")
        lap_text = f"{lap}" if lap is not None else "-"
        pos_text = f"{pos}" if pos is not None else "-"
        self.set_label("lp", f"{lap_text} / {pos_text}")

        sector = data.get("sector")
        self.set_label("sec", "-" if sector is None else str(sector))

        cur_t = data.get("time_current") or "--:--.---"
        last_t = data.get("time_last") or "--:--.---"
        best_t = data.get("time_best") or "--:--.---"
        self.set_label("times", f"{cur_t} / {last_t} / {best_t}")

        tyres = data.get("tyreCoreTemperature") or []
        if len(tyres) == 4:
            try:
                tcore_txt = " / ".join(f"{float(v):.0f}" for v in tyres)
            except Exception:
                tcore_txt = "-"
        else:
            tcore_txt = "-"
        self.set_label("tcore", tcore_txt)

        press = data.get("wheelsPressurePsi") or []
        if len(press) == 4:
            press_parts: List[str] = []
            for v in press:
                try:
                    psi_val = float(v)
                except Exception:
                    continue
                press_parts.append(f"{psi_to_bar(psi_val):.1f}/{psi_val:.1f}")
            press_txt = " | ".join(press_parts) if press_parts else "-"
        else:
            press_txt = "-"
        self.set_label("press", press_txt)

        wheels = data.get("wheelLinearKmh") or []
        if len(wheels) == 4:
            try:
                wheels_txt = " / ".join(f"{float(v):.0f}" for v in wheels)
            except Exception:
                wheels_txt = "-"
        else:
            wheels_txt = "-"
        self.set_label("wheelspeed", wheels_txt)

        susp = data.get("suspensionTravel") or []
        if len(susp) == 4:
            try:
                susp_txt = " / ".join(f"{float(v) * 1000.0:.0f}" for v in susp)
            except Exception:
                susp_txt = "-"
        else:
            susp_txt = "-"
        self.set_label("susp", susp_txt)

        ride = data.get("rideHeight") or []
        if len(ride) >= 2:
            try:
                ride_txt = " / ".join(f"{float(v) * 1000.0:.0f}" for v in ride[:2])
            except Exception:
                ride_txt = "-"
        else:
            ride_txt = "-"
        self.set_label("ride", ride_txt)

        load = data.get("wheelLoad") or []
        if len(load) == 4:
            try:
                load_txt = " / ".join(f"{float(v) / 9.80665:.0f}" for v in load)
            except Exception:
                load_txt = "-"
        else:
            load_txt = "-"
        self.set_label("load", load_txt)

        drs_val = data.get("drs")
        self.set_label("drs", "-" if drs_val is None else f"{float(drs_val):.2f}")

        tc_val = data.get("tc")
        self.set_label("tc", "-" if tc_val is None else f"{float(tc_val):.2f}")

        abs_val = data.get("abs")
        self.set_label("abs", "-" if abs_val is None else f"{float(abs_val):.2f}")

        air = data.get("airDensity")
        self.set_label("airrho", "-" if air is None else f"{float(air):.3f}")

        cg = data.get("cgHeight")
        self.set_label("cgh", "-" if cg is None else f"{float(cg) * 100.0:.0f}")

        grip = data.get("surfaceGrip")
        self.set_label("grip", "-" if grip is None else f"{float(grip):.2f}")

        steer = data.get("steerAngle")
        self.set_label("steer", "-" if steer is None else f"{float(steer):.1f}")

        if playback_index is not None and self.play_mode == "playback":
            snaps = self._current_playback_snapshots()
            if snaps:
                elapsed = max(0.0, snap.ts - snaps[0].ts)
                self.time_label.configure(text=f"{elapsed:.1f} с")
        else:
            self.time_label.configure(text="LIVE")

        if playback_index is not None:
            snaps = self._current_playback_snapshots()
            self._refresh_graphs(highlight_ts=highlight_ts, snapshots=snaps)
        else:
            self._refresh_graphs(highlight_ts=highlight_ts)

    def on_new_state(self, data: Dict[str, Any]) -> None:
        if not data:
            return
        timestamp = data.get("timestamp")
        try:
            ts = float(timestamp) if timestamp is not None else time.time()
        except Exception:
            ts = time.time()
        snap = Snapshot(ts, deepcopy(data))
        self.latest_snapshot = snap
        self.history.append(snap)
        self._trim_history()

        if self.recording and self.active_recording is not None:
            self.active_recording.snapshots.append(Snapshot(ts, deepcopy(data)))

        if self.play_mode == "live":
            self.apply_snapshot(snap, highlight_ts=ts)


    root = tk.Tk()
    root.title("AC Telemetry - Advanced")
    root.geometry("1100x760+120+120")
    root.minsize(900, 600)

    outer = ttk.Frame(root, padding=8)
    outer.pack(fill="both", expand=True)

    left = ttk.Frame(outer)
    left.pack(side="left", fill="y", padx=(0, 8))

    controls = ttk.Frame(left)
    controls.pack(fill="x", pady=(0, 6))
    controls.columnconfigure(0, weight=1)
    controls.columnconfigure(1, weight=1)
    controls.columnconfigure(2, weight=1)

    view_btn = ttk.Button(controls, text="Показать графики")
    view_btn.grid(row=0, column=0, padx=(0, 4), sticky="ew")
    record_btn = ttk.Button(controls, text="Начать запись")
    record_btn.grid(row=0, column=1, padx=(0, 4), sticky="ew")
    playback_btn = ttk.Button(controls, text="Режим воспроизведения")
    playback_btn.grid(row=0, column=2, sticky="ew")

    playback_controls = ttk.Frame(controls)
    playback_controls.grid(row=1, column=0, columnspan=3, pady=(4, 0), sticky="ew")
    playback_controls.columnconfigure(3, weight=1)

    prev_btn = ttk.Button(playback_controls, text="<")
    prev_btn.grid(row=0, column=0, padx=(0, 2))
    play_btn = ttk.Button(playback_controls, text="Play")
    play_btn.grid(row=0, column=1, padx=(0, 2))
    next_btn = ttk.Button(playback_controls, text=">")
    next_btn.grid(row=0, column=2, padx=(0, 6))
    slider = ttk.Scale(playback_controls, from_=0, to=0, orient="horizontal")
    slider.grid(row=0, column=3, sticky="ew")
    time_label = ttk.Label(playback_controls, text="LIVE")
    time_label.grid(row=0, column=4, padx=(6, 0))

    ttk.Label(controls, text="Записи:").grid(row=2, column=0, sticky="w", pady=(6, 0))
    records_var = tk.StringVar()
    records_cb = ttk.Combobox(controls, textvariable=records_var, state="disabled", width=34)
    records_cb.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(6, 0))

    ttk.Label(controls, text="Окно графика:").grid(row=3, column=0, sticky="w", pady=(4, 0))
    window_var = tk.StringVar()
    window_cb = ttk.Combobox(controls, textvariable=window_var, state="readonly", width=12)
    window_cb.grid(row=3, column=1, sticky="w", pady=(4, 0))
    window_choices = [5, 10, 20, 30, 60, 120]

    canvasL = tk.Canvas(left, bg="#0f0f10", highlightthickness=0, width=360)
    vsb = ttk.Scrollbar(left, orient="vertical", command=canvasL.yview)
    frm = ttk.Frame(canvasL)
    frame_id = canvasL.create_window((0, 0), window=frm, anchor="nw")
    canvasL.configure(yscrollcommand=vsb.set)
    canvasL.pack(side="left", fill="y")
    vsb.pack(side="left", fill="y")

    def _on_conf(event=None):
        canvasL.configure(scrollregion=canvasL.bbox("all"))
        w = event.width if event else canvasL.winfo_width()
        try:
            canvasL.itemconfig(frame_id, width=w)
        except Exception:
            pass

    frm.bind("<Configure>", _on_conf)
    canvasL.bind("<Configure>", _on_conf)

    def _scroll_cards(event):
        delta = 0
        if hasattr(event, "delta") and event.delta:
            delta = event.delta
        elif hasattr(event, "num") and event.num in (4, 5):
            delta = 120 if event.num == 4 else -120
        if delta:
            canvasL.yview_scroll(int(-delta / 120), "units")
        return "break"

    canvasL.bind("<MouseWheel>", _scroll_cards)
    canvasL.bind("<Button-4>", _scroll_cards)
    canvasL.bind("<Button-5>", _scroll_cards)

    right = ttk.Frame(outer)
    right.pack(side="left", fill="both", expand=True)
    top = ttk.Frame(right)
    top.pack(fill="x")
    lbl_title = ttk.Label(top, text="Car: -   Track: -", font=("Consolas", 12, "bold"))
    lbl_title.pack(side="left")
    lbl_map_status = ttk.Label(top, text="Карта: нет данных")
    lbl_map_status.pack(side="right")

    cv_map = tk.Canvas(right, bg="#0b0b0d", highlightthickness=0, cursor="fleur")
    cv_map.pack(fill="both", expand=True, pady=(8, 0))

    mpanel = MapPanel(cv_map)

    map_controls = ttk.Frame(left)
    map_controls.pack(fill="x", pady=(0, 6))
    center_var = tk.BooleanVar(value=True)

    def _toggle_center() -> None:
        mpanel.set_center_mode(bool(center_var.get()))
        mpanel.reset_view()

    center_chk = ttk.Checkbutton(map_controls, text="Следовать за машиной", variable=center_var, command=_toggle_center)
    center_chk.pack(side="left")
    reset_btn = ttk.Button(map_controls, text="Сбросить вид", command=lambda: mpanel.reset_view())
    reset_btn.pack(side="left", padx=(8, 0))
    center_var.set(True)
    _toggle_center()

    cards_holder = ttk.Frame(frm)
    cards_holder.pack(fill="x", expand=True)
    graphs_holder = ttk.Frame(frm)

    def value_getter(key: str, idx: Optional[int] = None):
        def _inner(data: Dict[str, Any]):
            value = data.get(key)
            if idx is not None:
                try:
                    value = value[idx]
                except Exception:
                    return None
            return value
        return _inner

    graph_configs = [
        {
            "title": "Скорость (km/h)",
            "series": [
                {"name": "Speed", "color": "#4fa3ff", "extract": value_getter("speedKmh")},
            ],
        },
        {
            "title": "Обороты двигателя",
            "series": [
                {"name": "RPM", "color": "#ffae4f", "extract": value_getter("rpm")},
            ],
        },
        {
            "title": "Педали",
            "series": [
                {"name": "Газ", "color": "#5ecb5e", "extract": value_getter("gas")},
                {"name": "Тормоз", "color": "#ff6464", "extract": value_getter("brake")},
            ],
        },
        {
            "title": "Температура шин (°C)",
            "series": [
                {"name": "FL", "color": "#ff7070", "extract": value_getter("tyreCoreTemperature", 0)},
                {"name": "FR", "color": "#ffb470", "extract": value_getter("tyreCoreTemperature", 1)},
                {"name": "RL", "color": "#70baff", "extract": value_getter("tyreCoreTemperature", 2)},
                {"name": "RR", "color": "#70ffac", "extract": value_getter("tyreCoreTemperature", 3)},
            ],
        },
    ]

    graph_manager = GraphManager(graphs_holder)
    graph_manager.build(graph_configs)

    def card(title, keys_and_labels: List[Tuple[str, str]]) -> Dict[str, Any]:
        holder: Dict[str, Any] = {}
        box = ttk.Frame(cards_holder, padding=8)
        box.pack(fill="x", pady=(4, 4))
        box["borderwidth"] = 1
        box["relief"] = "solid"
        ttk.Label(box, text=title, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6), columnspan=2)
        r = 1
        for key, label in keys_and_labels:
            ttk.Label(box, text=label).grid(row=r, column=0, sticky="w")
            val = ttk.Label(box, text="-")
            val.grid(row=r, column=1, sticky="e")
            holder[key] = val
            r += 1
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)
        return holder

    refs: Dict[str, ttk.Label] = {}

    def reg(d: Dict[str, ttk.Label]):
        refs.update(d)

    reg(card("Авто и трасса", [
        ("car", "Автомобиль"),
        ("track", "Трасса"),
        ("lp", "Круг / позиция"),
        ("sec", "Сектор"),
        ("times", "Время (текущее/последнее/лучшее)"),
    ]))
    reg(card("Шины и давление", [
        ("tcore", "Темп. FL/FR/RL/RR (°C)"),
        ("press", "Давление FL/FR/RL/RR (bar/psi)"),
        ("wheelspeed", "Скорость колёс FL/FR/RL/RR (км/ч)"),
    ]))
    reg(card("Подвеска и нагрузка", [
        ("susp", "Ход подвески FL/FR/RL/RR (мм)"),
        ("ride", "Клиренс перед/зад (мм)"),
        ("load", "Нагрузка FL/FR/RL/RR (кг)"),
    ]))
    reg(card("Системы и среда", [
        ("drs", "DRS"),
        ("tc", "TC уровень"),
        ("abs", "ABS уровень"),
        ("airrho", "Плотность воздуха (кг/м³)"),
        ("cgh", "Центр тяжести (см)"),
        ("grip", "Грипп покрытия"),
    ]))
    reg(card("Руль", [
        ("steer", "Угол руля (°)"),
    ]))

    def set_lbl(key: str, text: str):
        lab = refs.get(key)
        if lab:
            lab.configure(text=text)

    controller = AdvancedStateController(
        root=root,
        mpanel=mpanel,
        lbl_title=lbl_title,
        lbl_map_status=lbl_map_status,
        set_label=set_lbl,
        graph_manager=graph_manager,
        view_btn=view_btn,
        record_btn=record_btn,
        playback_btn=playback_btn,
        play_btn=play_btn,
        prev_btn=prev_btn,
        next_btn=next_btn,
        slider=slider,
        time_label=time_label,
        cards_holder=cards_holder,
        graphs_holder=graphs_holder,
        scroll_callback=_on_conf,
    )

    def poll_queue():
        try:
            while True:
                msg = state_queue.get_nowait()
                if isinstance(msg, dict) and msg.get("cmd") == "exit":
                    controller.shutdown()
                    root.destroy()
                    return
                if isinstance(msg, dict) and msg.get("type") == "state":
                    controller.on_new_state(msg.get("data", {}))
        except queue.Empty:
            pass
        root.after(poll_ms, poll_queue)

    root.after(poll_ms, poll_queue)
    root.protocol("WM_DELETE_WINDOW", lambda: (state_queue.put({"cmd": "exit"}), controller.shutdown(), root.destroy()))
    root.mainloop()
# ----------------------------------------------------------------------
# Main program (pygame)
# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Assetto Corsa Telemetry: pygame + Advanced Tk (separate process)")
    ap.add_argument("--hz", type=float, default=50.0, help="Частота опроса SHM, Гц")
    ap.add_argument("--buffer-secs", type=float, default=20.0, help="Глубина графиков, сек")
    ap.add_argument("--no-topmost", action="store_true", help="Не делать окно поверх всех")

    ap.add_argument("--csv", help="Путь к CSV-логу (опционально)")
    ap.add_argument("--udp", help="UDP адрес для JSON-стрима, host:port (опционально)")

    ap.add_argument("--unit", choices=["kmh", "mph", "ms"], default="kmh", help="Единицы скорости")
    ap.add_argument("--speed-source", choices=["auto", "kmh", "vector"], default="auto",
                    help="Источник скорости: speedKmh | |velocity|*3.6 | auto")
    ap.add_argument("--speed-deadzone", type=float, default=0.5, help="Мёртвая зона (км/ч): ниже порога → 0.00")
    ap.add_argument("--speed-ema", type=float, default=0.25, help="EMA сглаживание 0..1")

    BoolAction = getattr(argparse, "BooleanOptionalAction", None)
    if BoolAction:
        ap.add_argument("--adv-window", action=BoolAction, default=True,
                        help="Разрешить продвинутое окно (управляется кнопкой)")
    else:
        ap.add_argument("--adv-window", dest="adv_window", action="store_true", default=True,
                        help="Разрешить Advanced окно (кнопка в главном)")
        ap.add_argument("--no-adv-window", dest="adv_window", action="store_false",
                        help="Запретить Advanced окно")

    ap.add_argument("--adv-poll-ms", type=int, default=100, help="Интервал обновления Advanced, мс")
    ap.add_argument("--ac-root", help="Корень Assetto Corsa (поиск карт)")
    ap.add_argument("--track-map", help="Путь к PNG карте трассы (ручной выбор)")

    args = ap.parse_args()
    if not hasattr(args, "adv_window"):
        setattr(args, "adv_window", True)

    # pygame UI
    pygame.init()
    pygame.display.set_caption("Assetto Corsa — Telemetry")
    screen = pygame.display.set_mode((1280, 780), pygame.RESIZABLE)
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("Consolas", 16)
    fontb = pygame.font.SysFont("Consolas", 20, bold=True)
    is_topmost = not args.no_topmost
    if is_topmost:
        set_topmost_for_pygame_window(True)

    # AC SHM lazy attach
    shm_phys = shm_graph = shm_stat = None
    ac_attached = False
    last_ac_try = 0.0

    def attach_ac() -> bool:
        nonlocal shm_phys, shm_graph, shm_stat, ac_attached
        try:
            shm_phys = SHMReader("acpmf_physics")
            shm_graph = SHMReader("acpmf_graphics")
            shm_stat = SHMReader("acpmf_static")
            ac_attached = True
            print("[AC] SHM attached")
            return True
        except RuntimeError:
            shm_phys = shm_graph = shm_stat = None
            ac_attached = False
            return False

    def detach_ac():
        nonlocal shm_phys, shm_graph, shm_stat, ac_attached
        try:
            if shm_phys: shm_phys.close()
            if shm_graph: shm_graph.close()
            if shm_stat: shm_stat.close()
        except Exception:
            pass
        shm_phys = shm_graph = shm_stat = None
        ac_attached = False
        print("[AC] SHM detached")

    attach_ac()

    # CSV/UDP
    csv_fh = None
    if args.csv:
        csv_fh = open(args.csv, "w", encoding="utf-8", newline="")
        csv_fh.write("ts,car,track,cfg,gear_raw,gear_out,rpm,speed_raw,speed_filt,gas,brake,steer_deg,status,lap,pos,cur,last,best,sector,prsFL,prsFR,prsRL,prsRR\n")
    udp_sock = None; udp_addr = None
    if args.udp:
        try:
            host, port_s = args.udp.rsplit(":", 1)
            udp_addr = (host, int(port_s))
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except Exception as e:
            print("Некорректный --udp, отключаю:", e)

    target_fps = 60
    poll_period = 1.0 / max(1.0, float(args.hz))
    graph_fps = 30.0
    capacity = max(120, min(2400, int(float(args.buffer_secs) * graph_fps)))

    unit_label = {"kmh": "km/h", "mph": "mph", "ms": "m/s"}[args.unit]
    plot_speed = Plot(f"Speed ({unit_label})", capacity)
    plot_speed.add_series(Series("speed", (80, 180, 255)))
    plot_rpm = Plot("RPM", capacity)
    plot_rpm.add_series(Series("rpm", (255, 190, 80)))
    plot_ped = Plot("Pedals", capacity)
    plot_ped.add_series(Series("gas", (120, 220, 120), y_min=0, y_max=1, autoscale=False))
    plot_ped.add_series(Series("brake", (220, 120, 120), y_min=0, y_max=1, autoscale=False))

    def kmh_to_out(v_kmh: float) -> float:
        if args.unit == "kmh": return v_kmh
        if args.unit == "mph": return v_kmh * 0.621371
        return v_kmh / 3.6

    last_poll = 0.0
    last_graph = 0.0
    status_text = "AC:WAIT"
    cur_time = last_time = best_time = ""
    lap = pos = sec = 0
    car_model = "—"
    track_name = "—"
    track_cfg = ""

    gear_raw = 1
    rpm = 0
    gas = 0.0
    brake = 0.0
    steer_deg = 0.0
    speed_kmh_raw = 0.0
    speed_kmh_filt: Optional[float] = None

    trail: deque[Tuple[float, float]] = deque(maxlen=3000)  # world X,Z

    # Advanced process plumbing
    from multiprocessing import Process, Queue, set_start_method
    try:
        set_start_method("spawn", force=True)
    except RuntimeError:
        pass  # already set

    adv_allowed = bool(args.adv_window)
    adv_running = False
    adv_q: Optional[Queue] = None
    adv_proc: Optional[Process] = None

    ac_roots = guess_ac_roots(args.ac_root)
    manual_map = str(Path(args.track_map).resolve()) if args.track_map else None

    def open_advanced():
        nonlocal adv_running, adv_q, adv_proc
        if not adv_allowed:
            return
        if adv_running:
            if adv_proc and adv_proc.is_alive():
                return
            close_advanced()
        adv_q = Queue(maxsize=2)
        adv_proc = Process(target=advanced_process_main,
                           args=(adv_q, [str(p) for p in ac_roots], manual_map, int(args.adv_poll_ms)),
                           daemon=True)
        adv_proc.start()
        adv_running = True

    def close_advanced():
        nonlocal adv_running, adv_q, adv_proc
        q = adv_q
        p = adv_proc
        if q:
            try:
                q.put({"cmd": "exit"}, block=False)
            except Exception:
                pass
        if p:
            p.join(timeout=1.0)
            if p.is_alive():
                p.terminate()
                p.join(timeout=0.5)
        if q:
            try:
                q.close()
                q.join_thread()
            except Exception:
                pass
        adv_proc = None
        adv_q = None
        adv_running = False

    # UI button rect
    btn_rect = pygame.Rect(0, 0, 0, 0)

    def draw_button(surface, rect, text, on):
        pygame.draw.rect(surface, (40, 120, 60) if on else (90, 90, 90), rect, border_radius=8)
        pygame.draw.rect(surface, (30, 30, 30), rect, 2, border_radius=8)
        txt = fontb.render(text, True, (255, 255, 255))
        surface.blit(txt, (rect.centerx - txt.get_width() // 2, rect.centery - txt.get_height() // 2))

    running = True
    while running:
        now = time.time()

        if not ac_attached and now - last_ac_try >= 2.0:
            last_ac_try = now
            attach_ac()

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    running = False
                elif ev.key == pygame.K_F11:
                    is_topmost = not is_topmost
                    set_topmost_for_pygame_window(is_topmost)
                elif ev.key == pygame.K_c and (pygame.key.get_mods() & pygame.KMOD_CTRL):
                    for p in (plot_speed, plot_rpm, plot_ped):
                        for s in p.series:
                            s.buf.clear()
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                if adv_allowed and btn_rect.collidepoint(ev.pos):
                    if adv_running and adv_proc and not adv_proc.is_alive():
                        close_advanced()
                    if adv_running:
                        close_advanced()
                    else:
                        open_advanced()
            elif ev.type == pygame.VIDEORESIZE:
                screen = pygame.display.set_mode((ev.w, ev.h), pygame.RESIZABLE)
                if is_topmost:
                    set_topmost_for_pygame_window(True)

        # poll AC
        if now - last_poll >= poll_period:
            last_poll = now
            if ac_attached and shm_phys and shm_graph and shm_stat:
                try:
                    p = shm_phys.copy_into(SPageFilePhysics)
                    g = shm_graph.copy_into(SPageFileGraphics)
                    s = shm_stat.copy_into(SPageFileStatic)

                    status_text = AC_STATUS.get(int(g.status), "?")
                    cur_time = wstr(g.currentTime)
                    last_time = wstr(g.lastTime)
                    best_time = wstr(g.bestTime)
                    lap = int(g.completedLaps)
                    pos = int(g.position)
                    sec = int(g.currentSectorIndex)

                    car_model = wstr(s.carModel) or car_model
                    track_name = wstr(s.track) or track_name
                    track_cfg = wstr(s.trackConfig) or ""

                    gear_raw = int(p.gear)
                    rpm = int(p.rpms)
                    gas = max(0.0, min(1.0, float(p.gas)))
                    brake = max(0.0, min(1.0, float(p.brake)))

                    kmh_field = float(p.speedKmh)
                    vx, vy, vz = float(p.velocity[0]), float(p.velocity[1]), float(p.velocity[2])
                    kmh_vec = math.sqrt(vx * vx + vy * vy + vz * vz) * 3.6
                    if args.speed_source == "kmh" or (args.speed_source == "auto" and (kmh_field > 0.05 or kmh_vec < 0.1)):
                        raw_kmh = kmh_field
                    else:
                        raw_kmh = kmh_vec
                    if abs(raw_kmh) < max(0.0, float(args.speed_deadzone)):
                        raw_kmh = 0.0
                    speed_kmh_raw = raw_kmh
                    alpha = min(max(float(args.speed_ema), 0.0), 1.0)
                    speed_kmh_filt = (speed_kmh_raw if speed_kmh_filt is None
                                      else (speed_kmh_filt + alpha * (speed_kmh_raw - speed_kmh_filt)) if alpha > 0.0
                                      else speed_kmh_raw)

                    steer_deg = float(p.steerAngle)

                    car_x = float(g.carCoordinates[0])
                    car_z = float(g.carCoordinates[2])
                    trail.append((car_x, car_z))

                    if hasattr(s, "tyreRadius") and s.tyreRadius:
                        tyreR = [float(s.tyreRadius[i]) for i in range(4)]
                    else:
                        tyreR = [0.33, 0.33, 0.33, 0.33]
                    wa = [float(p.wheelAngularSpeed[i]) for i in range(4)]
                    vlin = [wa[i] * tyreR[i] * 3.6 for i in range(4)]
                    prs_psi = [float(p.wheelsPressure[i]) for i in range(4)]

                    # send state to Advanced (latest only)
                    if adv_running and adv_q is not None:
                        state_payload = {
                            "type": "state",
                            "data": {
                                "carModel": car_model, "track": track_name, "trackConfig": track_cfg,
                                "lap": lap, "position": pos, "sector": sec,
                                "time_current": cur_time or "--:--.---",
                                "time_last": last_time or "--:--.---",
                                "time_best": best_time or "--:--.---",
                                "suspensionTravel": [float(p.suspensionTravel[i]) for i in range(4)],
                                "rideHeight": [float(p.rideHeight[i]) for i in range(2)],
                                "wheelLoad": [float(p.wheelLoad[i]) for i in range(4)],
                                "camberRAD": [float(p.camberRAD[i]) for i in range(4)],
                                "wheelAngularSpeed": wa,
                                "wheelLinearKmh": vlin,
                                "wheelsPressurePsi": prs_psi,
                                "tyreCoreTemperature": [float(p.tyreCoreTemperature[i]) for i in range(4)],
                                "drs": float(p.drs), "tc": float(p.tc), "abs": float(p.abs),
                                "airDensity": float(p.airDensity), "cgHeight": float(p.cgHeight),
                                "surfaceGrip": float(g.surfaceGrip),
                                "steerAngle": steer_deg,
                                "timestamp": now,
                                "speedKmh": float(speed_kmh_filt if speed_kmh_filt is not None else speed_kmh_raw),
                                "rpm": rpm,
                                "gas": gas,
                                "brake": brake,
                                "car_pos": [car_x, car_z],
                                "trail": list(trail),
                            }
                        }
                        try:
                            # drop stale item to keep only latest
                            try:
                                while True:
                                    adv_q.get_nowait()
                            except queue.Empty:
                                pass
                            adv_q.put_nowait(state_payload)
                        except Exception:
                            pass

                    if csv_fh:
                        csv_fh.write(
                            "{ts:.3f},{car},{trk},{cfg},{gr},{go},{rpm:d},{raw:.3f},{filt:.3f},{gas:.3f},{brk:.3f},{sw:.1f},{st},{lap:d},{pos:d},{cur},{last},{best},{sec:d},{p0:.1f},{p1:.1f},{p2:.1f},{p3:.1f}\n".format(
                                ts=now, car=car_model, trk=track_name, cfg=track_cfg,
                                gr=gear_raw, go=gear_text_offset(gear_raw), rpm=rpm,
                                raw=speed_kmh_raw, filt=(speed_kmh_filt or 0.0), gas=gas, brk=brake, sw=steer_deg,
                                st=status_text, lap=lap, pos=pos, cur=cur_time, last=last_time, best=best_time, sec=sec,
                                p0=prs_psi[0], p1=prs_psi[1], p2=prs_psi[2], p3=prs_psi[3]
                            )
                        )
                    if udp_sock and udp_addr:
                        payload = {
                            "t": now, "status": status_text, "carModel": car_model, "track": track_name, "trackConfig": track_cfg,
                            "physics": {"gearRaw": gear_raw, "gear": gear_text_offset(gear_raw), "rpm": rpm,
                                        "speedKmh": speed_kmh_filt, "gas": gas, "brake": brake,
                                        "steerDeg": steer_deg, "steerSrc": "AC"},
                            "graphics": {"lap": lap, "position": pos, "sector": sec,
                                         "time": {"current": cur_time, "last": last_time, "best": best_time}}
                        }
                        try:
                            udp_sock.sendto(json.dumps(payload).encode("utf-8"), udp_addr)
                        except Exception:
                            pass

                except Exception:
                    detach_ac()
                    status_text = "AC:WAIT"
                    cur_time = last_time = best_time = ""
                    lap = pos = sec = 0
                    speed_kmh_raw = 0.0
                    speed_kmh_filt = 0.0
                    rpm = 0
                    gas = brake = 0.0
                    gear_raw = 1
                    steer_deg = 0.0
                    trail.clear()
            else:
                status_text = "AC:WAIT"
                alpha = min(max(float(args.speed_ema), 0.0), 1.0)
                speed_kmh_filt = (0.0 if speed_kmh_filt is None
                                  else (speed_kmh_filt + alpha * (0.0 - speed_kmh_filt)) if alpha > 0.0
                                  else 0.0)
                rpm = 0
                gas = brake = 0.0
                gear_raw = 1
                steer_deg = 0.0

        # push to plots
        if now - last_graph >= (1.0 / graph_fps):
            last_graph = now
            plot_speed.set_title(f"Speed ({unit_label})")
            plot_speed.push(0, kmh_to_out(speed_kmh_filt or 0.0))
            plot_rpm.push(0, float(rpm))
            plot_ped.push(0, float(gas))
            plot_ped.push(1, float(brake))

        # draw main UI
        screen.fill((12, 12, 12))
        W, H = screen.get_width(), screen.get_height()
        gap = 10; top_h = 58
        right_w = max(460, int(W * 0.36))
        left_w = W - right_w - gap * 3
        cell_h = (H - top_h - gap * 4) // 3
        x0 = gap; y0 = top_h

        header_rect = pygame.Rect(gap, gap, W - gap * 2, top_h - gap)
        pygame.draw.rect(screen, (18, 18, 18), header_rect, border_radius=8)
        pygame.draw.rect(screen, (60, 60, 60), header_rect, 1, border_radius=8)

        btn_w, btn_h = 220, 36
        btn_rect = pygame.Rect(header_rect.right - btn_w - 12, header_rect.centery - btn_h // 2, btn_w, btn_h)
        draw_button(screen, btn_rect, f"Advanced: {'ON' if adv_running else 'OFF'}", adv_running)

        head_text = f"Car: {car_model}   Track: {track_name}" + (f" [{track_cfg}]" if track_cfg else "")
        head_surf = fontb.render(ellipsize(head_text, fontb, btn_rect.left - header_rect.left - 20), True, (230, 230, 230))
        screen.blit(head_surf, (header_rect.left + 12, header_rect.centery - head_surf.get_height() // 2))

        r_speed = pygame.Rect(x0, y0, left_w, cell_h)
        r_rpm = pygame.Rect(x0, y0 + cell_h + gap, left_w, cell_h)
        r_ped = pygame.Rect(x0, y0 + 2 * (cell_h + gap), left_w, cell_h)
        plot_speed.draw(screen, r_speed, fontb)
        plot_rpm.draw(screen, r_rpm, fontb)
        plot_ped.draw(screen, r_ped, fontb)

        r_info = pygame.Rect(x0 + left_w + gap, y0, right_w, H - y0 - gap)
        pygame.draw.rect(screen, (18, 18, 18), r_info, border_radius=8)
        pygame.draw.rect(screen, (60, 60, 60), r_info, 1, border_radius=8)

        def info_line(lbl: str, val: str, y: int, bold: bool = False) -> int:
            label_max = r_info.width // 2 - 20
            value_max = r_info.width // 2 - 20
            f = fontb if bold else font
            lbl_text = ellipsize(lbl, f, label_max)
            val_text = ellipsize(val, f, value_max)
            screen.blit(f.render(lbl_text, True, (185, 185, 185)), (r_info.left + 12, y))
            screen.blit(f.render(val_text, True, (255, 255, 255)), (r_info.left + r_info.width // 2, y))
            return y + (f.get_height() + 6)

        speed_out = kmh_to_out(speed_kmh_filt or 0.0)
        fmt_speed = "{:.2f} " + unit_label
        yy = r_info.top + 12
        yy = info_line("Статус", status_text, yy, True)
        yy = info_line("Круг / Позиция", f"{lap} / {pos}", yy)
        yy = info_line("Сектор", f"{sec}", yy)
        yy = info_line("Время круга", (cur_time or "--:--.---"), yy)
        yy = info_line("Прошлый / Лучший", f"{last_time or '--:--.---'} / {best_time or '--:--.---'}", yy)
        yy += 8
        yy = info_line("Скорость", fmt_speed.format(speed_out), yy, True)
        yy = info_line("Обороты", f"{rpm}", yy)
        yy = info_line("Передача", f"{gear_text_offset(gear_raw)} (raw:{gear_raw})", yy)
        yy += 8
        yy = info_line("Педали", f"Газ {gas*100:4.0f}%   Тормоз {brake*100:4.0f}%", yy)
        yy = info_line("Руль (из игры)", f"{steer_deg:+.1f}°", yy)

        tips = [
            "ESC — выход, Ctrl+C — очистить графики, F11 — поверх всех.",
            "Кнопка Advanced — включает отдельный процесс окна телеметрии.",
            "Карта: map.png / ui/outline.png + data/map.ini; side_l/side_r — границы трассы.",
            "В окне карты: средняя кнопка — панорама, колесо — зум.",
        ]
        ytips = r_info.bottom - 10
        for t in reversed(tips):
            lines = wrap_text(t, font, r_info.width - 24)
            for ln in reversed(lines):
                surf = font.render(ln, True, (150, 150, 150))
                ytips -= surf.get_height()
                screen.blit(surf, (r_info.left + 12, ytips))
            ytips -= 6

        pygame.display.flip()
        clock.tick(target_fps)

        # monitor advanced process state
        if adv_running and adv_proc and not adv_proc.is_alive():
            close_advanced()

    # cleanup
    if csv_fh:
        try: csv_fh.flush()
        except Exception: pass
        csv_fh.close()
    if ac_attached:
        detach_ac()
    if adv_running:
        close_advanced()
    pygame.quit()


if __name__ == "__main__":
    main()

