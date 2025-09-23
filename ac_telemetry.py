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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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
                ox=float(vals["X_OFFSET"]), oz=float(vals["Z_OFFSET"]), invert_y=True)


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
            self.map_w = 1
            self.map_h = 1
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

            self.cv.bind("<Configure>", lambda e: self.fit_to_view(force=True))
            self.cv.bind("<ButtonPress-2>", self._start_pan)
            self.cv.bind("<B2-Motion>", self._do_pan)
            self.cv.bind("<MouseWheel>", self._zoom)

        def _start_pan(self, e): self.pan_start = (e.x, e.y)

        def _do_pan(self, e):
            if not self.pan_start: return
            sx, sy = self.pan_start
            self.view_dx += (e.x - sx)
            self.view_dy += (e.y - sy)
            self.pan_start = (e.x, e.y)
            self.redraw()

        def _zoom(self, e):
            factor = 1.1 if e.delta > 0 else 0.9
            mx, my = e.x, e.y
            ix = (mx - self.view_dx) / (self.view_scale or 1.0)
            iy = (my - self.view_dy) / (self.view_scale or 1.0)
            self.view_scale *= factor
            self.view_dx = mx - ix * self.view_scale
            self.view_dy = my - iy * self.view_scale
            self.redraw()

        def fit_to_view(self, force=False):
            cw = self.cv.winfo_width()
            ch = self.cv.winfo_height()
            if cw <= 2 or ch <= 2: return
            s = min(cw / max(1, self.map_w), ch / max(1, self.map_h))
            self.view_scale = s
            self.view_dx = (cw - self.map_w * s) / 2
            self.view_dy = (ch - self.map_h * s) / 2
            if force:
                self.redraw()

        def world_to_img(self, x: float, z: float) -> Tuple[float, float]:
            T = self.transform
            if not T: return x, z
            px = T["ox"] + x * T["sx"]
            py = T["oz"] + z * T["sx"]
            if T.get("invert_y", True):
                py = T["h"] - py
            return px, py

        def load_assets_if_needed(self, track_name: Optional[str], track_cfg: Optional[str]) -> Optional[str]:
            changed = (track_name != self.last_track) or (track_cfg != self.last_cfg)
            if not changed:
                return None
            self.last_track, self.last_cfg = track_name, track_cfg

            self.map_img_tk = None
            self.map_img_pil = None
            self.map_w = self.map_h = 1
            self.sideL_img = []
            self.sideR_img = []
            self.trail_img = []

            # find assets
            if manual_map and manual_map.exists():
                self.track_assets = TrackAssets()
                self.track_assets.base = manual_map.parent
                self.track_assets.map_png = manual_map
                self.track_assets.transform = dict(w=1024.0, h=1024.0, sx=1.0, ox=512.0, oz=512.0, invert_y=True)
            else:
                self.track_assets = find_track_assets(track_name, track_cfg, ac_roots)
            self.transform = (self.track_assets.transform if self.track_assets and self.track_assets.transform else None)

            status = "Карта: неизвестна"
            img_path = None
            if self.track_assets:
                img_path = self.track_assets.map_png or self.track_assets.outline_png

            if img_path and img_path.exists():
                try:
                    if PIL_OK and Image is not None and ImageTk is not None:
                        self.map_img_pil = Image.open(img_path)
                        self.map_w, self.map_h = self.map_img_pil.size
                        self.map_img_tk = ImageTk.PhotoImage(self.map_img_pil)
                    else:
                        self.map_img_tk = tk.PhotoImage(file=str(img_path))
                        self.map_w = self.map_img_tk.width()
                        self.map_h = self.map_img_tk.height()
                    status = f"Карта: {self.track_assets.base.name if self.track_assets and self.track_assets.base else '?'}"
                except Exception:
                    status = "Карта: ошибка загрузки"
            else:
                if self.track_assets:
                    status = "Карта: не найдена (рисую траекторию)"
                else:
                    status = "Карта: неизвестна"

            # load side lines
            if self.track_assets and self.track_assets.side_l and self.track_assets.side_r:
                Lw = read_side_csv_points(self.track_assets.side_l)
                Rw = read_side_csv_points(self.track_assets.side_r)
                self.sideL_img = [self.world_to_img(x, z) for (x, z) in Lw]
                self.sideR_img = [self.world_to_img(x, z) for (x, z) in Rw]

            self.fit_to_view(force=True)
            return status

        def redraw(self):
            self.cv.delete("all")
            # background image
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
                w = self.cv.winfo_width(); h = self.cv.winfo_height()
                self.cv.create_rectangle(10, 10, w - 10, h - 10, outline="#333")

            def draw_poly(pts, color="#ffcc00", width=2):
                if not pts: return
                L: List[float] = []
                s = self.view_scale; dx = self.view_dx; dy = self.view_dy
                for x, y in pts:
                    L.extend([dx + x * s, dy + y * s])
                if len(L) >= 4:
                    self.cv.create_line(*L, fill=color, width=width, capstyle="round", joinstyle="round")

            if self.sideL_img: draw_poly(self.sideL_img, "#ffcc00", 2)
            if self.sideR_img: draw_poly(self.sideR_img, "#ffcc00", 2)
            if self.trail_img:
                draw_poly(self.trail_img, "#00e5ff", 2)
                x, y = self.trail_img[-1]
                cx = self.view_dx + x * self.view_scale
                cy = self.view_dy + y * self.view_scale
                self.cv.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="#fff", fill="#ff0", width=2)
                self.cv.create_text(cx + 10, cy - 10, text="YOU", fill="#fff", anchor="w")

    # ---- Build Tk UI (in this process main thread) ----
    root = tk.Tk()
    root.title("AC Telemetry — Advanced")
    root.geometry("1100x760+120+120")
    root.minsize(900, 600)

    outer = ttk.Frame(root, padding=8)
    outer.pack(fill="both", expand=True)

    left = ttk.Frame(outer)
    left.pack(side="left", fill="y", padx=(0, 8))
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
    canvasL.bind_all("<MouseWheel>", lambda e: canvasL.yview_scroll(int(-1 * (e.delta / 120)), "units"))

    right = ttk.Frame(outer)
    right.pack(side="left", fill="both", expand=True)
    top = ttk.Frame(right)
    top.pack(fill="x")
    lbl_title = ttk.Label(top, text="Car: —   Track: —", font=("Consolas", 12, "bold"))
    lbl_title.pack(side="left")
    lbl_map_status = ttk.Label(top, text="Карта: поиск…")
    lbl_map_status.pack(side="right")

    cv_map = tk.Canvas(right, bg="#0b0b0d", highlightthickness=0, cursor="fleur")
    cv_map.pack(fill="both", expand=True, pady=(8, 0))

    mpanel = MapPanel(cv_map)

    # Cards
    def card(title, keys_and_labels: List[Tuple[str, str]]) -> Dict[str, Any]:
        holder: Dict[str, Any] = {}
        box = ttk.Frame(frm, padding=8)
        box.pack(fill="x", pady=(4, 4))
        box["borderwidth"] = 1
        box["relief"] = "solid"
        ttk.Label(box, text=title, font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 6), columnspan=2)
        r = 1
        for key, label in keys_and_labels:
            ttk.Label(box, text=label).grid(row=r, column=0, sticky="w")
            val = ttk.Label(box, text="—")
            val.grid(row=r, column=1, sticky="e")
            holder[key] = val
            r += 1
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=1)
        return holder

    refs: Dict[str, ttk.Label] = {}

    def reg(d: Dict[str, ttk.Label]): refs.update(d)

    reg(card("Сессия и машина", [
        ("car", "Машина"),
        ("track", "Трасса"),
        ("lp", "Круг / Позиция"),
        ("sec", "Сектор"),
        ("times", "Время (тек./прошл./лучший)"),
    ]))
    reg(card("Шины и скорость колёс", [
        ("tcore", "Темп. ядра FL/FR/RL/RR (°C)"),
        ("press", "Давление FL/FR/RL/RR (bar/psi)"),
        ("wheelspeed", "Скорость колёс FL/FR/RL/RR (км/ч)"),
    ]))
    reg(card("Подвеска и нагрузки", [
        ("susp", "Ход подвески FL/FR/RL/RR (см)"),
        ("ride", "Клиренс перед/зад (см)"),
        ("load", "Нагрузка FL/FR/RL/RR (кг)"),
    ]))
    reg(card("Аэродинамика/электроника", [
        ("drs", "DRS"),
        ("tc", "TC активность"),
        ("abs", "ABS активность"),
        ("airrho", "Плотность воздуха (кг/м³)"),
        ("cgh", "Высота ЦТ (см)"),
        ("grip", "Грип покрытия"),
    ]))
    reg(card("Руль", [
        ("steer", "Поворот руля (°)"),
    ]))

    latest: Dict[str, Any] = {}

    def set_lbl(key: str, text: str):
        lab = refs.get(key)
        if lab:
            lab.configure(text=text)

    def poll_queue():
        nonlocal latest
        drained = False
        try:
            while True:
                msg = state_queue.get_nowait()
                if isinstance(msg, dict) and msg.get("cmd") == "exit":
                    root.destroy()
                    return
                if isinstance(msg, dict) and msg.get("type") == "state":
                    latest = msg.get("data", {})
                    drained = True
        except queue.Empty:
            pass

        if drained and latest:
            car = latest.get("carModel", "—")
            track = latest.get("track", "—")
            cfg = latest.get("trackConfig", "")
            lbl_title.configure(text=f"Car: {car}   Track: {track}" + (f" [{cfg}]" if cfg else ""))

            status = mpanel.load_assets_if_needed(track if track and track != "—" else None, cfg if cfg else None)
            if status:
                lbl_map_status.configure(text=status)

            set_lbl("car", car)
            set_lbl("track", track + (f" [{cfg}]" if cfg else ""))
            set_lbl("lp", f"{latest.get('lap', 0)} / {latest.get('position', 0)}")
            set_lbl("sec", f"{latest.get('sector', 0)}")
            set_lbl("times", f"{latest.get('time_current','--:--.---')} / {latest.get('time_last','--:--.---')} / {latest.get('time_best','--:--.---')}")

            tcore = latest.get("tyreCoreTemperature", [0, 0, 0, 0])
            prspsi = latest.get("wheelsPressurePsi", [0, 0, 0, 0])
            prsbar = [psi_to_bar(x) for x in prspsi]
            wlin = latest.get("wheelLinearKmh", [0, 0, 0, 0])

            set_lbl("tcore", " / ".join(f"{x:.1f}" for x in tcore))
            set_lbl("press", " / ".join(f"{b:.2f}/{p:.1f}" for b, p in zip(prsbar, prspsi)))
            set_lbl("wheelspeed", " / ".join(f"{v:.1f}" for v in wlin))

            sus = latest.get("suspensionTravel", [0, 0, 0, 0])
            rh = latest.get("rideHeight", [0, 0])
            load = latest.get("wheelLoad", [0, 0, 0, 0])
            set_lbl("susp", " / ".join(f"{x*100:.1f}" for x in sus))
            set_lbl("ride", " / ".join(f"{x*100:.1f}" for x in rh))
            set_lbl("load", " / ".join(f"{x/9.81:.0f}" for x in load))

            set_lbl("drs", "ON" if latest.get("drs", 0) > 0.5 else "off")
            set_lbl("tc", f"{latest.get('tc', 0):.2f}")
            set_lbl("abs", f"{latest.get('abs', 0):.2f}")
            set_lbl("airrho", f"{latest.get('airDensity', 0):.3f}")
            set_lbl("cgh", f"{latest.get('cgHeight', 0)*100:.1f}")
            set_lbl("grip", f"{latest.get('surfaceGrip', 0):.2f}")
            set_lbl("steer", f"{latest.get('steerAngle', 0.0):+.1f}")

            trail_world: List[Tuple[float, float]] = latest.get("trail", [])
            if mpanel.transform and (mpanel.map_img_tk or mpanel.map_img_pil):
                mpanel.trail_img = [mpanel.world_to_img(x, z) for (x, z) in trail_world]
            else:
                mpanel.trail_img = trail_world
            mpanel.redraw()

        root.after(poll_ms, poll_queue)

    root.after(poll_ms, poll_queue)
    root.protocol("WM_DELETE_WINDOW", lambda: (state_queue.put({"cmd": "exit"}), root.destroy()))
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
        if not adv_allowed or adv_running:
            return
        adv_q = Queue(maxsize=2)
        adv_proc = Process(target=advanced_process_main,
                           args=(adv_q, [str(p) for p in ac_roots], manual_map, int(args.adv_poll_ms)),
                           daemon=True)
        adv_proc.start()
        adv_running = True

    def close_advanced():
        nonlocal adv_running, adv_q, adv_proc
        if adv_q:
            try:
                adv_q.put({"cmd": "exit"}, block=False)
            except Exception:
                pass
        if adv_proc:
            adv_proc.join(timeout=1.0)
            if adv_proc.is_alive():
                adv_proc.terminate()
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

                    trail.append((float(g.carCoordinates[0]), float(g.carCoordinates[2])))

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
            adv_running = False
            adv_q = None
            adv_proc = None

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
