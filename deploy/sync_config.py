"""Standalone offline Syncthing pair configuration; never starts a process/API.

Run only while this dedicated Syncthing instance is stopped. Reads its generated
XML privately, preserves keys, and prints only the folder ID and network policy.
The same helper works on Windows and Linux; provide that host's local/peer IDs.
"""
import argparse
import copy
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET

FOLDER = "obsidian-tasks"
IGNORE = "!/Задачи.md\n**\n"
DEVICE = re.compile(r"^[A-Z2-7]{7}(?:-[A-Z2-7]{7}){7}$")


def set_child(parent, name, value):
    node = parent.find(name)
    if node is None:
        node = ET.SubElement(parent, name)
    node.text = str(value).lower() if isinstance(value, bool) else str(value)


def configure(home, folder_path, local_ip, peer_ip, local_id, peer_id):
    for value in (local_ip, peer_ip):
        ip = ipaddress.ip_address(value)
        if ip.version != 4 or ip not in ipaddress.ip_network("100.64.0.0/10"):
            raise ValueError("Tailnet IPv4 required")
    if not DEVICE.fullmatch(local_id) or not DEVICE.fullmatch(peer_id) or local_id == peer_id:
        raise ValueError("Distinct device IDs required")
    home, folder_path = Path(home), Path(folder_path)
    config = home / "config.xml"
    tree = ET.parse(config)
    root = tree.getroot()
    if any(item.get("id") not in (FOLDER, "default") for item in root.findall("folder")):
        raise ValueError("Unexpected folder in this dedicated home")
    if any(item.get("id") not in (local_id, peer_id) for item in root.findall("device")):
        raise ValueError("Unexpected device in this dedicated home")
    if not any(item.get("id") == local_id for item in root.findall("device")):
        raise ValueError("Generated local device ID does not match")
    # `generate` may include a default folder; remove its configuration only,
    # never its on-disk contents, in this explicitly dedicated fresh home.
    for item in list(root.findall("folder")):
        if item.get("id") == "default":
            root.remove(item)
    folder_path.mkdir(parents=True, exist_ok=True)
    ignore = folder_path / ".stignore"
    if ignore.exists() and ignore.read_text(encoding="utf-8-sig") != IGNORE:
        raise ValueError("Existing ignore policy differs")
    options = root.find("options")
    if options is None:
        options = ET.SubElement(root, "options")
    for item in list(options.findall("listenAddress")):
        options.remove(item)
    set_child(options, "listenAddress", f"tcp://{local_ip}:22000")
    for key, value in {"globalAnnounceEnabled": False, "localAnnounceEnabled": False,
                       "relaysEnabled": False, "natEnabled": False, "startBrowser": False,
                       "autoUpgradeIntervalH": 0, "crashReportingEnabled": False,
                       "urAccepted": -1}.items():
        set_child(options, key, value)
    gui = root.find("gui")
    if gui is None:
        gui = ET.SubElement(root, "gui")
    gui.set("enabled", "true")
    set_child(gui, "address", "127.0.0.1:8384")
    peer = next((item for item in root.findall("device") if item.get("id") == peer_id), None)
    if peer is None:
        template = root.find("defaults/device")
        peer = copy.deepcopy(template) if template is not None else ET.Element("device")
        root.append(peer)
    peer.set("id", peer_id)
    peer.set("name", "Obsidian task peer")
    peer.set("introducer", "false")
    for node in list(peer.findall("address")):
        peer.remove(node)
    set_child(peer, "address", f"tcp://{peer_ip}:22000")
    set_child(peer, "paused", False)
    set_child(peer, "autoAcceptFolders", False)
    folder = next((item for item in root.findall("folder") if item.get("id") == FOLDER), None)
    if folder is None:
        template = root.find("defaults/folder")
        folder = copy.deepcopy(template) if template is not None else ET.Element("folder")
        root.insert(0, folder)
    folder.attrib.update({"id": FOLDER, "label": "Obsidian — задачи", "path": str(folder_path),
                          "type": "sendreceive", "rescanIntervalS": "60", "fsWatcherEnabled": "true",
                          "fsWatcherDelayS": "1", "ignorePerms": "true"})
    for node in list(folder.findall("device")):
        folder.remove(node)
    ET.SubElement(folder, "device", {"id": local_id})
    ET.SubElement(folder, "device", {"id": peer_id})
    set_child(folder, "maxConflicts", -1)
    set_child(folder, "paused", False)
    set_child(folder, "junctionsAsDirs", False)
    # Save the policy before exposing a configured folder to its first scan.
    ignore.write_text(IGNORE, encoding="utf-8")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    shutil.copy2(config, home / ("config.before-pair-" + stamp + ".xml"))
    temp = home / "config.prepared.xml"
    tree.write(temp, encoding="utf-8", xml_declaration=True)
    os.replace(temp, config)
    return {"status": "configured", "folder_id": FOLDER, "folder_type": "sendreceive",
            "peer_devices": 1, "listen_address": f"tcp://{local_ip}:22000",
            "gui_address": "127.0.0.1:8384", "discovery": False, "relays": False,
            "nat": False, "max_conflicts": -1, "ignore_policy": "only_tasks"}


def main():
    parser = argparse.ArgumentParser()
    for name in ("home", "folder-path", "local-ip", "peer-ip", "local-id", "peer-id"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        result = configure(args.home, args.folder_path, args.local_ip, args.peer_ip, args.local_id, args.peer_id)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (OSError, ValueError, ET.ParseError):
        print('{"status":"failed","error":"configuration_validation_failed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
