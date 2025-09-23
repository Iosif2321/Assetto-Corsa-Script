# moza_ac_music_combo_v6.py
# Музыка (SMTC+VK) + Now Playing + маппер для AC (сканкоды).
# Новое: btn18 — «мигатель» фар с учётом фаз (всегда OFF на отпускании долгого).
# Фикс: нет дублирующихся функций -> Pylance чисто.

import os, sys, time, asyncio, threading, ctypes
from typing import Any, Optional, Dict, Set, List, Tuple

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "1")
os.environ.setdefault("SDL_XINPUT_ENABLED", "0")

# ===== Музыка (индексы = номер кнопки - 1) =====
BTN_PLAY_PAUSE = 0   # btn0 -> Pause/Play
BTN_NEXT       = 1   # btn1 -> Next
BTN_PREV       = 2   # btn2 -> Prev
DEBOUNCE_MS    = 120

# ===== Источник SMTC =====
FORCE_SOURCE = "A025C540.Yandex.Music"   # для браузера: "chrome" или "yandex"
PREFERRED    = ["yandex", "chrome", "msedge", "opera", "firefox", "spotify"]

# ===== Now Playing =====
OUT_FILE = os.path.expandvars(r"%USERPROFILE%\Documents\Assetto Corsa\aimp_now_playing.txt")
POLL_NOWPLAYING_S = 0.4

# ===== Признак "мы в игре" =====
ALLOWED_TITLE_SUBSTR = ["assetto corsa", "content manager"]
ALLOWED_PROC_SUBSTR  = ["acs.exe", "assettocorsa"]

# ===== Маппер AC =====
ONE_BASED_LABELS   = True
LONG_PRESS_MS      = 250
VERBOSE            = True

# Свет (L) — параметры мигания
PULSE_ON_MS      = 30     # сколько держим фазу ON (между двумя L)
PULSE_GAP_MS     = 35     # пауза внутри двойного тапа (не критично)
PULSE_PERIOD_MS  = 90     # период миганий при удержании
LIGHT_BTN_LABEL  = 18     # НОВОЕ: удержание btn18 — мигать + всегда OFF на отпускании

# Бинды
SINGLE_ACTIONS_1B: Dict[int, Dict[str, Any]] = {
    22: { 'short_scancode': ['F1'], 'hold_after_long_scancode': ['W'] },  # F1 / держать W
    8:  { 'hold_scancode': ['Q'] },                                       # держать Q
    6:  { 'hold_scancode': ['E'] },                                       # держать E
    19: { 'short_pulse_scancode': ['L'],  # оставили «миг» на 19 как раньше
          'hold_repeat_pulse_scancode': ['L'],
          'repeat_after_long': True },
}
# Модификатор: держим 1 и жмём 8/7/6/5 -> ←/↓/→/↑
MODIFIER_BTN_LABEL = 1
ARROW_COMBO_LABELS = { 8: 'LEFT', 7: 'DOWN', 6: 'RIGHT', 5: 'UP' }

# НОВОЕ: модификатор 3 для ABS/TC
MOD2_BTN_IDX = 3  # держим btn3 и жмём:
MOD2_MAP = {      # btn -> клавиша (топовый ряд цифр)
    7: '7',  # ABS −
    5: '8',  # ABS +
    4: '0',  # TC +
    6: '9',  # TC −
}

def log(*a): print("[INFO]", *a)

# ===== SendInput (сканкоды) для игры =====
SCANCODES = {
    'F1':0x3B, 'W':0x11, 'Q':0x10, 'E':0x12, 'L':0x26,
    'LEFT':0x4B, 'RIGHT':0x4D, 'UP':0x48, 'DOWN':0x50,
    # цифры top-row:
    '1':0x02, '2':0x03, '3':0x04, '4':0x05, '5':0x06,
    '6':0x07, '7':0x08, '8':0x09, '9':0x0A, '0':0x0B,
}
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
class KEYBDINPUT(ctypes.Structure):
    _fields_ = ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_uint), ("time", ctypes.c_uint), ("dwExtraInfo", ULONG_PTR)
class INPUT(ctypes.Structure):
    _fields_ = ("type", ctypes.c_uint), ("ki", KEYBDINPUT), ("padding", ctypes.c_ulong)
SendInput = ctypes.windll.user32.SendInput
KEYEVENTF_SCANCODE, KEYEVENTF_KEYUP, KEYEVENTF_EXTENDEDKEY = 0x0008, 0x0002, 0x0001
_user32, _kernel = ctypes.windll.user32, ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

def get_fg_title() -> str:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd: return ""
    n = _user32.GetWindowTextLengthW(hwnd) + 1
    buf = ctypes.create_unicode_buffer(n)
    _user34 = _user32.GetWindowTextW(hwnd, buf, n)
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
            return buf.value.split("\\")[-1].lower()
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
    sc = SCANCODES[key]
    ext = key in ('LEFT','RIGHT','UP','DOWN')
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if ext else 0)
    ip = INPUT(type=1, ki=KEYBDINPUT(0, sc, flags, 0, ULONG_PTR(0)), padding=0)
    SendInput(1, ctypes.byref(ip), ctypes.sizeof(INPUT))

def release_scancode(key: str):
    sc = SCANCODES[key]
    ext = key in ('LEFT','RIGHT','UP','DOWN')
    flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if ext else 0)
    ip = INPUT(type=1, ki=KEYBDINPUT(0, sc, flags, 0, ULONG_PTR(0)), padding=0)
    SendInput(1, ctypes.byref(ip), ctypes.sizeof(INPUT))

def tap_scancode(keys_down: List[str], hold_ms: int = 35):
    if not allowed_to_send(): return
    for k in keys_down: press_scancode(k)
    time.sleep(max(0, hold_ms)/1000.0)
    for k in reversed(keys_down): release_scancode(k)

def pulse_scancode(key: str, on_ms: int, gap_ms: int):
    # Двойной тап: L -> (on_ms) -> L
    if not allowed_to_send(): return
    tap_scancode([key], on_ms)
    time.sleep(max(0, gap_ms)/1000.0)
    tap_scancode([key], on_ms)

# ===== AC state helpers =====
def L_idx(n: int) -> int: return n - 1 if ONE_BASED_LABELS else n

class ButtonState:
    def __init__(self, idx: int):
        self.idx = idx
        self.is_down = False
        self.down_ms = 0
        self.suppressed_until_up = False
        self.hold_keys: Optional[List[str]] = None
        self.hold_after_long_pending: Optional[List[str]] = None
        # повторы
        self.repeat_mode: Optional[str] = None  # 'pulse' | 'flash18'
        self.repeat_every_ms: Optional[int] = None
        self.repeat_after_long: bool = False
        self.next_repeat_ms: Optional[int] = None
        # pulse mode
        self.pulse_key: Optional[str] = None
        # flash18 bookkeeping
        self.flash_in_on_phase: bool = False
        self.flash_toggle_count: int = 0

    def start_hold(self, keys: List[str]):
        self.hold_keys = keys
        if allowed_to_send():
            for k in keys: press_scancode(k)

    def stop_hold(self):
        if self.hold_keys and allowed_to_send():
            for k in reversed(self.hold_keys): release_scancode(k)
        self.hold_keys = None

    def start_pulse(self, key: str, period_ms: int, after_long: bool, now_ms: int):
        self.repeat_mode = 'pulse'
        self.pulse_key = key
        self.repeat_every_ms = period_ms
        self.repeat_after_long = after_long
        self.next_repeat_ms = now_ms

    def start_flash18(self, period_ms: int, now_ms: int):
        # специальный режим мигания для btn18: в каждом тике делаем L (ON) -> sleep -> L (OFF).
        self.repeat_mode = 'flash18'
        self.repeat_every_ms = period_ms
        self.repeat_after_long = False
        self.next_repeat_ms = now_ms
        self.flash_in_on_phase = False
        self.flash_toggle_count = 0

    def stop_repeat(self):
        self.repeat_mode = None
        self.repeat_every_ms = None
        self.next_repeat_ms = None
        self.pulse_key = None
        self.flash_in_on_phase = False

# ====== SMTC (музыка) ======
MediaManager: Any = None
PS: Any = None
USE_SMTC = True
try:
    from winsdk.windows.media.control import (  # type: ignore
        GlobalSystemMediaTransportControlsSessionManager as _MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PS,
    )
    MediaManager = _MediaManager; PS = _PS
except Exception:
    USE_SMTC = False
    log("winsdk не найден — SMTC отключён, останутся VK-медиаклавиши.")

def _start_loop():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop
LOOP = _start_loop()
SMTC_READY = False
SMTC_MGR: Any = None
CURRENT_SRC: Optional[str] = None

async def smtc_init_async():
    global SMTC_READY, SMTC_MGR
    try:
        SMTC_MGR = await MediaManager.request_async()
        SMTC_READY = True
        log("SMTC готов")
    except Exception as e:
        log("SMTC недоступен:", e)

def _status_score(st: Any) -> int:
    try:
        if st == PS.PLAYING: return 3
        if st == PS.PAUSED:  return 2
        if st == PS.STOPPED: return 1
    except Exception: pass
    return 0

async def _pick_session(mgr: Any):
    try: sessions: List[Any] = mgr.get_sessions()
    except Exception: return None
    if not sessions: return None
    def score(s: Any) -> Tuple[int,int]:
        try: a = (s.source_app_user_model_id or "").lower()
        except Exception: a = ""
        pref = 2 if FORCE_SOURCE and FORCE_SOURCE.lower() in a else (1 if any(p in a for p in PREFERRED) else 0)
        try: st = s.get_playback_info().playback_status
        except Exception: st = None
        return (pref, _status_score(st))
    best, best_sc = None, (-9, -9)
    for s in sessions:
        sc = score(s)
        if sc > best_sc: best_sc, best = sc, s
    return best

async def _smtc_now(mgr: Any) -> Tuple[str, Optional[str]]:
    s = await _pick_session(mgr)
    if not s: return "", None
    try:
        p = await s.try_get_media_properties_async()
        artist = (p.artist or "").strip(); title = (p.title or "").strip()
        txt = f"{artist} — {title}".strip(" —")
    except Exception: txt = ""
    try: aumid = s.source_app_user_model_id
    except Exception: aumid = None
    return txt, aumid

def smtc_send(cmd: str):
    if not (USE_SMTC and SMTC_READY): return
    async def _do():
        try:
            s = await _pick_session(SMTC_MGR)
            if not s: return
            if cmd == "play_pause":
                try: st = s.get_playback_info().playback_status
                except Exception: st = None
                if st == PS.PLAYING: await s.try_pause_async()
                else:                await s.try_play_async()
            elif cmd == "next": await s.try_skip_next_async()
            elif cmd == "prev": await s.try_skip_previous_async()
        except Exception: pass
    asyncio.run_coroutine_threadsafe(_do(), LOOP)

# ===== VK медиа-клавиши (резерв) =====
VK_MEDIA_NEXT_TRACK, VK_MEDIA_PREV_TRACK, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB3
SendInputVK = ctypes.windll.user32.SendInput
class KEYBDINPUT_VK(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]
class INPUT_VK(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT_VK)]
VK_KEYDOWN, VK_KEYUP = 0x0001, 0x0002
def vk_tap(vk: int):
    down = INPUT_VK(1, KEYBDINPUT_VK(vk, 0, VK_KEYDOWN, 0, None))
    up   = INPUT_VK(1, KEYBDINPUT_VK(vk, 0, VK_KEYDOWN | VK_KEYUP, 0, None))
    SendInputVK(1, ctypes.byref(down), ctypes.sizeof(INPUT_VK))
    SendInputVK(1, ctypes.byref(up),   ctypes.sizeof(INPUT_VK))

def using_yandex_uwp() -> bool:
    return isinstance(CURRENT_SRC, str) and "a025c540.yandex.music" in CURRENT_SRC.lower()

def cmd_play_pause():
    log("CMD: Play/Pause"); smtc_send("play_pause")
    if not using_yandex_uwp(): vk_tap(VK_MEDIA_PLAY_PAUSE)

def cmd_next():
    log("CMD: Next"); smtc_send("next")
    if not using_yandex_uwp(): vk_tap(VK_MEDIA_NEXT_TRACK)

def cmd_prev():
    log("CMD: Prev"); smtc_send("prev")
    if not using_yandex_uwp(): vk_tap(VK_MEDIA_PREV_TRACK)

# ===== Now Playing (фон) =====
def ensure_outdir(): os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

async def nowplaying_task():
    global CURRENT_SRC
    ensure_outdir()
    last_txt, last_src = None, None
    while True:
        try:
            if USE_SMTC and SMTC_READY: txt, src = await _smtc_now(SMTC_MGR)
            else: txt, src = "", None
            if src != last_src: log("Источник:", repr(src)); last_src = src
            CURRENT_SRC = src
            if txt != last_txt:
                with open(OUT_FILE, "w", encoding="utf-8") as f: f.write(txt)
                log("Файл обновлён:", repr(txt)); last_txt = txt
        except Exception as e:
            log("Ошибка now_playing:", e)
        await asyncio.sleep(POLL_NOWPLAYING_S)

# ===== Джойстик / Руль =====
import pygame
def choose_joystick() -> Optional[Any]:
    pygame.joystick.quit(); pygame.joystick.init()
    cnt = pygame.joystick.get_count()
    if cnt == 0:
        print(">>> Руль/джойстик не найден."); return None
    chosen = None
    for i in range(cnt):
        js = pygame.joystick.Joystick(i); js.init()
        name = js.get_name()
        print(f"[{i}] '{name}'  buttons={js.get_numbuttons()}  axes={js.get_numaxes()}  hats={js.get_numhats()}")
        if chosen is None and any(s.lower() in name.lower() for s in ["moza","racing","wheel","es","r5"]):
            chosen = js
    return chosen or pygame.joystick.Joystick(0)

def build_config():
    actions = {L_idx(k): v for k, v in SINGLE_ACTIONS_1B.items()}
    mod_btn  = L_idx(MODIFIER_BTN_LABEL)
    arrows   = {L_idx(k): v for k, v in ARROW_COMBO_LABELS.items()}
    light18  = L_idx(LIGHT_BTN_LABEL)
    return actions, mod_btn, arrows, light18

last_down_ms: Dict[Tuple[int,int], int] = {}

def main_loop():
    pygame.init()
    js = choose_joystick()
    if not js: return
    print(f"Используется устройство: {js.get_name()}")
    print(f"Кнопок: {js.get_numbuttons()}, осей: {js.get_numaxes()}, хатов: {js.get_numhats()}")

    actions, MOD_BTN, ARROWS, LIGHT18_IDX = build_config()
    states: Dict[int, ButtonState] = {i: ButtonState(i) for i in range(js.get_numbuttons())}
    pressed: Set[int] = set()
    clock = pygame.time.Clock()

    print("\nМузыка: 1=Пауза, 2=Следующий, 3=Предыдущий."
          "\nИгра: 22(F1/W), 8(Q), 6(E), 19(L-миг), 1+8/7/6/5 -> стрелки,"
          f"\n      {LIGHT_BTN_LABEL}+hold -> мигатель L (всегда OFF при отпускании), 3+7/5/4/6 -> 7/8/0/9.\n")

    while True:
        now = int(time.time()*1000)
        in_game = allowed_to_send()

        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                b = event.button
                key = (event.joy, b)
                if now - last_down_ms.get(key, 0) < DEBOUNCE_MS: continue
                last_down_ms[key] = now
                if VERBOSE: print(("GAME" if in_game else "OS"), "DOWN", b)

                st = states[b]
                st.is_down = True; st.down_ms = now
                st.suppressed_until_up = False
                st.stop_repeat(); st.hold_after_long_pending = None

                if in_game:
                    pressed.add(b)

                    # НОВОЕ: мод 3 для ABS/TC
                    if (MOD2_BTN_IDX in pressed) and (b in MOD2_MAP):
                        tap_scancode([MOD2_MAP[b]], 35)
                        st.suppressed_until_up = True
                        if MOD2_BTN_IDX in states:
                            states[MOD2_BTN_IDX].suppressed_until_up = True
                        continue

                    # Старое комбо: btn0 + 8/7/6/5 -> стрелки (и подавляем btn0, чтобы не было паузы)
                    if (MOD_BTN in pressed) and (b in ARROWS):
                        tap_scancode([ARROWS[b]], 35)
                        st.suppressed_until_up = True
                        if MOD_BTN in states:
                            states[MOD_BTN].suppressed_until_up = True
                        continue

                    # Спец: удержание btn18 -> старт «flash18» (мигает L, с отслеживанием фазы)
                    if b == LIGHT18_IDX:
                        st.start_flash18(PULSE_PERIOD_MS, now)
                        continue

                    # Обычные игровые действия
                    cfg = actions.get(b, {})
                    if 'hold_scancode' in cfg: st.start_hold(list(cfg['hold_scancode']))
                    if 'hold_after_long_scancode' in cfg: st.hold_after_long_pending = list(cfg['hold_after_long_scancode'])
                    if 'hold_repeat_pulse_scancode' in cfg:
                        k = list(cfg['hold_repeat_pulse_scancode'])[0]
                        after_long = bool(cfg.get('repeat_after_long', False))
                        st.start_pulse(k, PULSE_PERIOD_MS, after_long, now)

                # Музыка: в игре — Next/Prev сразу; Pause — на UP (если не было комбо)
                if not in_game:
                    if   b == BTN_PLAY_PAUSE: cmd_play_pause()
                    elif b == BTN_NEXT:       cmd_next()
                    elif b == BTN_PREV:       cmd_prev()
                else:
                    if   b == BTN_NEXT:       cmd_next()
                    elif b == BTN_PREV:       cmd_prev()

            elif event.type == pygame.JOYBUTTONUP:
                b = event.button
                st = states[b]; st.is_down = False

                if in_game:
                    if b in pressed: pressed.discard(b)

                    # если это btn18 и мы были в фазе ON — дослать OFF
                    if b == LIGHT18_IDX and st.repeat_mode == 'flash18':
                        if st.flash_in_on_phase:
                            tap_scancode(['L'], 20)   # финальный OFF
                            st.flash_toggle_count += 1
                        log(f"LIGHT18 toggles={st.flash_toggle_count}")
                        st.stop_repeat()

                    # останов удержаний/повторов
                    st.stop_hold()
                    # короткий/долгий для 22/19
                    cfg = actions.get(b, {})
                    dur = now - st.down_ms
                    if not st.suppressed_until_up:
                        if dur >= LONG_PRESS_MS:
                            if 'long_scancode' in cfg: tap_scancode(list(cfg['long_scancode']), 35)
                        else:
                            if 'short_scancode' in cfg: tap_scancode(list(cfg['short_scancode']), 35)
                            elif 'short_pulse_scancode' in cfg:
                                k = list(cfg['short_pulse_scancode'])[0]
                                pulse_scancode(k, PULSE_ON_MS, PULSE_GAP_MS)

                    # Пауза на btn0 — только если НЕ было комбо
                    if b == BTN_PLAY_PAUSE and not st.suppressed_until_up:
                        cmd_play_pause()

        # Тики игровых удержаний/повторов
        if in_game:
            for idx, st in states.items():
                # запуск hold_after_long
                if st.is_down and st.hold_after_long_pending and st.hold_keys is None:
                    if now - st.down_ms >= LONG_PRESS_MS:
                        st.start_hold(st.hold_after_long_pending)
                        st.suppressed_until_up = True
                        st.hold_after_long_pending = None

                # пульс на 19 (двойные L)
                if st.is_down and st.repeat_mode == 'pulse' and st.repeat_every_ms:
                    if st.repeat_after_long and (now - st.down_ms) < LONG_PRESS_MS:
                        pass
                    else:
                        if st.next_repeat_ms is None or now >= st.next_repeat_ms:
                            if st.pulse_key:
                                pulse_scancode(st.pulse_key, PULSE_ON_MS, PULSE_GAP_MS)
                            st.next_repeat_ms = now + st.repeat_every_ms

                # спец-мигатель на 18: ON->OFF в каждом тике, следим за фазой
                if st.is_down and st.repeat_mode == 'flash18' and st.repeat_every_ms:
                    if st.next_repeat_ms is None or now >= st.next_repeat_ms:
                        # ON
                        tap_scancode(['L'], 10)   # toggle -> ON (если было OFF)
                        st.flash_in_on_phase = True
                        time.sleep(max(0, PULSE_ON_MS)/1000.0)
                        # OFF
                        tap_scancode(['L'], 10)   # toggle -> OFF
                        st.flash_in_on_phase = False
                        st.flash_toggle_count += 2
                        st.next_repeat_ms = now + st.repeat_every_ms

        clock.tick(250)

# ===== Старт =====
# Запускаем фоновые задачи SMTC и основной цикл
def main():
    if USE_SMTC:
        asyncio.run_coroutine_threadsafe(smtc_init_async(), LOOP)
        asyncio.run_coroutine_threadsafe(nowplaying_task(), LOOP)
    else:
        os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
        with open(OUT_FILE, "w", encoding="utf-8") as f: f.write("")
        log("SMTC выключен — только VK-медиа и пустой Now Playing.")
    try:
        main_loop()
    except KeyboardInterrupt:
        print("Выход.")

if __name__ == "__main__":
    main()
