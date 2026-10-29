"""Read and normalize agent inventory CSV records."""

import csv
from dataclasses import dataclass, field
from pathlib import Path


IP_FIELD = "local_metadata.host.ip"
HOST_FIELD = "local_metadata.host.name"
OS_FIELD = "local_metadata.os.full"


@dataclass
class Node:
    hostname: str
    addresses: list[str] = field(default_factory=list)
    operating_systems: list[str] = field(default_factory=list)


def read_nodes(source: Path) -> list[Node]:
    """Merge repeated hostnames, preserving all distinct IP and OS values."""
    nodes: dict[str, Node] = {}
    with source.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        required = {IP_FIELD, HOST_FIELD, OS_FIELD}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing required fields: {', '.join(sorted(missing))}")
        for row_number, row in enumerate(reader, start=2):
            hostname = (row.get(HOST_FIELD) or "").strip()
            addresses = (row.get(IP_FIELD) or "").strip()
            operating_system = (row.get(OS_FIELD) or "").strip()
            if not any((hostname, addresses, operating_system)):
                continue
            # Unnamed rows must not all collapse into a single node.
            key = f"host:{hostname.casefold()}" if hostname else f"row:{row_number}"
            node = nodes.setdefault(key, Node(hostname or f"Unnamed host (row {row_number})"))
            for address in addresses.split(","):
                address = address.strip()
                if address and address not in node.addresses:
                    node.addresses.append(address)
            if operating_system and operating_system not in node.operating_systems:
                node.operating_systems.append(operating_system)
    return list(nodes.values())
