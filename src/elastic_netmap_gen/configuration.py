"""Load VLAN declarations and device annotations without changing inventory."""

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path

from .vlan import DEFAULT_RULES, VlanRule


@dataclass(frozen=True)
class DeviceSettings:
    device_type: str | None = None
    kct: bool = False
    vlans: tuple[int, ...] = ()
    internet: bool = False
    server_type: str | None = None
    notes: str = ""


@dataclass
class Configuration:
    vlan_rules: tuple[VlanRule, ...] = DEFAULT_RULES
    devices: dict[str, DeviceSettings] = field(default_factory=dict)
    connections: tuple[tuple[str, str], ...] = ()


def _object(value: object, allowed: set[str], context: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be a JSON object")
    unknown = value.keys() - allowed
    if unknown:
        raise ValueError(f"Unknown {context} fields: {', '.join(sorted(unknown))}")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def load_configuration(path: Path) -> Configuration:
    with path.open(encoding="utf-8-sig") as stream:
        data = json.load(stream, object_pairs_hook=_unique_object)
    data = _object(data, {"vlans", "devices", "connections"}, "configuration")
    config = Configuration()
    if "vlans" in data:
        if not isinstance(data["vlans"], list):
            raise ValueError("vlans must be a list")
        rules = {}
        for entry in data["vlans"]:
            entry = _object(entry, {"range", "vlan_id"}, "VLAN declaration")
            if not isinstance(entry.get("range"), str):
                raise ValueError("Each VLAN declaration requires an IPv4 CIDR range")
            if type(entry.get("vlan_id")) is not int:
                raise ValueError("Each VLAN declaration requires an integer vlan_id")
            rule = VlanRule(ipaddress.IPv4Network(entry["range"]), entry["vlan_id"])
            if rule.network in rules:
                raise ValueError(f"Duplicate VLAN range: {rule.network}")
            rules[rule.network] = rule
        config.vlan_rules = tuple(rules.values())
    devices = data.get("devices", {})
    if not isinstance(devices, dict):
        raise ValueError("devices must be an object keyed by hostname")
    for hostname, settings in devices.items():
        key = hostname.strip().casefold()
        if not key or key in config.devices:
            raise ValueError(f"Empty or duplicate device hostname: {hostname!r}")
        settings = _object(settings, {"type", "kct", "vlans", "internet", "server_type", "notes"}, f"device {hostname}")
        kind = settings.get("type")
        if "type" in settings and kind not in ("router", "firewall", "switch", "server", "pc"):
            raise ValueError(f"Invalid device type for {hostname}; use router, firewall, switch, server, or pc")
        server_type = settings.get("server_type")
        if "server_type" in settings:
            if kind != "server" or not isinstance(server_type, str) or not server_type.strip():
                raise ValueError(f"server_type requires type server and a non-empty string for {hostname}")
            server_type = server_type.strip()
        kct = settings.get("kct", False)
        if type(kct) is not bool:
            raise ValueError(f"kct must be true or false for {hostname}")
        vlans = settings.get("vlans", [])
        if not isinstance(vlans, list) or any(type(value) is not int or not 1 <= value <= 4094 for value in vlans):
            raise ValueError(f"vlans must be a list of VLAN IDs from 1 to 4094 for {hostname}")
        if len(set(vlans)) != len(vlans):
            raise ValueError(f"Duplicate VLAN connections for {hostname}")
        internet = settings.get("internet", False)
        if type(internet) is not bool:
            raise ValueError(f"internet must be true or false for {hostname}")
        if (vlans or internet) and kind not in ("router", "firewall"):
            raise ValueError(f"VLAN and Internet connections require router or firewall type for {hostname}")
        notes = settings.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError(f"notes must be a string for {hostname}")
        config.devices[key] = DeviceSettings(kind, kct, tuple(vlans), internet, server_type, notes.strip())
    connections = data.get("connections", [])
    if not isinstance(connections, list):
        raise ValueError("connections must be a list")
    links = []
    for connection in connections:
        connection = _object(connection, {"from", "to"}, "connection")
        endpoints = []
        for key in ("from", "to"):
            value = connection.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Connection {key} must be a non-empty hostname or internet")
            endpoints.append(value.strip().casefold())
        source, target = endpoints
        if source == target:
            raise ValueError("A connection cannot link a device to itself")
        if target == "internet":
            raise ValueError("internet must be the source of a connection")
        if (source, target) in links:
            raise ValueError(f"Duplicate connection: {source} -> {target}")
        links.append((source, target))
    # Acyclic declared paths provide a deterministic upstream-to-downstream order.
    pending = dict.fromkeys(endpoint for link in links for endpoint in link)
    while pending:
        ready = [name for name in pending if not any(target == name and source in pending for source, target in links)]
        if not ready:
            raise ValueError("connections must not contain a directed cycle")
        for name in ready:
            del pending[name]
    config.connections = tuple(links)
    return config
