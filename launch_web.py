"""Open the local UI after Uvicorn starts accepting connections."""
import os
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn


def web_port():
    return int(os.getenv("WEB_PORT", "8000"))


def open_when_ready():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            with opener.open(f"http://127.0.0.1:{web_port()}/health", timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(f"http://127.0.0.1:{web_port()}")
                    return
        except OSError:
            time.sleep(0.5)


if __name__ == "__main__":
    os.environ["WEB_AUTH_MODE"] = "local"
    os.environ["DRY_RUN"] = "true"
    os.environ["ENABLE_LIVE_APPROVAL"] = "false"
    port = web_port()
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(f"Port {port} is in use. Close the previous service before restarting.") from None
    threading.Thread(target=open_when_ready, daemon=True).start()
    uvicorn.run("web.app:app", host="127.0.0.1", port=port, proxy_headers=False)
