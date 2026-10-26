"""Govee Desktop device inventory.

Uses the GoveeAPI.dll that ships with Govee Desktop (Settings -> API) to list
every device on your Govee account, then writes the list to
govee_desktop_devices.csv and govee_desktop_devices.json next to this script.

Before running:
  1. Install the one dependency:      python -m pip install pythonnet
  2. Start Govee Desktop as administrator (right-click -> Run as administrator).
  3. In Govee Desktop: Settings -> API -> Enable API (already on).
  4. Close Scenic DreamView and Razer DreamView if either is running.

Run from a Command Prompt (also "Run as administrator" if the first try fails):

    python govee_desktop_inventory.py

Options:
    --dll PATH       Path to GoveeAPI.dll if the script can't find it.
    --test "NAME"    After listing, run a short light test on one device
                     (exact name from the list): red, green, blue, segment
                     rainbow, then back to warm white at 100%.

It asks for the API GUID shown on the Settings -> API screen (typing is
hidden). You can also set GOVEE_DESKTOP_GUID instead. The GUID is never
written to disk or printed.
"""

import argparse
import csv
import getpass
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

ERRORS = {
    1: "Govee Desktop is not running",
    100: "program running error",
    101: "initialization failure",
    102: "device is offline",
    1001: "API GUID error (check the GUID on Settings -> API)",
    1002: "parameter is empty",
    1003: "device does not exist (name must match exactly)",
    1010: "color value error",
    1011: "invalid brightness",
    4000: "no response; watch the light, it may still have changed",
    4001: "failed to send",
}


def describe(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    return "OK" if code == 0 else f"{code} ({ERRORS.get(code, 'unknown error')})"


def find_dll(explicit):
    if explicit:
        return explicit if os.path.isfile(explicit) else None

    candidates = []
    # Folder of the running Govee Desktop process, if we can see it.
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.Path -like '*Govee*' } | "
             "Select-Object -ExpandProperty Path -Unique"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        for exe in out.splitlines():
            exe = exe.strip()
            if exe:
                candidates.append(os.path.dirname(exe))
    except (OSError, subprocess.SubprocessError):
        pass

    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env)
        if base:
            candidates += glob.glob(os.path.join(base, "*Govee*"))
            candidates += glob.glob(os.path.join(base, "*", "*Govee*"))
            candidates += glob.glob(os.path.join(base, "Programs", "*Govee*"))

    for folder in candidates:
        for pattern in ("GoveeAPI.dll", os.path.join("GoveeAPI", "GoveeAPI.dll"),
                        os.path.join("**", "GoveeAPI.dll")):
            hits = glob.glob(os.path.join(folder, pattern), recursive=True)
            if hits:
                return hits[0]
    return None


def load_runtime(dll_path):
    """Pick .NET Framework or .NET (Core) to match how the DLL was built."""
    with open(dll_path, "rb") as f:
        blob = f.read()
    try:
        import pythonnet
    except ImportError:
        print("pythonnet is not installed. Run:  python -m pip install pythonnet")
        sys.exit(2)

    if b".NETCoreApp" in blob:
        install_dir = os.path.dirname(os.path.dirname(dll_path))
        configs = (glob.glob(os.path.join(os.path.dirname(dll_path), "*.runtimeconfig.json"))
                   + glob.glob(os.path.join(install_dir, "*.runtimeconfig.json")))
        print(f".NET runtime: coreclr ({'config ' + configs[0] if configs else 'no runtimeconfig found'})")
        if configs:
            pythonnet.load("coreclr", runtime_config=configs[0])
        else:
            pythonnet.load("coreclr")
    else:
        print(".NET runtime: .NET Framework")
        pythonnet.load("netfx")


def connect(dll_path, guid):
    load_runtime(dll_path)
    import clr  # noqa: F401  (provided by pythonnet)

    sys.path.append(os.path.dirname(dll_path))
    try:
        clr.AddReference(os.path.splitext(dll_path)[0])
    except Exception:
        clr.AddReference("GoveeAPI")
    from GoveeAPI import ConnectGovee  # type: ignore

    api = ConnectGovee()

    # The guide lists the GoveeAPI methods with slightly different names in
    # different places, so print what this DLL actually exposes.
    methods = sorted({m.Name for m in api.GetType().GetMethods()
                      if m.DeclaringType.Name == "ConnectGovee"})
    print("GoveeAPI methods:", ", ".join(methods))

    try:
        result = api.InitConnect(guid)
    except TypeError:
        from System import Guid  # type: ignore
        result = api.InitConnect(Guid(guid))
    print("InitConnect:", describe(result))
    if str(result) != "0":
        sys.exit(3)
    return api, methods


def parse_devices(raw):
    data = json.loads(str(raw))
    if isinstance(data, dict):
        for key in ("data", "devices", "Devices", "Data"):
            if isinstance(data.get(key), list):
                return data[key], data
        return [data], data
    return data, data


def run_test(api, name, segments):
    def step(label, fn):
        print(f"  {label:28} {describe(fn())}")
        time.sleep(1.5)

    print(f"\nLight test on '{name}':")
    step("on", lambda: api.DeviceSwitchControl(name, 1))
    step("brightness 100", lambda: api.DeviceBrightnessControl(name, 100))
    step("red", lambda: api.DeviceColorControl(name, 255, 0, 0))
    step("green", lambda: api.DeviceColorControl(name, 0, 255, 0))
    step("blue", lambda: api.DeviceColorControl(name, 0, 0, 255))
    if segments:
        rainbow = ["#FF0000", "#FF7F00", "#FFFF00", "#00FF00", "#0000FF", "#8B00FF"]
        colors = ",".join(rainbow[i % len(rainbow)] for i in range(segments))
        step(f"segment rainbow ({segments})", lambda: api.DeviceSegmentsColor(name, colors, 0))
    step("warm white", lambda: api.DeviceColorControl(name, 255, 180, 107))


def main():
    parser = argparse.ArgumentParser(description="List Govee devices through Govee Desktop.")
    parser.add_argument("--dll", help="path to GoveeAPI.dll")
    parser.add_argument("--test", metavar="NAME", help="run a short light test on this device")
    args = parser.parse_args()

    dll = find_dll(args.dll)
    if not dll:
        print("Could not find GoveeAPI.dll. Find it in the Govee Desktop install folder")
        print('(usually a "GoveeAPI" subfolder) and run again with --dll "full\\path\\GoveeAPI.dll".')
        return 2
    print("Using:", dll)

    guid = os.environ.get("GOVEE_DESKTOP_GUID", "").strip()
    if not guid:
        guid = getpass.getpass("Govee Desktop API GUID: ").strip()
    if not guid:
        print("No GUID entered.")
        return 2

    api, methods = connect(dll, guid)

    raw = api.GetDeviceBaseInfo()
    try:
        devices, full = parse_devices(raw)
    except ValueError:
        print("GetDeviceBaseInfo did not return JSON. Raw result:")
        print(raw)
        return 4

    rows = []
    for d in devices:
        rows.append({
            "name": d.get("Name", ""),
            "type": d.get("SkuType", ""),
            "segments": d.get("SegmentNums", ""),
            "lan_on": {1: "yes", 0: "no"}.get(d.get("IsLANOn"), d.get("IsLANOn", "")),
            "other_fields": json.dumps({k: v for k, v in d.items()
                                        if k not in ("Name", "SkuType", "SegmentNums", "IsLANOn")}),
        })
    rows.sort(key=lambda r: (str(r["type"]), str(r["name"])))

    csv_path = os.path.join(HERE, "govee_desktop_devices.csv")
    json_path = os.path.join(HERE, "govee_desktop_devices.json")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["name"])
        writer.writeheader()
        writer.writerows(rows)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"methods": methods, "raw": full}, f, indent=2)

    print()
    print(f"{'NAME':34} {'TYPE':18} {'SEGMENTS':9} LAN")
    for r in rows:
        print(f"{str(r['name'])[:34]:34} {str(r['type'])[:18]:18} {str(r['segments']):9} {r['lan_on']}")
    print()
    lan_on = sum(1 for r in rows if r["lan_on"] == "yes")
    print(f"{len(rows)} devices, {lan_on} with LAN on.")
    print(f"Saved: {csv_path}")
    print(f"       {json_path}")
    if lan_on < len(rows):
        print("Devices with LAN = no can't be controlled here until LAN Control is switched on")
        print("in the Govee Home app (device -> settings gear -> LAN Control), if the model has it.")

    if args.test:
        match = next((r for r in rows if r["name"] == args.test), None)
        if not match:
            print(f"\nNo device named exactly '{args.test}'. Copy the name from the list above.")
            return 1
        try:
            segs = int(match["segments"] or 0)
        except ValueError:
            segs = 0
        run_test(api, args.test, segs)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
