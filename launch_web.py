"""Open the local UI after Uvicorn starts accepting connections."""
import os
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn


def open_when_ready():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            with opener.open("http://127.0.0.1:8000/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open("http://127.0.0.1:8000")
                    return
        except OSError:
            time.sleep(0.5)


if __name__ == "__main__":
    os.environ["WEB_AUTH_MODE"] = "local"
    os.environ["DRY_RUN"] = "true"
    os.environ["ENABLE_LIVE_APPROVAL"] = "false"
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8000))
        except OSError:
            raise SystemExit("Port 8000 is in use. Close the previous service before restarting.") from None
    threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run("web.app:app", host="127.0.0.1", port=8000, proxy_headers=False)
