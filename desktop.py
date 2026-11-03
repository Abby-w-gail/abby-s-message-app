# desktop app starts here
import ctypes
import os
import socket
import sys
import threading
import time

import webview

PORT = 8000  # local server port here
TITLE = "abby's chatroom(s)"  # window title text here
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ICON = os.path.join(BASE, "static", "icon.ico")
SERVER = "https://chatroom.abbyw.net/"  # put railway url here later


def port_open(port):  # is a server already running
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def run_server():  # local server runs in background
    import uvicorn
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", log_config=None)


if len(sys.argv) > 1:  # url given, use it
    url = sys.argv[1]
elif SERVER:  # online mode, real chatting
    url = SERVER
else:
    if not port_open(PORT):  # second window reuses first server
        threading.Thread(target=run_server, daemon=True).start()
        for _ in range(50):  # wait for server to start
            if port_open(PORT):
                break
            time.sleep(0.1)
    url = f"http://127.0.0.1:{PORT}" 

# window icon starts here
def set_icon(window):  # swaps python logo for ours
    window.events.shown.wait(10)
    if os.name != "nt" or not os.path.exists(ICON):
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    hwnd = user32.FindWindowW(None, TITLE)
    if not hwnd:
        return
    for size, which in ((16, 0), (32, 1)):  # small and big icons
        icon = user32.LoadImageW(None, ICON, 1, size, size, 0x10)
        user32.SendMessageW(hwnd, 0x80, which, icon)


if os.name == "nt":  # own taskbar entry, not python
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("abby.chatrooms")
# window icon ends here

# native window opens here
window = webview.create_window(TITLE, url, width=800, height=830,
                               min_size=(500, 520), background_color="#f4a6c8")
webview.start(set_icon, window)
# desktop app ends here
