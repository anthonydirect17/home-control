"""Start Home Control.

    python -m home_control              control your real lights via Govee Desktop
    python -m home_control --demo       try it with pretend lights
    python -m home_control --set-guid   save the Govee Desktop API GUID in Windows Credential Manager
"""

import argparse
import getpass
import os
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(prog="home_control", description="Control center for Govee lights.")
    parser.add_argument("--demo", action="store_true", help="use pretend lights instead of Govee Desktop")
    parser.add_argument("--host", default="127.0.0.1", help="address to serve on (default: this PC only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default=os.path.join(ROOT, "data"), help="folder for saved settings")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    parser.add_argument("--set-guid", action="store_true", help="save the Govee Desktop API GUID and exit")
    args = parser.parse_args()

    from .secrets import APP_TARGET, get_guid, write_credential

    if args.set_guid:
        guid = getpass.getpass("Govee Desktop API GUID (from Settings -> API): ").strip()
        if not guid:
            print("Nothing entered; nothing saved.")
            return 1
        write_credential(APP_TARGET, guid)
        print(f"Saved in Windows Credential Manager as '{APP_TARGET}'.")
        return 0

    from .controller import Controller
    from .scheduler import Scheduler
    from .server import create_app
    from .store import Store

    store = Store(os.path.join(args.data, "settings.json"))

    if args.demo:
        from .govee.demo import DemoBackend
        backend = DemoBackend()
    else:
        from .govee.desktop import GoveeDesktopBackend
        guid, source = get_guid(store.get("settings"))
        if not guid:
            print(f"Can't connect to Govee Desktop: {source}")
            print("Or try the app with pretend lights:  python -m home_control --demo")
            return 2
        print(f"Using the API GUID from {source}.")
        backend = GoveeDesktopBackend(guid, store.get("settings").get("govee_dll"))

    controller = Controller(backend, store)
    status = controller.connect()
    if status["connected"]:
        print(f"Connected to {backend.label}: {len(controller.devices)} devices.")
    else:
        print(f"Not connected yet: {status['error']}")
        print("The page will still open; use Reconnect there once it's fixed.")

    scheduler = Scheduler(controller, store)
    scheduler.start()

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
        controller.stop_all()
    return 0


if __name__ == "__main__":
    sys.exit(main())
