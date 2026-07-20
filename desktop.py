#!/usr/bin/env python3
"""
multibuy desktop — runs the dashboard inside a native window (pywebview).

    pip install -r requirements.txt pywebview
    python3 desktop.py

Or build a single Windows .exe with build.bat (PyInstaller). The Flask server
runs on a background thread bound to localhost; the window points at it. Closing
the window stops everything (including any running DCA / auto-sell).
"""

import os
import socket
import threading
import time

# Make sure the bundled CA certificates are used for outbound HTTPS (Jupiter,
# PumpPortal, Dexscreener, Jito, RPCs). In a frozen PyInstaller build the OS
# trust store may not be reachable, so point the common env vars at certifi's
# bundle before anything imports requests/urllib3.
try:
    import certifi
    _ca = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", _ca)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
except Exception:
    pass

import webview            # pywebview
import dashboard


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(port):
    # use_reloader=False is required when running Flask off the main thread.
    dashboard.app.run(host="127.0.0.1", port=port, debug=False,
                      use_reloader=False, threaded=True)


def _wait_until_up(port, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    port = _free_port()
    threading.Thread(target=_serve, args=(port,), daemon=True).start()
    _wait_until_up(port)
    webview.create_window("multibuy", f"http://127.0.0.1:{port}",
                          width=1320, height=920, min_size=(1000, 700))
    # The taskbar / .exe icon is set by PyInstaller's --icon flag (see build.bat).
    webview.start()   # blocks until the window is closed


if __name__ == "__main__":
    main()
