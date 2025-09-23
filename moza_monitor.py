# moza_overlay_resizable_hotkeys_log.py
# Python 3.6+ ; окно поверх всех, глобальные хоткеи, запись CSV (F10/F11/F12)

import sys, os, json, time, threading, queue, traceback
import pygame

os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
pygame.init(); pygame.joystick.init()

CFG = "moza_monitor_config.json"
ERR = "moza_monitor_error.log"

# ---- окно / размеры ----
WIN_W, WIN_H = 820, 420
MINI_MIN_W, MINI_MIN_H = 280, 110
ASSIGN_DELTA = 0.12
SMOOTH_ALPHA = 0.25
JITTER_EPS   = 0.6

# ---- состояние ----
steer_axis=thr_axis=brk_axis=clt_axis=None
inv_steer=inv_thr=inv_brk=inv_clt=False
assign_mode=None
disp={"steer":0.0,"thr":0.0,"brk":0.0,"clt":0.0}
mini=True
topmost=True
show_clutch=True
wheel_range_deg=900
hk_error=False  # не удалось зарегистрировать часть hotkeys

# автокалибровка сырых осей -1..1
cal={
 "steer":{"min":+1.0,"max":-1.0},
 "thr":{"min":+1.0,"max":-1.0},
 "brk":{"min":+1.0,"max":-1.0},
 "clt":{"min":+1.0,"max":-1.0},
}

# ---- запись ----
LOG_DIR = "logs"
recording = False
log_fh = None
log_started = 0.0
log_last_flush = 0.0
log_mark_id = 0
log_mark_pending = ""   # ставится на ближайшую запись

def ensure_log_dir():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except Exception:
        pass

def now_iso():
    lt = time.localtime()
    return time.strftime("%Y-%m-%d %H:%M:%S", lt) + ".%03d" % int((time.time()%1)*1000)

def new_log_path():
    ensure_log_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return os.path.join(LOG_DIR, "moza_%s.csv" % stamp)

def start_recording(device_name):
    global recording, log_fh, log_started, log_last_flush, log_mark_id
    if recording: return
    path = new_log_path()
    log_fh = open(path, "w", encoding="utf-8", newline="")
    # мета-инфо
    log_fh.write("# device=%s\n" % device_name.replace("\n"," "))
    log_fh.write("# range_deg=%d\n" % wheel_range_deg)
    log_fh.write("# axes: steer=%s thr=%s brk=%s clt=%s\n" %
                 (steer_axis, thr_axis, brk_axis, clt_axis))
    # заголовок
    log_fh.write("iso_ts,t_ms,steer_deg,steer_pct,thr_pct,brk_pct,clt_pct,buttons_mask,buttons,mark\n")
    log_started = time.time()
    log_last_flush = log_started
    log_mark_id = 0
    recording = True

def stop_recording():
    global recording, log_fh
    if not recording: return
    try:
        log_fh.flush(); log_fh.close()
    except Exception:
        pass
    log_fh = None
    recording = False

def split_recording(device_name):
    # закрыть текущий и открыть новый, если сейчас идёт запись
    if recording:
        stop_recording()
        start_recording(device_name)

def write_row(iso_ts, t_ms, steer_deg, steer_pct, thr, brk, clt, btn_mask, btn_list, mark):
    if not recording or log_fh is None: return
    # список кнопок как "1|2|5" (без пробелов)
    btn_str = "|".join(str(x) for x in btn_list) if btn_list else ""
    line = "%s,%d,%d,%.3f,%.3f,%.3f,%.3f,%d,%s,%s\n" % (
        iso_ts, t_ms, int(steer_deg), float(steer_pct),
        float(thr) if thr is not None else -1.0,
        float(brk) if brk is not None else -1.0,
        float(clt) if clt is not None else -1.0,
        int(btn_mask), btn_str, mark
    )
    log_fh.write(line)

def try_flush():
    global log_last_flush
    if not recording or log_fh is None: return
    t = time.time()
    if t - log_last_flush >= 0.5:  # сбрасываем дважды в секунду
        try: log_fh.flush()
        except Exception: pass
        log_last_flush = t

# ---- конфиг ----
def load_cfg():
    global steer_axis,thr_axis,brk_axis,clt_axis
    global inv_steer,inv_thr,inv_brk,inv_clt
    global mini,topmost,show_clutch,wheel_range_deg,cal
    try:
        with open(CFG,"r",encoding="utf-8") as f: c=json.load(f)
        steer_axis=c.get("steer_axis"); thr_axis=c.get("thr_axis")
        brk_axis=c.get("brk_axis");     clt_axis=c.get("clt_axis")
        inv_steer=c.get("inv_steer",False); inv_thr=c.get("inv_thr",False)
        inv_brk=c.get("inv_brk",False);     inv_clt=c.get("inv_clt",False)
        mini=c.get("mini",True); topmost=c.get("topmost",True)
        show_clutch=c.get("show_clutch",True)
        wheel_range_deg=int(c.get("wheel_range_deg",900))
        ccal=c.get("cal",{})
        for k in cal:
            if k in ccal and "min" in ccal[k] and "max" in ccal[k]:
                cal[k]["min"]=float(ccal[k]["min"]); cal[k]["max"]=float(ccal[k]["max"])
    except Exception:
        pass

def save_cfg():
    c={"steer_axis":steer_axis,"thr_axis":thr_axis,"brk_axis":brk_axis,"clt_axis":clt_axis,
       "inv_steer":inv_steer,"inv_thr":inv_thr,"inv_brk":inv_brk,"inv_clt":inv_clt,
       "mini":mini,"topmost":topmost,"show_clutch":show_clutch,
       "wheel_range_deg":wheel_range_deg,"cal":cal}
    try:
        with open(CFG,"w",encoding="utf-8") as f: json.dump(c,f,ensure_ascii=False,indent=2)
    except Exception:
        pass

load_cfg()

# ---- WinAPI: topmost + глобальные хоткеи ----
import ctypes, ctypes.wintypes as wt
user32 = ctypes.windll.user32

WM_HOTKEY = 0x0312
MOD_NOREPEAT=0x4000
VK = { "ESC":0x1B, "F1":0x70,"F2":0x71,"F3":0x72,"F4":0x73,"F5":0x74,"F6":0x75,
       "F7":0x76,"F8":0x77,"F9":0x78,"F10":0x79,"F11":0x7A,"F12":0x7B,
       "R":0x52, "D1":0x31,"D2":0x32,"D3":0x33,"D4":0x34 }

def apply_topmost(enable=True):
    try:
        hwnd = pygame.display.get_wm_info().get("window")
        SWP_NOMOVE=0x0002; SWP_NOSIZE=0x0001; SWP_SHOWWINDOW=0x0040
        HWND_TOPMOST=-1; HWND_NOTOPMOST=-2
        user32.SetWindowPos(wt.HWND(hwnd),
            HWND_TOPMOST if enable else HWND_NOTOPMOST, 0,0,0,0, SWP_NOMOVE|SWP_NOSIZE|SWP_SHOWWINDOW)
    except Exception:
        pass

hotq = queue.Queue()
_registered_ids = []
def _reg_hotkey(idnum, vk):
    global hk_error
    if not user32.RegisterHotKey(None, idnum, MOD_NOREPEAT, vk):
        hk_error = True
    else:
        _registered_ids.append(idnum)
def start_hotkey_thread():
    def worker():
        # регистрируем все нужные клавиши
        pairs = [
            (1, VK["ESC"]),
            (2, VK["F1"]), (3, VK["F2"]), (4, VK["F3"]), (5, VK["F4"]),
            (6, VK["F5"]), (7, VK["F6"]), (8, VK["F7"]),
            (9, VK["F8"]), (10, VK["F9"]),
            (11, VK["D1"]), (12, VK["D2"]), (13, VK["D3"]), (14, VK["D4"]),
            (15, VK["R"]),
            (16, VK["F10"]),  # REC start/stop
            (17, VK["F11"]),  # marker
            (18, VK["F12"])   # split
        ]
        for idnum, vk in pairs:
            _reg_hotkey(idnum, vk)
        msg = wt.MSG()
        while True:
            res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if res == 0: break
            if msg.message == WM_HOTKEY:
                hotq.put(int(msg.wParam))
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    threading.Thread(target=worker, daemon=True).start()
def stop_hotkeys():
    for idnum in _registered_ids:
        try: user32.UnregisterHotKey(None, idnum)
        except Exception: pass

# ---- окно / шрифты ----
def ensure_window():
    global screen, font, font2, fontMini
    flags = pygame.RESIZABLE
    size = (max(MINI_MIN_W, 300), max(MINI_MIN_H, 125)) if mini else (WIN_W, WIN_H)
    screen = pygame.display.set_mode(size, flags=flags)
    pygame.display.set_caption("MOZA Monitor (Python)")
    font  = pygame.font.SysFont("Consolas", 22)
    font2 = pygame.font.SysFont("Consolas", 16)
    fontMini = pygame.font.SysFont("Consolas", 13)
    apply_topmost(topmost)
ensure_window()
clock = pygame.time.Clock()

# ---- joystick ----
def pick_joystick():
    pygame.joystick.quit(); pygame.joystick.init()
    cnt=pygame.joystick.get_count()
    if cnt==0: return None,"— (нет устройств)"
    idx=0
    for i in range(cnt):
        nm=pygame.joystick.Joystick(i).get_name().lower()
        if "moza" in nm or "wheel" in nm or "base" in nm: idx=i; break
    js=pygame.joystick.Joystick(idx); js.init(); return js, js.get_name()
def auto_bind_if_unset(js):
    global steer_axis,thr_axis,brk_axis,clt_axis
    if js is None: return
    n=js.get_numaxes()
    if n==0: return
    def pick_live(excl):
        best=None; bestv=0.05
        for i in range(n):
            if i in excl: continue
            v=abs(js.get_axis(i))
            if v>bestv: bestv=v; best=i
        return best
    used=set(a for a in (steer_axis,thr_axis,brk_axis,clt_axis) if a is not None)
    if steer_axis is None:
        c=pick_live(used); 
        if c is not None: steer_axis=c; used.add(c)
    if thr_axis is None:
        c=pick_live(used); 
        if c is not None: thr_axis=c; used.add(c)
    if brk_axis is None:
        c=pick_live(used); 
        if c is not None: brk_axis=c; used.add(c)
    if clt_axis is None:
        c=pick_live(used); 
        if c is not None: clt_axis=c; used.add(c)
def begin_assign(mode):
    global assign_mode; assign_mode=mode
def try_capture_axis(js):
    global assign_mode,steer_axis,thr_axis,brk_axis,clt_axis
    if assign_mode is None or js is None: return
    best_i=None; best_v=0.0
    for i in range(js.get_numaxes()):
        d=abs(js.get_axis(i))
        if d>best_v: best_v=d; best_i=i
    if best_i is not None and best_v>=ASSIGN_DELTA:
        if   assign_mode=='steer': steer_axis=best_i
        elif assign_mode=='thr' :  thr_axis =best_i
        elif assign_mode=='brk' :  brk_axis =best_i
        elif assign_mode=='clt' :  clt_axis =best_i
        assign_mode=None; save_cfg()

# ---- maths ----
def read_raw(js, idx):
    if js is None or idx is None: return None
    if idx<0 or idx>=js.get_numaxes(): return None
    return float(js.get_axis(idx))  # −1..1
def update_cal(key, raw):
    if raw is None: return
    d = cal[key]
    if raw < d["min"]: d["min"]=raw
    if raw > d["max"]: d["max"]=raw
def map_to_pct(raw, key, invert=False):
    if raw is None: return None
    d = cal[key]; mn, mx = d["min"], d["max"]
    if mx - mn < 1e-5: return 50.0
    p = (raw - mn) / (mx - mn) * 100.0
    if invert: p = 100.0 - p
    if p < 0.0: p = 0.0
    if p > 100.0: p = 100.0
    return p
def map_wheel_deg(raw):
    if raw is None: return 0
    d = cal["steer"]; mn, mx = d["min"], d["max"]
    if mx - mn < 1e-5:
        center = 0.0; half = 1.0
    else:
        center = (mn + mx) / 2.0
        half = max(center - mn, mx - center)
        if half < 1e-4: half = 1.0
    norm = (raw - center) / half
    if norm < -1.0: norm = -1.0
    if norm >  1.0: norm =  1.0
    return int(round(norm * (wheel_range_deg/2)))
def smooth(key, target):
    if target is None: return None
    cur=disp[key]
    if abs(target-cur) < JITTER_EPS: return cur
    cur = cur + SMOOTH_ALPHA*(target-cur)
    disp[key]=cur; return cur

# ---- draw ----
BG=(20,20,20); FG=(240,240,240); ACC=(190,190,190); BAR_BG=(70,70,70)
def draw_text(x,y,s,f,clr=FG):
    surf=f.render(s,True,clr); screen.blit(surf,(x,y)); return y+surf.get_height()+2
def trim_to_width(s, font, maxw):
    if font.size(s)[0] <= maxw: return s
    while s and font.size(s+"…")[0] > maxw: s=s[:-1]
    return s+"…"
def draw_bar(rect, label, p, inv=False):
    x,y,w,h = rect
    h = max(10, h)
    lab_w = int(w*0.35)
    lab = trim_to_width(label, font2, lab_w-6)
    draw_text(x+4, y-2, lab, font2, (230,230,230))
    xbar = x + lab_w
    wbar = max(60, int(w*0.55))
    pygame.draw.rect(screen, BAR_BG, (xbar, y, wbar, h), 1)
    if p is None:
        draw_text(xbar+wbar+6, y-2, "—", font2, (220,220,220))
    else:
        pp = 100-p if inv else p
        if pp < 0: pp = 0
        if pp > 100: pp = 100
        fill = int((pp/100.0)*(wbar-2))
        if fill>0: pygame.draw.rect(screen, ACC, (xbar+1, y+1, fill, h-2))
        draw_text(xbar+wbar+6, y-2, "%3d%%" % int(round(pp)), font2, (220,220,220))

# ---- действия ----
def reset_calibration():
    for k in cal:
        cal[k]["min"]=+1.0; cal[k]["max"]=-1.0
    save_cfg()
def cycle_range():
    global wheel_range_deg
    for o in (360,540,720,900,1080,1440):
        if o>wheel_range_deg: wheel_range_deg=o; break
    else:
        wheel_range_deg=360
    save_cfg()
def toggle_clutch():
    global show_clutch; show_clutch = not show_clutch; save_cfg()
def toggle_view():
    global mini; mini = not mini; ensure_window(); save_cfg()
def toggle_top():
    global topmost; topmost = not topmost; apply_topmost(topmost); save_cfg()
def toggle_rec(device_name):
    global recording
    if recording: stop_recording()
    else: start_recording(device_name)
def mark_rec():
    global log_mark_id, log_mark_pending
    if recording:
        log_mark_id += 1
        log_mark_pending = "M%d" % log_mark_id
def split_rec(device_name):
    if recording:
        split_recording(device_name)

# ---- глобальные хоткеи ----
WM_HOTKEY = 0x0312
def start_hotkeys():
    global hk_error
    def worker():
        pairs = [
            (1, VK["ESC"]),
            (2, VK["F1"]), (3, VK["F2"]), (4, VK["F3"]), (5, VK["F4"]),
            (6, VK["F5"]), (7, VK["F6"]), (8, VK["F7"]),
            (9, VK["F8"]), (10, VK["F9"]),
            (11, VK["D1"]), (12, VK["D2"]), (13, VK["D3"]), (14, VK["D4"]),
            (15, VK["R"]),
            (16, VK["F10"]),  # REC
            (17, VK["F11"]),  # MARK
            (18, VK["F12"])   # SPLIT
        ]
        for idnum, vk in pairs:
            if not user32.RegisterHotKey(None, idnum, MOD_NOREPEAT, vk):
                hk_error = True
        msg = wt.MSG()
        while True:
            res = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if res == 0: break
            if msg.message == WM_HOTKEY:
                hotq.put(int(msg.wParam))
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    threading.Thread(target=worker, daemon=True).start()
def stop_hotkeys():
    for i in range(1, 19):
        try: user32.UnregisterHotKey(None, i)
        except Exception: pass
start_hotkeys()

# ---- основной цикл ----
def main():
    js, js_name = pick_joystick()
    auto_bind_if_unset(js)
    last_top = 0.0

    while True:
        # глобальные хоткеи
        try:
            while True:
                hid = hotq.get_nowait()
                if   hid==1:  stop_hotkeys(); pygame.quit(); sys.exit(0)
                elif hid==2:  begin_assign('steer')
                elif hid==3:  begin_assign('thr')
                elif hid==4:  begin_assign('brk')
                elif hid==5:  begin_assign('clt')
                elif hid==6:  reset_calibration()
                elif hid==7:  cycle_range()
                elif hid==8:  toggle_clutch()
                elif hid==9:  toggle_view()
                elif hid==10: toggle_top()
                elif hid==11: globals().__setitem__('inv_steer', not inv_steer)
                elif hid==12: globals().__setitem__('inv_thr',   not inv_thr)
                elif hid==13: globals().__setitem__('inv_brk',   not inv_brk)
                elif hid==14: globals().__setitem__('inv_clt',   not inv_clt)
                elif hid==15: js, js_name = pick_joystick(); auto_bind_if_unset(js)
                elif hid==16: toggle_rec(js_name)
                elif hid==17: mark_rec()
                elif hid==18: split_rec(js_name)
        except queue.Empty:
            pass

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                stop_hotkeys(); stop_recording(); pygame.quit(); sys.exit(0)
            if e.type == pygame.VIDEORESIZE and mini:
                w = max(MINI_MIN_W, e.w)
                rows = 3 + (1 if show_clutch else 0)
                base = 26 + rows*22 + 20
                h = max(MINI_MIN_H, e.h, base)
                pygame.display.set_mode((w,h), flags=pygame.RESIZABLE)
                apply_topmost(topmost)

        if js is not None and not js.get_init():
            js, js_name = pick_joystick(); auto_bind_if_unset(js)
        auto_bind_if_unset(js); try_capture_axis(js)

        # raw −1..1
        r_steer = read_raw(js, steer_axis)
        r_thr   = read_raw(js,   thr_axis)
        r_brk   = read_raw(js,   brk_axis)
        r_clt   = read_raw(js,   clt_axis)  # логируем, даже если сцепление скрыто

        # калибровка
        update_cal("steer", r_steer); update_cal("thr", r_thr); update_cal("brk", r_brk); update_cal("clt", r_clt)

        # проценты/градусы (НЕ сглаженные — для точного лога)
        thr_pct = map_to_pct(r_thr, "thr", invert=inv_thr)
        brk_pct = map_to_pct(r_brk, "brk", invert=inv_brk)
        clt_pct = map_to_pct(r_clt, "clt", invert=inv_clt)
        deg = map_wheel_deg(r_steer)
        if inv_steer: deg = -deg
        steer_pct = (deg/(wheel_range_deg/2))*50.0 + 50.0
        if steer_pct < 0: steer_pct = 0.0
        if steer_pct > 100: steer_pct = 100.0

        # сглаженные — только для отрисовки
        s_steer = smooth("steer", steer_pct)
        s_thr   = smooth("thr",   thr_pct)
        s_brk   = smooth("brk",   brk_pct)
        s_clt   = smooth("clt",   clt_pct) if show_clutch else None

        # кнопки
        btn_mask = 0
        btn_list = []
        if js:
            nb = js.get_numbuttons()
            for i in range(nb):
                if js.get_button(i):
                    btn_mask |= (1<<i)
                    btn_list.append(i+1)

        # запись
        if recording:
            iso = now_iso()
            t_ms = int((time.time() - log_started) * 1000.0)
            mark = log_mark_pending
            write_row(iso, t_ms, deg, steer_pct, thr_pct, brk_pct, clt_pct, btn_mask, btn_list, mark)
            log_mark_pending = ""  # сбрасываем после записи
            try_flush()

        # ---- отрисовка ----
        screen.fill((20,20,20))
        rec_txt = " | REC" if recording else ""
        if not mini:
            y=10
            hkwarn = " | HK ERR" if hk_error else ""
            y=draw_text(10,y,"Device: %s (джойстиков: %d) [F10 rec F11 mark F12 split | F8 вид | F7 сцепл | F6 %d° | F9 top | F5 reset | ESC]%s%s" %
                        (js_name, pygame.joystick.get_count(), wheel_range_deg, rec_txt, hkwarn), font, (235,235,235))
            y=draw_text(10,y,"руль: %+4d°" % deg, font2, (220,220,220)); y+=6
            full_w = screen.get_width()-20
            row_h  = 24
            draw_bar((10,y,full_w,row_h), "руль  (A%s, ±%d°)" % (steer_axis if steer_axis is not None else "—", wheel_range_deg), s_steer); y+=28
            draw_bar((10,y,full_w,row_h), "газ   (A%s)" % (thr_axis if thr_axis is not None else "—"), s_thr, inv_thr); y+=28
            draw_bar((10,y,full_w,row_h), "тормоз(A%s)" % (brk_axis if brk_axis is not None else "—"), s_brk, inv_brk); y+=28
            if show_clutch:
                draw_bar((10,y,full_w,row_h), "сцепл.(A%s)" % (clt_axis if clt_axis is not None else "—"), s_clt, inv_clt); y+=28
            # кнопки
            btn_line="Кнопки: " + (", ".join(str(x) for x in btn_list) if btn_list else "—")
            btn_line = trim_to_width(btn_line, font, screen.get_width()-20)
            draw_text(10,y,btn_line,font,(235,235,235))
        else:
            w,h = screen.get_width(), screen.get_height()
            left  = "[F10 rec][F11 mark][F12 split][F8 вид][F7 сцепл]"
            right = "%+4d°  F6:%d%s%s" % (deg, wheel_range_deg, (" REC" if recording else ""), (" HKERR" if hk_error else ""))
            left  = trim_to_width(left,  fontMini, w//2 - 8)
            right = trim_to_width(right, fontMini, w//2 - 8)
            draw_text(6,4,left,fontMini,(220,220,220))
            surf = fontMini.render(right, True, (220,220,220))
            screen.blit(surf, (w - surf.get_width() - 6, 4))
            rows = 3 + (1 if show_clutch else 0)
            top  = 22; bottom = 20
            avail_h = max(40, h - top - bottom)
            row_h = max(14, int(avail_h / rows))
            y = top
            draw_bar((6,y,w-12,row_h-6), "руль",   s_steer, False); y+=row_h
            draw_bar((6,y,w-12,row_h-6), "газ",    s_thr,   inv_thr); y+=row_h
            draw_bar((6,y,w-12,row_h-6), "тормоз", s_brk,   inv_brk); y+=row_h
            if show_clutch:
                draw_bar((6,y,w-12,row_h-6), "сцепл.", s_clt, inv_clt); y+=row_h
            btn="Кн: " + (", ".join(str(x) for x in btn_list) if btn_list else "—")
            btn = trim_to_width(btn, fontMini, w-12)
            draw_text(6, h-fontMini.get_height()-4, btn, fontMini, (220,220,220))

        if topmost and time.time()-last_top > 2.0:
            apply_topmost(True); last_top = time.time()

        pygame.display.flip()
        clock.tick(60)

# ---- run ----
if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        stop_hotkeys(); stop_recording()
    except Exception as ex:
        stop_hotkeys(); stop_recording()
        try:
            with open(ERR,"w",encoding="utf-8") as f: f.write(traceback.format_exc())
        finally:
            print("Ошибка:", ex); print("Лог:", ERR)
            try: input("Enter для выхода…")
            except Exception: pass
