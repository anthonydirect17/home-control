"""Start Home Control.

    python -m home_control                    control your lights
    python -m home_control --demo             try it with pretend lights
    python -m home_control --set-pin          set the 6-digit page PIN
    python -m home_control --set-govee-key    save your Govee API key
    python -m home_control --set-guid         save the Govee Desktop API GUID (optional)
    python -m home_control --ring-login       sign in to Ring (email, hidden password, two-step code)
    python -m home_control --secrets-store credential-manager
                                              keep secrets in Windows Credential Manager instead
                                              of data/secrets.json (see secrets.py)

Secrets go into the active secrets store: by default data/secrets.json in the
app's own data folder, locked to the account running the app. Typing is
always hidden.
"""

import argparse
import getpass
import os
import sys
import threading
import time
import traceback
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_MAX_BYTES = 1_000_000
LOG_KEEP = 3
RESTART_DELAY = 60


def save_secret(name, settings):
    from .secrets import SECRETS, save_secret_value
    value = getpass.getpass(f"{SECRETS[name]['label']} (typing is hidden): ").strip()
    if not value:
        print("Nothing entered; nothing saved.")
        return 1
    print(f"Saved in {save_secret_value(name, value, settings)}.")
    return 0


def set_pin(settings):
    from .auth import PIN_LENGTH, hash_pin, valid_pin_format
    from .secrets import save_secret_value
    pin = getpass.getpass(f"New {PIN_LENGTH}-digit page PIN (typing is hidden): ").strip()
    if not valid_pin_format(pin):
        print(f"The PIN must be exactly {PIN_LENGTH} digits. Nothing saved.")
        return 1
    if getpass.getpass("Type it again: ").strip() != pin:
        print("The two entries didn't match. Nothing saved.")
        return 1
    where = save_secret_value("page-pin-hash", hash_pin(pin), settings)
    print(f"PIN saved (only a salted hash) in {where}.")
    print("Every remembered device will be asked for the new PIN. Restart the app if it's running.")
    return 0


def set_secrets_store(store, choice):
    from .secrets import STORE_FILE, copy_secrets_to_file, secrets_file
    if sys.platform != "win32" and choice != STORE_FILE:
        print("Only Windows has Credential Manager; this system always uses the private file.")
        return 1
    if choice == STORE_FILE and sys.platform == "win32":
        copied = copy_secrets_to_file(store.get("settings"))
        if copied:
            print(f"Copied {len(copied)} saved secret(s) from Credential Manager into {secrets_file()}: "
                  + ", ".join(copied))
    store.update_settings({"secrets_store": choice})
    print(f"Secrets store is now: {choice}. Restart the app if it's running.")
    return 0


class MaskedLog:
    """A log file that keeps IP addresses out: everything written passes through mask_addresses()."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        from .netaddr import mask_addresses
        self._stream.write(mask_addresses(text))
        return len(text)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def open_log(path):
    """Send all output to a log file, keeping a few old ones. Needed when started hidden (pythonw).

    Addresses are masked in everything written, tracebacks included."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
        for i in range(LOG_KEEP - 1, 0, -1):
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        os.replace(path, f"{path}.1")
    log = MaskedLog(open(path, "a", encoding="utf-8", buffering=1))
    sys.stdout = sys.stderr = log
    print(f"--- start {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


def build_backends(store, args):
    if args.demo:
        from .govee.demo import DemoBackend
        return [DemoBackend()]

    from .govee.lan import LanBackend
    from .secrets import get_secret
    settings = store.get("settings")
    backends = [LanBackend(store)]
    print("Govee LAN: on (lights with LAN Control turned on)")

    key, where = get_secret("govee-api-key", settings)
    if key:
        from .govee.cloud import CloudBackend
        backends.append(CloudBackend(key))
        print(f"Govee cloud: on (key from {where})")
    else:
        print(f"Govee cloud: off, API key {where}")

    if settings.get("use_govee_desktop", True):
        guid, where = get_secret("govee-desktop-guid", settings)
        if guid:
            from .govee.desktop import GoveeDesktopBackend
            backends.append(GoveeDesktopBackend(guid, settings.get("govee_dll")))
            print(f"Govee Desktop: on (GUID from {where}); used for multi-segment effects")
        else:
            print(f"Govee Desktop: off, GUID {where}. Only needed for multi-segment effects.")
    return backends


def build_ring(store, args, bus):
    from .ring import TOKEN_SECRET, DemoRing, RingBridge, node_command, setup_problem
    from .secrets import get_secret, save_secret_value
    if args.demo:
        return DemoRing(bus)
    settings = store.get("settings")
    problem = setup_problem(settings.get("node_path"))
    if problem:
        print(f"Ring: off, {problem}.")
        return None
    print("Ring: on (signs in with the saved Ring token; --ring-login if there is none)")
    return RingBridge(
        node_command(settings.get("node_path")),
        get_token=lambda: get_secret(TOKEN_SECRET, store.get("settings")),
        save_token=lambda token: save_secret_value(TOKEN_SECRET, token, store.get("settings")),
        bus=bus,
        port=int(settings.get("ring_helper_port") or 8782),
    )


def build_gate(store, demo):
    from .auth import PinGate, load_or_create_session_key
    from .secrets import get_secret, save_secret_value
    settings = store.get("settings")
    pin_hash, _ = get_secret("page-pin-hash", settings)
    key, saved = load_or_create_session_key(get_secret, save_secret_value, settings)
    if not saved:
        print("Couldn't save the cookie key; devices will need the PIN again after a restart.")
    gate = PinGate(pin_hash, key, required=not demo)
    if not gate.pin_set:
        print("No page PIN is set; the page stays locked until you run: python -m home_control --set-pin"
              if gate.required else "Demo mode with no PIN set: the page is open.")
    return gate


def main():
    parser = argparse.ArgumentParser(prog="home_control", description="Control center for Govee lights.")
    parser.add_argument("--demo", action="store_true", help="use pretend lights")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to serve on (default: this PC only); 'tailscale' = this machine's "
                             "current Tailscale address, looked up at every start")
    parser.add_argument("--port", type=int, default=8780)  # 8765 is reserved for the trading desk; never use it
    parser.add_argument("--data", default=os.path.join(ROOT, "data"), help="folder for saved settings")
    parser.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    parser.add_argument("--log-file", help="write all output here (rotated at 1 MB, 3 kept)")
    parser.add_argument("--keep-running", action="store_true",
                        help="restart automatically 60 s after any stop or crash (for the auto-start task)")
    parser.add_argument("--set-pin", action="store_true", help="set the 6-digit page PIN and exit")
    parser.add_argument("--set-guid", action="store_true", help="save the Govee Desktop API GUID and exit")
    parser.add_argument("--set-govee-key", action="store_true", help="save the Govee API key and exit")
    parser.add_argument("--ring-login", action="store_true", help="sign in to Ring and exit")
    parser.add_argument("--secrets-store", choices=["credential-manager", "file"],
                        help="choose where secrets are kept, then exit")
    args = parser.parse_args()

    from .secrets import set_data_dir
    from .store import Store
    set_data_dir(args.data)
    store = Store(os.path.join(args.data, "settings.json"))

    if args.secrets_store:
        return set_secrets_store(store, args.secrets_store)
    if args.set_pin:
        return set_pin(store.get("settings"))
    if args.set_guid:
        return save_secret("govee-desktop-guid", store.get("settings"))
    if args.set_govee_key:
        return save_secret("govee-api-key", store.get("settings"))
    if args.ring_login:
        from .ring import TOKEN_SECRET, ring_login
        from .secrets import save_secret_value
        settings = store.get("settings")
        return ring_login(settings.get("node_path"), lambda t: save_secret_value(TOKEN_SECRET, t, settings))

    if args.log_file:
        open_log(args.log_file)
    if not args.keep_running:
        try:
            return serve(args, store)
        except RuntimeError as e:  # e.g. --host tailscale with Tailscale not running
            print(f"Can't start: {e}")
            return 2
    # Hidden auto-start: if the app stops for any reason other than Ctrl+C, start it again.
    while True:
        try:
            serve(args, store)
            print("Home Control stopped; starting again in 60 s.")
        except KeyboardInterrupt:
            return 0
        except BaseException:  # includes SystemExit from uvicorn (e.g. port busy)
            traceback.print_exc()
            print("Home Control crashed; starting again in 60 s.")
        time.sleep(RESTART_DELAY)


def serve(args, store):
    from .controller import Controller, StatePoller
    from .hub import Hub
    from .netaddr import resolve_host
    from .scheduler import Scheduler
    from .server import create_app

    host = resolve_host(args.host)  # raises if Tailscale isn't up yet; --keep-running retries
    from .events import EventBus

    started_at = time.time()
    gate = build_gate(store, args.demo)
    hub = Hub(build_backends(store, args), store)
    controller = Controller(hub, store)
    bus = EventBus()
    bus.subscribe(lambda event: controller.touch())  # Ring events count as activity for /api/health
    ring = build_ring(store, args, bus)
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
    if ring:
        ring.start()

    import uvicorn
    app = create_app(controller, store, scheduler, gate, started_at, ring=ring, bus=bus)
    if args.host.lower() == "tailscale":
        # The address itself stays out of the log.
        url = None
        print(f"Home Control is running on this machine's Tailscale address, port {args.port}")
    else:
        url = f"http://{host}:{args.port}/"
        print(f"Home Control is running at {url}  (Ctrl+C to stop)")
    if url and not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(app, host=host, port=args.port, log_level="warning")
    finally:
        scheduler.stop()
        poller.stop()
        if ring:
            ring.stop()
        controller.stop_all()
        hub.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
