import os, sys, time, asyncio, threading, ctypes
from typing import Any, Optional, Tuple, List

# --- чтобы pygame стабильно видел HID-рули на Windows ---
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
os.environ.setdefault("SDL_JOYSTICK_RAWINPUT", "1")
os.environ.setdefault("SDL_XINPUT_ENABLED", "0")

# ================== КОНФИГ ==================
# Индексы (index = номер кнопки - 1)
BTN_PLAY_PAUSE = 0   # пауза на btn0 (как и было)
BTN_NEXT       = 1   # вперед на btn1
BTN_PREV       = 2   # предыдущий на btn2
DEBOUNCE_MS    = 120

# Приоритет источника SMTC: UWP Яндекс Музыка
FORCE_SOURCE = "A025C540.Yandex.Music"  # если слушаешь в браузере — поставь "chrome" или "yandex"
PREFERRED    = ["yandex", "chrome", "msedge", "opera", "firefox", "spotify"]

# Файл «Исполнитель — Трек»
OUT_FILE = os.path.expandvars(r"%USERPROFILE%\Documents\Assetto Corsa\aimp_now_playing.txt")
POLL_NOWPLAYING_S = 0.4

def log(*a): print("[INFO]", *a)

# ================== SMTC (WinRT) ==================
MediaManager: Any = None
PS: Any = None
USE_SMTC = True
try:
    from winsdk.windows.media.control import (  # type: ignore
        GlobalSystemMediaTransportControlsSessionManager as _MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _PS,
    )
    MediaManager = _MediaManager
    PS = _PS
except Exception as e:
    USE_SMTC = False
    log("winsdk не найден — SMTC недоступен, останется только VK-медиаклавиши.")

def start_loop():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop
LOOP = start_loop()

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

def status_score(st: Any) -> int:
    try:
        if st == PS.PLAYING: return 3
        if st == PS.PAUSED:  return 2
        if st == PS.STOPPED: return 1
    except Exception:
        pass
    return 0

async def pick_session(mgr: Any):
    try:
        sessions: List[Any] = mgr.get_sessions()
    except Exception:
        return None
    if not sessions:
        return None

    def score(s: Any) -> Tuple[int,int]:
        try:
            aumid = (s.source_app_user_model_id or "").lower()
        except Exception:
            aumid = ""
        if FORCE_SOURCE:
            pref = 2 if FORCE_SOURCE.lower() in aumid else 0
        else:
            pref = 1 if any(p in aumid for p in PREFERRED) else 0
        try:
            st = s.get_playback_info().playback_status
        except Exception:
            st = None
        return (pref, status_score(st))

    best, best_sc = None, (-9, -9)
    for s in sessions:
        sc = score(s)
        if sc > best_sc:
            best_sc, best = sc, s
    return best

async def smtc_now(mgr: Any) -> Tuple[str, Optional[str], Any]:
    s = await pick_session(mgr)
    if not s: return "", None, None
    try:
        props = await s.try_get_media_properties_async()
        artist = (props.artist or "").strip()
        title  = (props.title  or "").strip()
        txt = f"{artist} — {title}".strip(" —")
    except Exception:
        txt = ""
    try:
        aumid = s.source_app_user_model_id
    except Exception:
        aumid = None
    try:
        st = s.get_playback_info().playback_status
    except Exception:
        st = None
    return txt, aumid, st

def smtc_send(cmd: str):
    if not (USE_SMTC and SMTC_READY): return
    async def _do():
        try:
            s = await pick_session(SMTC_MGR)
            if not s: return
            if cmd == "play_pause":
                try: st = s.get_playback_info().playback_status
                except Exception: st = None
                if st == PS.PLAYING:
                    await s.try_pause_async()
                else:
                    await s.try_play_async()
            elif cmd == "next":
                await s.try_skip_next_async()
            elif cmd == "prev":
                await s.try_skip_previous_async()
        except Exception:
            pass
    asyncio.run_coroutine_threadsafe(_do(), LOOP)

# ================== РЕЗЕРВ: VK медиаклавиши ==================
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
SendInput = ctypes.windll.user32.SendInput
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP       = 0x0002
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_void_p)]
class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("ki", KEYBDINPUT)]
def vk_tap(vk: int):
    down = INPUT(1, KEYBDINPUT(vk, 0, KEYEVENTF_EXTENDEDKEY, 0, None))
    up   = INPUT(1, KEYBDINPUT(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0, None))
    SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    SendInput(1, ctypes.byref(up),   ctypes.sizeof(INPUT))

def using_yandex_uwp() -> bool:
    return isinstance(CURRENT_SRC, str) and "a025c540.yandex.music" in CURRENT_SRC.lower()

def cmd_play_pause():
    log("CMD: Play/Pause")
    smtc_send("play_pause")
    # чтобы не «двойнить» в UWP-ЯМузыке, VK не дублируем
    if not using_yandex_uwp():
        vk_tap(VK_MEDIA_PLAY_PAUSE)

def cmd_next():
    log("CMD: Next")
    smtc_send("next")
    if not using_yandex_uwp():
        vk_tap(VK_MEDIA_NEXT_TRACK)

def cmd_prev():
    log("CMD: Prev")
    smtc_send("prev")
    if not using_yandex_uwp():
        vk_tap(VK_MEDIA_PREV_TRACK)

# ================== NOW PLAYING (фон) ==================
def ensure_outdir():
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)

async def nowplaying_task():
    global CURRENT_SRC
    ensure_outdir()
    last_txt, last_src = None, None
    while True:
        try:
            if USE_SMTC and SMTC_READY:
                txt, src, _st = await smtc_now(SMTC_MGR)
            else:
                txt, src = "", None
            if src != last_src:
                log("Источник:", repr(src))
                last_src = src
            CURRENT_SRC = src
            if txt != last_txt:
                with open(OUT_FILE, "w", encoding="utf-8") as f:
                    f.write(txt)
                log("Файл обновлён:", repr(txt))
                last_txt = txt
        except Exception as e:
            log("Ошибка now_playing:", e)
        await asyncio.sleep(POLL_NOWPLAYING_S)

# ================== РУЛЬ ==================
import pygame
last_down_ms: dict[Tuple[int,int], int] = {}

def joystick_loop():
    pygame.init(); pygame.joystick.init()
    n = pygame.joystick.get_count()
    if n == 0:
        print("Руль/геймпад не найден. Подключи и перезапусти.")
        sys.exit(1)
    for i in range(n):
        j = pygame.joystick.Joystick(i); j.init()
        print(f"Контроллер #{i}: {j.get_name()} (buttons={j.get_numbuttons()})")
    print("\nНазначения: 1=Пауза/Плей (idx 0), 2=Следующий (idx 1/2), 4=Предыдущий (idx 3).  Ctrl+C — выход.\n")

    clock = pygame.time.Clock()
    try:
        while True:
            for e in pygame.event.get():
                if e.type == pygame.JOYBUTTONDOWN:
                    key = (e.joy, e.button)
                    now = int(time.time()*1000)
                    if now - last_down_ms.get(key, 0) < DEBOUNCE_MS:
                        continue
                    last_down_ms[key] = now

                    print(f"DOWN js{e.joy} btn{e.button}")

                    b = e.button
                    if   b == BTN_PLAY_PAUSE: cmd_play_pause()
                    elif b == BTN_NEXT:       cmd_next()
                    elif b == BTN_PREV:       cmd_prev()
                # (JOYBUTTONUP не нужен для этой логики)
            clock.tick(250)
    except KeyboardInterrupt:
        pass
    finally:
        pygame.quit()

# ================== СТАРТ ==================
if __name__ == "__main__":
    if USE_SMTC:
        asyncio.run_coroutine_threadsafe(smtc_init_async(), LOOP)
        asyncio.run_coroutine_threadsafe(nowplaying_task(), LOOP)
    else:
        # даже без SMTC пишем пустую строку один раз, чтобы файл существовал
        os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
        with open(OUT_FILE, "w", encoding="utf-8") as f: f.write("")
        log("SMTC выключен — будет работать только VK-медиа и пустой файл Now Playing.")
    joystick_loop()
    print("Выход.")
