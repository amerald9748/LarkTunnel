# -*- coding: utf-8 -*-
r"""
desktop.py — LarkTunnel as a desktop app (one window, no console, no browser).
================================================================================

WHAT IT DOES
    1. Reads per-user settings (%APPDATA%\LarkTunnel\settings.json): env, port.
    2. If a LarkTunnel server already answers on that port (e.g. the owner's
       Task Scheduler service), it ATTACHES to it. Otherwise it starts the
       server in-process on 127.0.0.1 (falls back to the next free port).
    3. Opens the UI in a native window:
         pywebview + Edge WebView2  →  msedge --app=<url>  →  default browser
    4. When the window closes (or, for the fallbacks, when the page's
       heartbeat stops), it shuts the in-process server down and exits.

    Everything else — credentials, 授权, the workflow — lives in the web UI
    (⚙ 设置). This file is only the shell.

BUILD
    webapp\build.bat  →  webapp\dist\LarkTunnel\LarkTunnel.exe   (PyInstaller)
    Run from a checkout too:  python webapp\desktop.py

FLAGS / ENV
    --console          keep stdout on the console (default when not frozen)
    --browser          skip the native window, open the default browser
    LARK_ENV/LARK_PORT override settings.json for this launch
"""
import os
import sys
import time
import socket
import threading
import subprocess
import webbrowser
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import apppaths          # noqa: E402
import app_settings      # noqa: E402

TITLE = "LarkTunnel · 到仓核对台"
HEARTBEAT_GRACE = 90.0   # s without /api/ping after the page was seen -> exit
NEVER_SEEN_GRACE = 300.0  # s with no page at all (window failed to open?)


def _log_to_file():
    """Windowed exe has no console: send prints to logs/desktop.log."""
    path = apppaths.state_path("logs", "desktop.log")
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f
    print(f"\n===== LarkTunnel desktop start {time.strftime('%Y-%m-%d %H:%M:%S')} =====")


def _healthy(port, timeout=1.0):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            body = r.read(200).decode("utf-8", "ignore")
        return '"ok": true' in body.replace(" ", "").lower() or '"ok":true' in body.lower()
    except Exception:
        return False


def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _start_server(preferred):
    """Start the HTTP server in a daemon thread. Returns (httpd, port)."""
    import server
    last = None
    for port in [preferred] + [p for p in range(8790, 8800) if p != preferred]:
        if not _port_free(port):
            continue
        try:
            httpd = server.build_server(port)
        except OSError as e:
            last = e
            continue
        threading.Thread(target=httpd.serve_forever, name="larktunnel-http",
                         daemon=True).start()
        for _ in range(50):               # ≤5 s until it answers
            if _healthy(port, 0.5):
                return httpd, port
            time.sleep(0.1)
        return httpd, port
    raise SystemExit(f"cannot start local server (all ports busy): {last}")


def _edge_path():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
        if os.path.isfile(p):
            return p
    return None


def _open_webview(url, size):
    """Native window via pywebview (Edge WebView2). Blocks until closed.
    Returns False when pywebview is unavailable so the caller can fall back."""
    try:
        import webview
    except Exception as e:  # noqa
        print(f"[desktop] pywebview unavailable: {e}")
        return False
    w, h = int(size.get("width") or 1440), int(size.get("height") or 900)
    storage = os.path.join(apppaths.data_dir(), "webview")
    os.makedirs(storage, exist_ok=True)
    win = webview.create_window(TITLE, url, width=w, height=h, min_size=(1000, 680),
                                text_select=True)
    last = {"w": w, "h": h}

    def on_resized(width, height):
        last["w"], last["h"] = width, height

    def on_closed():
        try:
            app_settings.update_settings({"window": {"width": last["w"], "height": last["h"]}})
        except Exception:
            pass
    win.events.resized += on_resized
    win.events.closed += on_closed
    try:
        # private_mode=False keeps localStorage (theme, drafts) between runs
        webview.start(gui="edgechromium", private_mode=False, storage_path=storage)
        return True
    except Exception as e:  # noqa
        print(f"[desktop] webview failed: {e}")
        return False


def _open_edge_app(url, size):
    """Edge in --app mode (no address bar) with its own profile folder.
    Returns the Popen or None."""
    edge = _edge_path()
    if not edge:
        return None
    profile = os.path.join(apppaths.data_dir(), "edge-profile")
    os.makedirs(profile, exist_ok=True)
    w, h = int(size.get("width") or 1440), int(size.get("height") or 900)
    try:
        return subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}",
                                 f"--window-size={w},{h}", "--no-first-run",
                                 "--no-default-browser-check"])
    except OSError as e:
        print(f"[desktop] edge launch failed: {e}")
        return None


def _wait_for_heartbeat_end():
    """For the fallbacks (Edge hand-off / browser tab): the page pings
    /api/ping every ~20 s while open; exit once it has been silent."""
    import server
    t0 = time.time()
    while True:
        time.sleep(5)
        lp = server.last_ping()
        if lp and time.time() - lp > HEARTBEAT_GRACE:
            return
        if not lp and time.time() - t0 > NEVER_SEEN_GRACE:
            return


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if apppaths.is_frozen() and "--console" not in argv:
        _log_to_file()

    st = app_settings.get_settings()
    os.environ.setdefault("LARK_ENV", st.get("env") or "prod")
    os.environ.setdefault("LARK_PORT", str(st.get("port") or 8787))
    preferred = int(os.environ["LARK_PORT"])

    httpd = None
    if _healthy(preferred):
        port = preferred                     # attach to the running service
        print(f"[desktop] attached to existing server on :{port}")
    else:
        httpd, port = _start_server(preferred)
        print(f"[desktop] started server on :{port} (env {os.environ['LARK_ENV']})")
    url = f"http://127.0.0.1:{port}/"

    size = st.get("window") or {}
    try:
        if "--browser" in argv:
            webbrowser.open(url)
            if httpd:
                _wait_for_heartbeat_end()
        elif _open_webview(url, size):
            pass                              # window closed normally
        else:
            proc = _open_edge_app(url, size)
            if proc is None:
                webbrowser.open(url)
                if httpd:
                    _wait_for_heartbeat_end()
            else:
                proc.wait()
                # A quick exit means Edge handed the URL to an instance that
                # already owns this profile — keep serving while it is used.
                if httpd:
                    _wait_for_heartbeat_end()
    finally:
        if httpd:
            print("[desktop] shutting down server")
            httpd.shutdown()
    print("[desktop] bye")


if __name__ == "__main__":
    main()
