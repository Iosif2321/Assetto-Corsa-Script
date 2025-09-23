# moza_buttons_mapper.py (v7)
# - Низкоуровневый SendInput (scancode).
# - Ограничение по активному окну/процессу (игра/CM), чтобы не лезть в музыку.
# - Нумерация кнопок КАК НА РУЛЕ (1-based) — сам перевожу в 0-based.
# - Привязки:
#     22: короткое -> F1 (смена камеры), долгое -> удержание W (взгляд назад), LONG=250 мс
#     8  -> удержание Q
#     6  -> удержание E
#     19: КОРОТКО -> L, tiny-gap, снова L (однократный "миг"),
#         ДОЛГО -> повторять такие "миги" с коротким интервалом, пока держишь
#     1 + (8/7/6/5) -> (←/↓/→/↑), многократно, пока держишь 1

import argparse, time, sys, ctypes
from typing import Dict, Any, Optional, Set, List, Tuple

import pygame

# ==========================
# НАСТРОЙКИ
# ==========================

ONE_BASED_LABELS   = True
LONG_PRESS_MS      = 250      # порог "долгого"
ARROW_COMBO_WINDOW = 120      # окно для мод-комбо (мс)
VERBOSE            = True

# Ограничение "слать только в игру / Content Manager"
ALLOWED_TITLE_SUBSTR = ["assetto corsa", "content manager"]
ALLOWED_PROC_SUBSTR  = ["acs.exe", "assettocorsa"]

# Параметры мигания светом (L)
PULSE_ON_MS      = 30   # держать L в каждом тапе
PULSE_GAP_MS     = 35   # пауза между двумя тапами внутри одного мигания
PULSE_PERIOD_MS  = 90   # период повторения миганий при удержании

# ===== Конфиг (номера КАК НА РУЛЕ) =====
SINGLE_ACTIONS_1B: Dict[int, Dict[str, Any]] = {
    22: {  # короткое F1, долгий -> удержание W
        'short_scancode': ['F1'],
        'hold_after_long_scancode': ['W'],
    },
    8:  {  # удержание Q
        'hold_scancode': ['Q'],
    },
    6:  {  # удержание E
        'hold_scancode': ['E'],
    },
    # 19: короткий "миг" светом (L ..gap.. L), долгий — повторять "миги"
    19: {
        'short_pulse_scancode': ['L'],                 # два тапа L с маленькой задержкой
        'hold_repeat_pulse_scancode': ['L'],           # повторять такие "миги" при удержании
        'repeat_after_long': True,                     # начинать серию только после LONG_PRESS_MS
    },
}

# Модификатор: держим 1 и жмём 8/7/6/5 -> ←/↓/→/↑
MODIFIER_BTN_LABEL = 1
ARROW_COMBO_LABELS = { 8: 'LEFT', 7: 'DOWN', 6: 'RIGHT', 5: 'UP' }

# ==========================
# SCANCODE SENDINPUT
# ==========================

SCANCODES = {
    'F1':    (0x3B, False),
    'W':     (0x11, False),
    'Q':     (0x10, False),
    'E':     (0x12, False),
    'L':     (0x26, False),
    'ALT':   (0x38, False),

    'LEFT':  (0x4B, True),
    'RIGHT': (0x4D, True),
    'UP':    (0x48, True),
    'DOWN':  (0x50, True),
}

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
class KEYBDINPUT(ctypes.Structure):
    _fields_ = ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint), ("dwExtraInfo", ULONG_PTR)
class INPUT(ctypes.Structure):
    _fields_ = ("type", ctypes.c_uint), ("ki", KEYBDINPUT), ("padding", ctypes.c_ulong)
SendInput = ctypes.windll.user32.SendInput

KEYEVENTF_SCANCODE    = 0x0008
KEYEVENTF_KEYUP       = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

_user32 = ctypes.windll.user32
_kernel = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

def get_fg_title() -> str:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd: return ""
    n = _user32.GetWindowTextLengthW(hwnd) + 1
    buf = ctypes.create_unicode_buffer(n)
    _user32.GetWindowTextW(hwnd, buf, n)
    return buf.value

def get_fg_proc_name() -> str:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd: return ""
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h: return ""
    try:
        sz = ctypes.c_uint(32768)
        buf = ctypes.create_unicode_buffer(sz.value)
        if _kernel.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):
            path = buf.value
            return path.split("\\")[-1].lower()
    finally:
        _kernel.CloseHandle(h)
    return ""

def allowed_to_send() -> bool:
    title = get_fg_title().lower()
    proc  = get_fg_proc_name()
    if any(s in title for s in ALLOWED_TITLE_SUBSTR): return True
    if any(s in proc  for s in ALLOWED_PROC_SUBSTR):  return True
    return False

def press_scancode(key: str):
    sc, ext = SCANCODES[key]
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0)
    ip = INPUT(type=1, ki=KEYBDINPUT(0, sc, flags, 0, ULONG_PTR(0)), padding=0)
    SendInput(1, ctypes.byref(ip), ctypes.sizeof(INPUT))

def release_scancode(key: str):
    sc, ext = SCANCODES[key]
    flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0)
    ip = INPUT(type=1, ki=KEYBDINPUT(0, sc, flags, 0, ULONG_PTR(0)), padding=0)
    SendInput(1, ctypes.byref(ip), ctypes.sizeof(INPUT))

def tap_scancode(keys_down: List[str], hold_ms: int):
    if not allowed_to_send(): return
    for k in keys_down: press_scancode(k)
    time.sleep(max(0, hold_ms)/1000.0)
    for k in reversed(keys_down): release_scancode(k)

def pulse_scancode(key: str, on_ms: int, gap_ms: int):
    """Сделать «миг»: L (on_ms) -> пауза gap_ms -> L (on_ms)."""
    if not allowed_to_send(): return
    tap_scancode([key], on_ms)
    time.sleep(max(0, gap_ms)/1000.0)
    tap_scancode([key], on_ms)

def hold_start(keys: List[str]):
    if not allowed_to_send(): return
    for k in keys: press_scancode(k)

def hold_stop(keys: List[str]):
    if not keys: return
    if not allowed_to_send(): return
    for k in reversed(keys): release_scancode(k)

# ==========================
# CORE
# ==========================

def L(n: int) -> int:
    return n - 1 if ONE_BASED_LABELS else n

class ButtonState:
    def __init__(self, idx: int):
        self.idx = idx
        self.is_down = False
        self.down_ms = 0
        self.suppressed_until_up = False
        self.hold_keys: Optional[List[str]] = None
        self.hold_after_long_pending: Optional[List[str]] = None

        # повторы
        self.repeat_mode: Optional[str] = None  # 'keys' | 'pulse'
        self.repeat_keys: Optional[List[str]] = None
        self.repeat_every_ms: Optional[int] = None
        self.repeat_after_long: bool = False
        self.next_repeat_ms: Optional[int] = None
        # для pulse
        self.pulse_key: Optional[str] = None
        self.pulse_on_ms: int = 0
        self.pulse_gap_ms: int = 0
        # для keys-режима
        self.repeat_hold_ms: int = 40

    def start_hold(self, keys: List[str]):
        self.hold_keys = keys
        hold_start(keys)

    def stop_hold(self):
        if self.hold_keys:
            hold_stop(self.hold_keys)
        self.hold_keys = None

    def start_repeat_keys(self, keys: List[str], every_ms: int, after_long: bool, hold_ms: int, now: int):
        self.repeat_mode = 'keys'
        self.repeat_keys = keys
        self.repeat_every_ms = every_ms
        self.repeat_after_long = after_long
        self.repeat_hold_ms = hold_ms
        self.next_repeat_ms = now

    def start_repeat_pulse(self, key: str, on_ms: int, gap_ms: int, period_ms: int, after_long: bool, now: int):
        self.repeat_mode = 'pulse'
        self.pulse_key = key
        self.pulse_on_ms = on_ms
        self.pulse_gap_ms = gap_ms
        self.repeat_every_ms = period_ms
        self.repeat_after_long = after_long
        self.next_repeat_ms = now

    def stop_repeat(self):
        self.repeat_mode = None
        self.repeat_keys = None
        self.pulse_key = None
        self.repeat_every_ms = None
        self.next_repeat_ms = None

def choose_joystick() -> Optional[pygame.joystick.Joystick]:
    pygame.joystick.quit()
    pygame.joystick.init()
    cnt = pygame.joystick.get_count()
    if cnt == 0:
        print(">>> Руль/джойстик не найден.")
        return None
    chosen = None
    for i in range(cnt):
        js = pygame.joystick.Joystick(i); js.init()
        name = js.get_name()
        print(f"[{i}] '{name}'  buttons={js.get_numbuttons()}  axes={js.get_numaxes()}  hats={js.get_numhats()}")
        if chosen is None and any(s.lower() in name.lower() for s in ["moza","racing","wheel","es","r5"]):
            chosen = js
    return chosen or pygame.joystick.Joystick(0)

def build_config():
    actions = {L(k): v for k, v in SINGLE_ACTIONS_1B.items()}
    mod_btn  = L(MODIFIER_BTN_LABEL)
    arrows   = {L(k): v for k, v in ARROW_COMBO_LABELS.items()}
    return actions, mod_btn, arrows

def run(learn_mode: bool):
    pygame.init()
    js = choose_joystick()
    if not js: return
    print(f"Используется устройство: {js.get_name()}")
    print(f"Кнопок: {js.get_numbuttons()}, осей: {js.get_numaxes()}, хатов: {js.get_numhats()}")
    if learn_mode:
        print("LEARN MODE: жми кнопки. Ctrl+C — выход.")

    actions, MOD_BTN, ARROWS = build_config()
    states: Dict[int, ButtonState] = {i: ButtonState(i) for i in range(js.get_numbuttons())}
    pressed: Set[int] = set()
    clock = pygame.time.Clock()

    while True:
        now = int(time.time()*1000)

        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                b = event.button
                pressed.add(b)
                st = states[b]; st.is_down = True; st.down_ms = now
                st.suppressed_until_up = False
                st.stop_repeat()
                st.hold_after_long_pending = None

                if learn_mode:
                    print(f"BTN {b} DOWN"); continue

                # Модификатор 1 + (8/7/6/5) -> стрелки (каждый раз)
                if MOD_BTN in pressed and b in ARROWS:
                    if allowed_to_send():
                        tap_scancode([ARROWS[b]], 35)
                    st.suppressed_until_up = True
                    continue

                cfg = actions.get(b, {})

                # Мгновенное удержание
                if 'hold_scancode' in cfg:
                    st.start_hold(list(cfg['hold_scancode']))

                # Удержание только после long
                if 'hold_after_long_scancode' in cfg:
                    st.hold_after_long_pending = list(cfg['hold_after_long_scancode'])

                # Повтор «пульсом» (для света)
                if 'hold_repeat_pulse_scancode' in cfg:
                    key = list(cfg['hold_repeat_pulse_scancode'])[0]
                    after_long = bool(cfg.get('repeat_after_long', False))
                    st.start_repeat_pulse(
                        key=key,
                        on_ms=PULSE_ON_MS,
                        gap_ms=PULSE_GAP_MS,
                        period_ms=PULSE_PERIOD_MS,
                        after_long=after_long,
                        now=now
                    )

                # Повтор «keys» (на будущее, не используется сейчас)
                if 'hold_repeat_scancode' in cfg:
                    keys, period = cfg['hold_repeat_scancode']
                    st.start_repeat_keys(list(keys), int(period), bool(cfg.get('repeat_after_long', False)), 40, now)

            elif event.type == pygame.JOYBUTTONUP:
                b = event.button
                pressed.discard(b)
                st = states[b]; st.is_down = False

                if learn_mode:
                    print(f"BTN {b} UP"); continue

                # Остановить удержание/повторы
                st.stop_hold()
                st.stop_repeat()

                # Если комбо не подавляло — решаем short/long
                if not st.suppressed_until_up:
                    dur = now - st.down_ms
                    cfg = actions.get(b, {})
                    if dur >= LONG_PRESS_MS:
                        # если был hold_after_long, он стартовал в тиках и подавил short/long
                        if 'long_scancode' in cfg:
                            if allowed_to_send():
                                tap_scancode(list(cfg['long_scancode']), 35)
                    else:
                        # короткое: обычный scancode…
                        if 'short_scancode' in cfg:
                            if allowed_to_send():
                                tap_scancode(list(cfg['short_scancode']), 35)
                        # …или «пульс» (двойной тап)
                        elif 'short_pulse_scancode' in cfg:
                            key = list(cfg['short_pulse_scancode'])[0]
                            pulse_scancode(key, PULSE_ON_MS, PULSE_GAP_MS)

        # Тики: запуск удержания после long
        for st in states.values():
            if st.is_down and st.hold_after_long_pending and st.hold_keys is None:
                if now - st.down_ms >= LONG_PRESS_MS:
                    st.start_hold(st.hold_after_long_pending)
                    st.suppressed_until_up = True
                    st.hold_after_long_pending = None

        # Тики: повторы
        for st in states.values():
            if st.is_down and st.repeat_every_ms:
                if st.repeat_after_long and (now - st.down_ms) < LONG_PRESS_MS:
                    continue
                if st.next_repeat_ms is None or now >= st.next_repeat_ms:
                    if st.repeat_mode == 'keys' and st.repeat_keys:
                        tap_scancode(st.repeat_keys, st.repeat_hold_ms)
                    elif st.repeat_mode == 'pulse' and st.pulse_key:
                        pulse_scancode(st.pulse_key, st.pulse_on_ms, st.pulse_gap_ms)
                    st.next_repeat_ms = now + st.repeat_every_ms

        clock.tick(250)  # ~4 мс цикл

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--learn", action="store_true")
    args = ap.parse_args()
    run(learn_mode=args.learn)
