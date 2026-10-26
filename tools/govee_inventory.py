"""Govee device inventory.

Lists every Govee device on your account (cloud API) and every device that
answers on your home network (LAN API), then writes the combined list to
govee_devices.csv and govee_devices.json next to this script.

Run on the Windows PC, on the same network as the lights:

    python govee_inventory.py

It asks for your Govee API key (typing is hidden). You can also set the
GOVEE_API_KEY environment variable instead. The key is never written to
disk or printed. Uses only the Python standard library; nothing to install.

Get a key in the Govee Home app: Profile -> Settings (gear) ->
Apply for API Key. Govee emails it to you.
"""

import csv
import getpass
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

CLOUD_DEVICES_URL = "https://openapi.api.govee.com/router/api/v1/user/devices"
LAN_MULTICAST = ("239.255.255.250", 4001)
LAN_LISTEN_PORT = 4002
LAN_WAIT_SECONDS = 4.0

HERE = os.path.dirname(os.path.abspath(__file__))


def norm_id(device_id):
    return (device_id or "").replace(":", "").upper()


def fetch_cloud_devices(api_key):
    req = urllib.request.Request(
        CLOUD_DEVICES_URL,
        headers={"Govee-API-Key": api_key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Cloud: the API key was rejected (401). Check it and try again.")
        elif e.code == 429:
            print("Cloud: Govee rate limit hit (429). Wait a minute and try again.")
        else:
            print(f"Cloud: HTTP error {e.code}: {e.reason}")
        return None
    except urllib.error.URLError as e:
        print(f"Cloud: could not reach Govee ({e.reason}).")
        return None

    if body.get("code") != 200:
        print(f"Cloud: Govee returned code {body.get('code')}: {body.get('message')}")
        return None
    return body.get("data") or []


def segment_count(capabilities):
    """Number of addressable segments, if the device reports segment color."""
    for cap in capabilities:
        if cap.get("instance") != "segmentedColorRgb":
            continue
        for field in (cap.get("parameters") or {}).get("fields") or []:
            if field.get("fieldName") == "segment":
                rng = field.get("elementRange") or {}
                if "max" in rng:
                    return int(rng["max"]) - int(rng.get("min", 0)) + 1
                opts = field.get("options") or []
                if opts:
                    return len(opts)
    return None


def local_ipv4_addresses():
    addrs = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except socket.gaierror:
        pass
    # The address Windows would use for the default route.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        addrs.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def scan_lan():
    """Send the Govee LAN scan on every local adapter and collect replies."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("", LAN_LISTEN_PORT))
    except OSError as e:
        print(f"LAN: could not listen on UDP port {LAN_LISTEN_PORT} ({e}).")
        print("     Close the Govee desktop app or anything else using that port and retry.")
        return {}
    listener.settimeout(0.5)

    scan = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserve"}}}).encode()
    adapters = local_ipv4_addresses()
    print(f"LAN: scanning from {', '.join(adapters) or 'default adapter'} ...")
    for addr in adapters or [None]:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if addr:
            try:
                sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(addr))
            except OSError:
                pass
        try:
            sender.sendto(scan, LAN_MULTICAST)
        except OSError:
            pass
        sender.close()

    found = {}
    deadline = time.time() + LAN_WAIT_SECONDS
    while time.time() < deadline:
        try:
            data, src = listener.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            info = json.loads(data.decode())["msg"]["data"]
        except (ValueError, KeyError, TypeError):
            continue
        dev_id = norm_id(info.get("device"))
        if dev_id:
            found[dev_id] = {
                "ip": info.get("ip") or src[0],
                "sku": info.get("sku", ""),
                "device": info.get("device", ""),
                "wifi_fw": info.get("wifiVersionSoft", ""),
            }
    listener.close()
    return found


def main():
    api_key = os.environ.get("GOVEE_API_KEY", "").strip()
    if not api_key:
        api_key = getpass.getpass("Govee API key (Enter to skip cloud lookup): ").strip()

    cloud = fetch_cloud_devices(api_key) if api_key else None
    if not api_key:
        print("Cloud: skipped (no key). Only LAN devices will be listed.")
    lan = scan_lan()

    rows = []
    seen = set()
    for dev in cloud or []:
        dev_id = norm_id(dev.get("device"))
        seen.add(dev_id)
        caps = dev.get("capabilities") or []
        lan_hit = lan.get(dev_id)
        rows.append({
            "name": dev.get("deviceName", ""),
            "model": dev.get("sku", ""),
            "type": (dev.get("type") or "").replace("devices.types.", ""),
            "lan": "yes" if lan_hit else "no",
            "ip": lan_hit["ip"] if lan_hit else "",
            "segments": segment_count(caps) or "",
            "features": " ".join(sorted({c.get("instance", "") for c in caps if c.get("instance")})),
            "device_id": dev.get("device", ""),
        })
    for dev_id, hit in lan.items():
        if dev_id in seen:
            continue
        rows.append({
            "name": "(LAN only, not in cloud list)",
            "model": hit["sku"],
            "type": "",
            "lan": "yes",
            "ip": hit["ip"],
            "segments": "",
            "features": "",
            "device_id": hit["device"],
        })

    rows.sort(key=lambda r: (r["model"], r["name"]))

    csv_path = os.path.join(HERE, "govee_devices.csv")
    json_path = os.path.join(HERE, "govee_devices.json")
    fields = ["name", "model", "type", "lan", "ip", "segments", "features", "device_id"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"devices": rows, "cloud_raw": cloud or []}, f, indent=2)

    print()
    print(f"{'NAME':32} {'MODEL':8} {'LAN':4} {'IP':16} SEGMENTS")
    for r in rows:
        print(f"{r['name'][:32]:32} {r['model']:8} {r['lan']:4} {r['ip']:16} {r['segments']}")
    print()
    lan_count = sum(1 for r in rows if r["lan"] == "yes")
    print(f"{len(rows)} devices total, {lan_count} answering on the local network.")
    print(f"Saved: {csv_path}")
    print(f"       {json_path}")
    if cloud is not None and lan_count < len(rows):
        print("Devices showing LAN = no either don't support LAN control or have it switched off.")
        print("Check each one in the Govee Home app: device -> settings (gear) -> LAN Control.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
