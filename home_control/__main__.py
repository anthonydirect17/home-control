"""Start Home Control.

    python -m home_control                 control your lights
    python -m home_control --demo          try it with pretend lights
    python -m home_control --set-govee-key save your Govee API key (Credential Manager on Windows,
                                           ~/.config/home-control/secrets.json elsewhere)
    python -m home_control --set-guid      save the Govee Desktop API GUID there (optional)
"""

import argparse
import getpass
import os
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def save_secret(name):
    from .secrets import SECRETS, save_secret_value
    value = getpass.getpass(f"{SECRETS[name]['label']} (typing is hidden): ").strip()
    if not value:
        print("Nothing entered; nothing saved.")
        return 1
    print(f"Saved in {save_secret_value(name, value)}.")
    return 0


def build_backends(store, args):
    if args.demo:
        from .govee.demo import DemoBackend
        return [DemoBackend()]

    from .govee.lan import LanBackend
    from .secrets import get_secret
    backends = [LanBackend(store)]
    print("Govee LAN: on (lights with LAN Control turned on)")

    key, where = get_secret("govee-api-key", store.get("settings"))
    if key:
        from .govee.cloud import CloudBackend
        backends.append(CloudBackend(key))
        print(f"Govee cloud: on (key from {where})")
    else:
        print(f"Govee cloud: off, API key {where}")

    if store.get("settings").get("use_govee_desktop", True):
        guid, where = get_secret("govee-desktop-guid", store.get("settings"))
        if guid:
            from .govee.desktop import GoveeDesktopBackend
            backends.append(GoveeDesktopBackend(guid, store.get("settings").get("govee_dll")))
            print(f"Govee Desktop: on (GUID from {where}); used for multi-segment effects")
        else:
            print(f"Govee Desktop: off, GUID {where}. Only needed for multi-segment effects.")
    return backends


def main():
    parser = argparse.ArgumentParser(prog="home_control", description="Control center for Govee lights.")
    parser.add_argument("--demo", action="store_true", help="use pretend lights")
    parser.add_argument("--host", default="127.0.0.1", help="address to serve on (default: this PC only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=os.path.join(ROOT, "data"), help="folder for saved settings")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    parser.add_argument("--set-guid", action="store_true", help="save the Govee Desktop API GUID and exit")
    parser.add_argument("--set-govee-key", action="store_true", help="save the Govee API key and exit")
    args = parser.parse_args()

    if args.set_guid:
        return save_secret("govee-desktop-guid")
    if args.set_govee_key:
        return save_secret("govee-api-key")

    from .controller import Controller, StatePoller
    from .hub import Hub
    from .scheduler import Scheduler
    from .server import create_app
    from .store import Store

    store = Store(os.path.join(args.data, "settings.json"))
    hub = Hub(build_backends(store, args), store)
    controller = Controller(hub, store)
    status = controller.connect()
    for route, s in status["backends"].items():
        if not s["connected"]:
            print(f"{s['label']}: not connected: {s['error']}")
        elif s["note"]:
            print(f"{s['label']}: {s['note']}")
    print(f"{status['device_count']} lights found.")

    scheduler = Scheduler(controller, store)
    scheduler.start()
    poller = StatePoller(controller)
    poller.start()

    import uvicorn
    app = create_app(controller, store, scheduler)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
    print(f"Home Control is running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        scheduler.stop()
        poller.stop()
        controller.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
