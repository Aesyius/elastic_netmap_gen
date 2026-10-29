"""Export the agent inventory as an uncompressed diagrams.net diagram."""

import csv
import ipaddress
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


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


@dataclass
class _Box:
    label: str
    is_host: bool = False
    heading: str = ""
    details: str = ""
    children: list["_Box"] = field(default_factory=list)
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


_PRIVATE_RANGES = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _placement(node: Node) -> tuple[str, _Network | None]:
    """Prefer the first RFC 1918 interface; local-only hosts remain unassigned."""
    candidates = []
    for address in node.addresses:
        try:
            interface = ipaddress.ip_interface(address)
        except ValueError:
            continue
        ip = interface.ip
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            continue
        candidates.append(interface)
    for interface in candidates:
        if interface.version == 4:
            for private_range in _PRIVATE_RANGES:
                if interface.ip in private_range and interface.network.subnet_of(private_range):
                    return str(private_range), interface.network
    if candidates:
        interface = candidates[0]
        return f"Other IPv{interface.version}", interface.network
    return "Unassigned / local-only addresses", None


def _host_box(node: Node) -> _Box:
    values = ["IP addresses:"]
    values.extend(node.addresses or ["(not provided)"])
    values.append("OS:")
    values.extend(node.operating_systems or ["(not provided)"])
    lines = [line for value in values for line in textwrap.wrap(value, width=40)]
    heading_lines = textwrap.wrap(node.hostname, width=36)
    heading = "\n".join(heading_lines)
    details = "\n".join(lines)
    heading_height = max(28, 18 * len(heading_lines))
    return _Box(heading + "\n" + details, is_host=True, heading=heading,
                details=details, width=320,
                height=heading_height + 116 + 16 * len(lines))


def _hierarchy(nodes: list[Node]) -> _Box:
    inventory = _Box("LOGICAL NETWORK TOPOLOGY — illustrative layout, not discovered connectivity")
    groups: dict[str, dict[_Network | None, list[Node]]] = {}
    for node in nodes:
        group, network = _placement(node)
        groups.setdefault(group, {}).setdefault(network, []).append(node)

    # Address ranges retain a stable order; subnets sort numerically, not lexically.
    order = [str(network) for network in _PRIVATE_RANGES]
    order.extend(["Other IPv4", "Other IPv6", "Unassigned / local-only addresses"])
    for name in order:
        if name not in groups:
            continue
        group_box = _Box(name)
        inventory.children.append(group_box)
        networks = groups[name]
        boxes: dict[_Network, _Box] = {}
        if name in {str(network) for network in _PRIVATE_RANGES}:
            boxes[ipaddress.ip_network(name)] = group_box
        for network in sorted((net for net in networks if net is not None),
                              key=lambda net: (net.prefixlen, int(net.network_address))):
            if network in boxes:
                subnet_box = boxes[network]
            else:
                subnet_box = _Box(str(network))
                containers = [net for net in boxes if network.subnet_of(net)]
                parent = boxes[max(containers, key=lambda net: net.prefixlen)] if containers else group_box
                parent.children.append(subnet_box)
                boxes[network] = subnet_box
            subnet_box.children.extend(_host_box(node) for node in networks[network])
        group_box.children.extend(_host_box(node) for node in networks.get(None, []))
    return inventory


def _layout(box: _Box, *, top: bool = False) -> None:
    if box.is_host:
        return
    # Wider banks and side-by-side address ranges give a landscape canvas.
    columns = min(3, len(box.children)) if top else (
        6 if all(child.is_host for child in box.children) else 2)
    columns = max(1, columns)
    for child in box.children:
        _layout(child)
    column_widths = [0] * min(columns, len(box.children))
    for index, child in enumerate(box.children):
        column_widths[index % columns] = max(column_widths[index % columns], child.width)
    # Reserve the left rail for infrastructure; descendants sit to its right.
    y = 90
    for offset in range(0, len(box.children), columns):
        row = box.children[offset:offset + columns]
        x = 300
        for column, child in enumerate(row):
            child.x, child.y = x, y
            x += column_widths[column] + 30
        y += max(child.height for child in row) + 70
    box.width = max(1000 if top else 640,
                    300 + sum(column_widths) + 30 * len(column_widths))
    box.height = max(380, y)


def _device_image(kind: str) -> str:
    """Self-contained SVG icons; no external images or stencil libraries required."""
    drawings = {
        "router": (
            '<ellipse cx="48" cy="47" rx="43" ry="18" fill="#17668f"/>'
            '<path d="M5 29v18c0 24 86 24 86 0V29" fill="#237fa8" stroke="#144e70" stroke-width="2"/>'
            '<ellipse cx="48" cy="29" rx="43" ry="19" fill="#49a7ce" stroke="#144e70" stroke-width="2"/>'
            '<path d="M22 25h19l-5-5m5 5-5 5M74 33H55l5-5m-5 5 5 5'
            'M45 14v12l-5-4m5 4 5-4M51 44V32l-5 4m5-4 5 4" '
            'fill="none" stroke="white" stroke-width="3" stroke-linejoin="round"/>'
        ),
        "switch": (
            '<path d="m5 29 15-14h62l9 14v26H5Z" fill="#348fb8" stroke="#144e70" stroke-width="2"/>'
            '<path d="M5 29h86v26H5Z" fill="#17668f"/>'
            + ''.join(f'<rect x="{x}" y="35" width="7" height="8" rx="1" fill="#d6edf5"/>'
                      f'<circle cx="{x + 3}" cy="49" r="1.5" fill="#4ade80"/>'
                      for x in range(12, 78, 11)) +
            '<path d="M26 23h18m-4-4 4 4-4 4m30-4H52m4-4-4 4 4 4" '
            'fill="none" stroke="white" stroke-width="2"/>'
        ),
        "pc": (
            '<rect x="10" y="5" width="68" height="46" rx="4" fill="#6b8396" stroke="#334b60" stroke-width="2"/>'
            '<rect x="16" y="11" width="56" height="32" rx="1" fill="#b9e4f5"/>'
            '<path d="M19 40 68 14v26Z" fill="#8cc8e6"/>'
            '<path d="M39 51v7H25l-6 6h51l-6-6H50v-7" fill="#6b8396" stroke="#334b60" stroke-width="2"/>'
            '<rect x="82" y="20" width="11" height="44" rx="2" fill="#8ca2b1" stroke="#334b60"/>'
            '<circle cx="87" cy="56" r="2" fill="#22c55e"/>'
        ),
        "server": (
            '<rect x="25" y="3" width="46" height="63" rx="4" fill="#627d91" stroke="#334b60" stroke-width="2"/>'
            + ''.join(f'<rect x="31" y="{y}" width="34" height="12" rx="2" fill="#d2e2eb"/>'
                      f'<circle cx="59" cy="{y + 6}" r="2" fill="#22c55e"/>'
                      f'<path d="M35 {y + 5}h15" stroke="#627d91" stroke-width="2"/>'
                      for y in (9, 26, 43))
        ),
    }
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="96" height="72" viewBox="0 0 96 72">'
           + drawings[kind] + '</svg>')
    return "data:image/svg+xml," + quote(svg, safe="")


def write_drawio(nodes: list[Node], destination: Path) -> None:
    """Render Packet Tracer-inspired devices and explicitly illustrative links."""
    document = ET.Element("mxfile", host="app.diagrams.net", type="device")
    diagram = ET.SubElement(document, "diagram", id="inventory", name="Agent inventory")
    model = ET.SubElement(diagram, "mxGraphModel", grid="1", gridSize="10", page="0",
                         background="#ffffff", tooltips="1", connect="1", arrows="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    inventory = _hierarchy(nodes)
    _layout(inventory, top=True)
    inventory.x = inventory.y = 40
    counter = 0

    def geometry(cell: ET.Element, x: int, y: int, width: int, height: int) -> None:
        ET.SubElement(cell, "mxGeometry", {
            "x": str(x), "y": str(y), "width": str(width),
            "height": str(height), "as": "geometry",
        })

    def icon(identifier: str, parent: str, kind: str, x: int, y: int) -> None:
        cell = ET.SubElement(root, "mxCell", id=identifier, parent=parent, vertex="1",
                             value="", style=("shape=image;imageAspect=1;aspect=fixed;"
                                              f"image={_device_image(kind)};"))
        geometry(cell, x, y, 96, 72)

    def render(box: _Box, parent: str, depth: int) -> str:
        nonlocal counter
        counter += 1
        identifier = f"{'host' if box.is_host else 'group'}-{counter}"
        if box.is_host:
            style = ("rounded=0;whiteSpace=wrap;html=0;noLabel=1;"
                     "fontFamily=Helvetica;fillColor=none;strokeColor=none;container=1;")
        else:
            fill = ("#ffffff", "#edf5fc", "#f7fafc")[min(depth, 2)]
            style = ("swimlane;rounded=1;arcSize=12;container=1;collapsible=0;"
                     "recursiveResize=0;startSize=40;horizontal=1;whiteSpace=wrap;"
                     "html=0;fontSize=15;fontStyle=1;fontFamily=Helvetica;align=left;"
                     f"spacingLeft=16;fillColor={fill};swimlaneFillColor={fill};"
                     "strokeColor=#9db5c7;dashed=1;")
        cell = ET.SubElement(root, "mxCell", id=identifier, value=box.label,
                             vertex="1", parent=parent, style=style)
        geometry(cell, box.x, box.y, box.width, box.height)
        device_id = f"device-{counter}"
        if box.is_host:
            kind = "server" if "server" in box.label.casefold() else "pc"
            heading_height = max(28, 18 * len(box.heading.splitlines()))
            heading = ET.SubElement(root, "mxCell", id=f"hostname-{counter}",
                                    vertex="1", parent=identifier, value=box.heading,
                                    style=("text;html=0;align=center;verticalAlign=top;"
                                           "fontSize=13;fontStyle=1;fontFamily=Helvetica;"
                                           "whiteSpace=wrap;strokeColor=none;fillColor=none;"))
            geometry(heading, 8, 8, box.width - 16, heading_height)
            icon_y = heading_height + 16
            icon(device_id, identifier, kind, (box.width - 96) // 2, icon_y)
            details_y = icon_y + 84
            details = ET.SubElement(root, "mxCell", id=f"details-{counter}",
                                    vertex="1", parent=identifier, value=box.details,
                                    style=("text;html=0;align=center;verticalAlign=top;"
                                           "fontSize=11;fontFamily=Helvetica;whiteSpace=wrap;"
                                           "strokeColor=none;fillColor=none;"))
            geometry(details, 8, details_y, box.width - 16, box.height - details_y - 8)
            return device_id

        kind = "router" if depth < 2 else "switch"
        icon(device_id, identifier, kind, 72, 100)
        caption = "Core router" if depth == 0 else ("Distribution router" if depth == 1 else "Access switch")
        label = ET.SubElement(root, "mxCell", id=f"caption-{counter}", vertex="1", parent=identifier,
                              value=caption + " (illustrative)",
                              style="text;html=0;align=center;fontSize=12;fontStyle=1;strokeColor=none;fillColor=none;")
        geometry(label, 20, 180, 240, 24)
        if depth == 0:
            legend = ET.SubElement(root, "mxCell", id="topology-legend", vertex="1", parent=identifier,
                                   value="VISUAL ONLY | Devices and cables are illustrative, not discovered.\nHosts retain all IPs; placement uses the first eligible private subnet.",
                                   style="text;html=0;align=center;fontSize=12;fontColor=#526879;whiteSpace=wrap;")
            geometry(legend, 20, 230, 240, 120)
        for child in box.children:
            target = render(child, identifier, depth + 1)
            edge = ET.SubElement(root, "mxCell", id=f"cable-{target}", parent=identifier,
                                 edge="1", source=device_id, target=target,
                                 style=("edgeStyle=orthogonalEdgeStyle;rounded=0;html=0;"
                                        "endArrow=none;startArrow=none;strokeColor=#334155;strokeWidth=1.5;"
                                        "exitX=1;exitY=0.5;entryX=0;entryY=0.5;"))
            edge_geometry = ET.SubElement(edge, "mxGeometry", {"relative": "1", "as": "geometry"})
            points = ET.SubElement(edge_geometry, "Array", {"as": "points"})
            # Left-to-right cables use the header/column gutters, clear of labels.
            target_y = child.y + (max(28, 18 * len(child.heading.splitlines())) + 52
                                  if child.is_host else 136)
            for x, y in ((250, 136), (250, child.y - 24),
                         (child.x - 20, child.y - 24), (child.x - 20, target_y)):
                ET.SubElement(points, "mxPoint", x=str(x), y=str(y))
        return device_id

    render(inventory, "1", 0)

    ET.indent(document)
    ET.ElementTree(document).write(destination, encoding="utf-8", xml_declaration=True)


def export_drawio(source: Path, destination: Path) -> int:
    """Export the CSV and return the number of nodes written."""
    if source.resolve() == destination.resolve():
        raise ValueError("Input CSV and output diagram must be different files")
    nodes = read_nodes(source)
    if not nodes:
        raise ValueError("CSV contains no host records")
    write_drawio(nodes, destination)
    return len(nodes)
