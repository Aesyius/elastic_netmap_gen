"""Export the agent inventory as an uncompressed diagrams.net diagram."""

import ipaddress
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote


from .inventory import Node, read_nodes
from .configuration import DeviceSettings
from .vlan import DEFAULT_RULES, VlanRule, vlan_for
from .topology import Link, Vertex, radial_layout, route_links


@dataclass
class _Box:
    label: str
    is_host: bool = False
    heading: str = ""
    addresses: str = ""
    details: str = ""
    notes: str = ""
    device_type: str | None = None
    kct: bool = False
    hostname: str = ""
    vlan_id: int | None = None
    children: list["_Box"] = field(default_factory=list)
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0


_PRIVATE_RANGES = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))
_Interface = ipaddress.IPv4Interface | ipaddress.IPv6Interface
_HOST_WIDTH = 320
_NOTES_WIDTH = 280
_NOTES_GAP = 20


def _placement(node: Node) -> tuple[str, _Interface | None]:
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
                    return str(private_range), interface
    if candidates:
        interface = candidates[0]
        return f"Other IPv{interface.version}", interface
    return "Unassigned / local-only addresses", None


def _host_box(node: Node, settings: DeviceSettings = DeviceSettings()) -> _Box:
    addresses = "\n".join(line for value in (node.addresses or ["(not provided)"])
                          for line in textwrap.wrap(value, width=40))
    values = []
    values.append("OS:")
    values.extend(node.operating_systems or ["(not provided)"])
    if settings.device_type:
        values.append(f"{settings.device_type.title()} (configured)")
    if settings.server_type:
        values.append(f"Server type: {settings.server_type} (configured)")
    if settings.kct:
        values.append("Key Cyber Terrain (KCT)")
    lines = [line for value in values for line in textwrap.wrap(value, width=40)]
    heading_lines = textwrap.wrap(node.hostname, width=36)
    heading = "\n".join(heading_lines)
    details = "\n".join(lines)
    heading_height = max(18, 18 * len(heading_lines))
    notes = "\n".join(line for paragraph in settings.notes.splitlines()
                      for line in (textwrap.wrap(paragraph, width=38) or [""]))
    if notes:
        notes = "Notes:\n" + notes
    height = heading_height + 16 * len(addresses.splitlines()) + 116 + 16 * len(lines)
    return _Box(heading + "\n" + addresses + "\n" + details, is_host=True, heading=heading,
                 addresses=addresses,
                 device_type=settings.device_type, kct=settings.kct, hostname=node.hostname.casefold(),
                 details=details, notes=notes,
                 width=_HOST_WIDTH + (_NOTES_GAP + _NOTES_WIDTH + 8 if notes else 0),
                 height=max(height, 16 * len(notes.splitlines()) + 32 if notes else 0))


def _hierarchy(nodes: list[Node], rules: tuple[VlanRule, ...] = DEFAULT_RULES,
               devices: dict[str, DeviceSettings] | None = None,
               connections: tuple[tuple[str, str], ...] = ()) -> _Box:
    devices = devices or {}
    inventory = _Box("CONFIGURED NETWORK TOPOLOGY")
    groups: dict[int | str, _Box] = {}
    inline = {name for link in connections for name in link if name != "internet"}
    for node in nodes:
        settings = devices.get(node.hostname.casefold(), DeviceSettings())
        host = _host_box(node, settings)
        if settings.device_type in ("router", "firewall") or node.hostname.casefold() in inline:
            inventory.children.append(host)
            continue
        group, interface = _placement(node)
        identifier = vlan_for(interface.ip, rules) if interface is not None else None
        key = identifier if identifier is not None else (
            "Unknown VLAN" if group in {str(net) for net in _PRIVATE_RANGES} else group)
        if key not in groups:
            groups[key] = _Box(f"VLAN {identifier} (configured/inferred)" if identifier is not None else str(key),
                               vlan_id=identifier)
        groups[key].children.append(host)
    # Explicitly connected VLANs can be shown even when they have no inventory hosts.
    for settings in devices.values():
        for identifier in settings.vlans:
            groups.setdefault(identifier, _Box(f"VLAN {identifier} (configured)", vlan_id=identifier))
    pending = {child.hostname: child for child in inventory.children}
    inventory.children = []
    while pending:
        ready = [name for name in pending if not any(target == name and source in pending for source, target in connections)]
        if not ready:
            raise ValueError("connections must not contain a directed cycle")
        for name in ready:
            inventory.children.append(pending.pop(name))
    inventory.children.extend(groups[key] for key in sorted(groups, key=lambda key: (isinstance(key, str), key)))
    return inventory


def _layout(box: _Box, *, top: bool = False) -> None:
    if box.is_host:
        return
    if top:
        _topology_layout(box, {}, ())
        return
    columns = 6
    for child in box.children:
        _layout(child)
    column_widths = [0] * min(columns, len(box.children))
    for index, child in enumerate(box.children):
        column_widths[index % columns] = max(column_widths[index % columns], child.width)
    # Host cards share a compact grid inside each VLAN container.
    y = 60
    for offset in range(0, len(box.children), columns):
        row = box.children[offset:offset + columns]
        x = 30
        for column, child in enumerate(row):
            child.x, child.y = x, y
            x += column_widths[column] + 30
        y += max(child.height for child in row) + 70
    box.width = max(640, 30 + sum(column_widths) + 30 * len(column_widths))
    box.height = max(380, y)


def _box_key(box: _Box) -> str:
    return "host:" + box.hostname if box.is_host else "group:" + box.label


def _topology_layout(inventory: _Box, devices: dict[str, DeviceSettings],
                     connections: tuple[tuple[str, str], ...]) -> tuple[dict[str, Vertex], list[Link]]:
    vertices = {}
    vlans = {}
    for child in inventory.children:
        _layout(child)
        key = _box_key(child)
        vertices[key] = Vertex(key, child.width, child.height, child.device_type or "group")
        if child.vlan_id is not None:
            vlans[child.vlan_id] = key
    links: dict[tuple[str, str], Link] = {}
    for source, target in connections:
        source = "internet" if source == "internet" else "host:" + source
        target = "host:" + target
        links[source, target] = Link(source, target, True)
    for hostname, settings in devices.items():
        for vlan in settings.vlans:
            source, target = "host:" + hostname, vlans[vlan]
            links.setdefault((source, target), Link(source, target))
        if settings.internet:
            source, target = "internet", "host:" + hostname
            links.setdefault((source, target), Link(source, target))
    if any(source == "internet" for source, _ in links):
        vertices["internet"] = Vertex("internet", 200, 120, "cloud")
    ordered_links = sorted(links.values(), key=lambda link: (link.source, link.target))
    inventory.width, inventory.height = radial_layout(vertices, ordered_links)
    for child in inventory.children:
        vertex = vertices[_box_key(child)]
        child.x, child.y = vertex.x, vertex.y
    return vertices, ordered_links


def _device_image(kind: str) -> str:
    """Self-contained SVG icons; no external images or stencil libraries required."""
    drawings = {
        "firewall": (
            '<rect x="8" y="10" width="80" height="52" rx="3" fill="#d97745" stroke="#7c3626" stroke-width="2"/>'
            '<path d="M8 27h80M8 45h80M28 10v17m40-17v17M48 27v18M28 45v17m40-17v17" '
            'fill="none" stroke="#7c3626" stroke-width="2"/>'
            '<path d="m48 17 17 7v13c0 12-17 20-17 20S31 49 31 37V24Z" '
            'fill="#edf5fc" stroke="#334155" stroke-width="2"/>'
            '<path d="m40 36 6 6 11-13" fill="none" stroke="#237fa8" stroke-width="4"/>'
        ),
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


def write_drawio(nodes: list[Node], destination: Path,
                  vlan_rules: tuple[VlanRule, ...] = DEFAULT_RULES,
                  devices: dict[str, DeviceSettings] | None = None,
                  connections: tuple[tuple[str, str], ...] = ()) -> None:
    """Render inventory devices and explicitly configured topology links."""
    document = ET.Element("mxfile", host="app.diagrams.net", type="device")
    diagram = ET.SubElement(document, "diagram", id="inventory", name="Agent inventory")
    model = ET.SubElement(diagram, "mxGraphModel", grid="1", gridSize="10", page="0",
                         background="#ffffff", tooltips="1", connect="1", arrows="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    inventory = _hierarchy(nodes, vlan_rules, devices, connections)
    vertices, links = _topology_layout(inventory, devices or {}, connections)
    routes = route_links(vertices, links, inventory.width, inventory.height)
    inventory.x = inventory.y = 40
    counter = 0
    topology_ids: dict[str, str] = {}

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
        if depth == 1:
            topology_ids[_box_key(box)] = identifier
        if box.is_host:
            colors = "fillColor=#fff4cc;strokeColor=#b7791f;strokeWidth=2;" if box.kct else "fillColor=none;strokeColor=none;"
            style = ("rounded=0;whiteSpace=wrap;html=0;noLabel=1;"
                     f"fontFamily=Helvetica;{colors}container=1;")
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
            kind = box.device_type or ("server" if "server" in box.label.casefold() else "pc")
            heading_height = max(18, 18 * len(box.heading.splitlines()))
            heading = ET.SubElement(root, "mxCell", id=f"hostname-{counter}",
                                    vertex="1", parent=identifier, value=box.heading,
                                    style=("text;html=0;align=center;verticalAlign=top;"
                                           "fontSize=13;fontStyle=1;fontFamily=Helvetica;"
                                           "whiteSpace=wrap;strokeColor=none;fillColor=none;"))
            geometry(heading, 8, 8, _HOST_WIDTH - 16, heading_height)
            address_height = 16 * len(box.addresses.splitlines())
            addresses = ET.SubElement(root, "mxCell", id=f"addresses-{counter}",
                                      vertex="1", parent=identifier, value=box.addresses,
                                      style=("text;html=0;align=center;verticalAlign=top;"
                                             "fontSize=11;fontFamily=Helvetica;whiteSpace=wrap;"
                                             "strokeColor=none;fillColor=none;"))
            geometry(addresses, 8, 8 + heading_height, _HOST_WIDTH - 16, address_height)
            icon_y = heading_height + address_height + 16
            icon(device_id, identifier, kind, (_HOST_WIDTH - 96) // 2, icon_y)
            if box.kct:
                badge = ET.SubElement(root, "mxCell", id=f"kct-{counter}", vertex="1",
                                      parent=identifier, value="KCT",
                                      style="rounded=1;html=0;fontStyle=1;fontSize=12;fillColor=#f6c453;strokeColor=#b7791f;")
                geometry(badge, _HOST_WIDTH - 90, icon_y + 24, 70, 24)
            details_y = icon_y + 84
            details = ET.SubElement(root, "mxCell", id=f"details-{counter}",
                                    vertex="1", parent=identifier, value=box.details,
                                    style=("text;html=0;align=center;verticalAlign=top;"
                                           "fontSize=11;fontFamily=Helvetica;whiteSpace=wrap;"
                                           "strokeColor=none;fillColor=none;"))
            geometry(details, 8, details_y, _HOST_WIDTH - 16, box.height - details_y - 8)
            if box.notes:
                notes = ET.SubElement(root, "mxCell", id=f"notes-{counter}", vertex="1",
                                      parent=identifier, value=box.notes,
                                      style=("rounded=1;html=0;whiteSpace=wrap;align=left;verticalAlign=top;"
                                             "fontFamily=Helvetica;fontSize=11;spacing=8;"
                                             "fillColor=#fffbe6;strokeColor=#d6c885;"))
                geometry(notes, _HOST_WIDTH + _NOTES_GAP, 8, _NOTES_WIDTH,
                         16 * len(box.notes.splitlines()) + 16)
            return device_id

        if depth == 0:
            legend = ET.SubElement(root, "mxCell", id="topology-legend", vertex="1", parent=identifier,
                                   value="Links are user-declared, not discovered connectivity. VLAN membership is configured/inferred.\nKCT = Key Cyber Terrain. Each host retains all IPs; router/firewall links terminate on VLAN groups.",
                                   style="text;html=0;align=center;fontSize=12;fontColor=#526879;whiteSpace=wrap;")
            geometry(legend, 20, 50, box.width - 40, 60)
        for child in box.children:
            render(child, identifier, depth + 1)
        return identifier

    inventory_id = render(inventory, "1", 0)
    if "internet" in vertices:
        cloud = ET.SubElement(root, "mxCell", id="internet", parent=inventory_id,
                              vertex="1", value="Internet (configured)",
                              style="shape=cloud;whiteSpace=wrap;html=0;fillColor=#edf5fc;strokeColor=#9db5c7;")
        vertex = vertices["internet"]
        geometry(cloud, vertex.x, vertex.y, vertex.width, vertex.height)
        topology_ids["internet"] = "internet"

    for route in routes:
        source, target = topology_ids[route.link.source], topology_ids[route.link.target]
        sx, sy = route.source_port
        tx, ty = route.target_port
        edge = ET.SubElement(root, "mxCell", id=f"link-{source}-{target}", parent=inventory_id,
                             edge="1", source=source, target=target, value="",
                             style=("edgeStyle=none;noEdgeStyle=1;orthogonal=1;rounded=0;html=0;"
                                    f"endArrow={'block' if route.link.directed else 'none'};startArrow=none;"
                                    f"exitX={sx};exitY={sy};entryX={tx};entryY={ty};exitPerimeter=0;entryPerimeter=0;"
                                    "jumpStyle=arc;jumpSize=12;strokeColor=#334155;strokeWidth=1.5;"))
        edge_geometry = ET.SubElement(edge, "mxGeometry", {"relative": "1", "as": "geometry"})
        points = ET.SubElement(edge_geometry, "Array", {"as": "points"})
        for x, y in route.points[1:-1]:
            ET.SubElement(points, "mxPoint", x=str(x), y=str(y))

    ET.indent(document)
    ET.ElementTree(document).write(destination, encoding="utf-8", xml_declaration=True)


def export_drawio(source: Path, destination: Path,
                  vlan_rules: tuple[VlanRule, ...] = DEFAULT_RULES,
                  devices: dict[str, DeviceSettings] | None = None,
                  connections: tuple[tuple[str, str], ...] = ()) -> int:
    """Export the CSV and return the number of nodes written."""
    if source.resolve() == destination.resolve():
        raise ValueError("Input CSV and output diagram must be different files")
    nodes = read_nodes(source)
    if not nodes:
        raise ValueError("CSV contains no host records")
    if devices:
        unknown = devices.keys() - {node.hostname.casefold() for node in nodes}
        if unknown:
            raise ValueError(f"Configured devices not found in inventory: {', '.join(sorted(unknown))}")
    names = {node.hostname.casefold() for node in nodes}
    if connections and "internet" in names:
        raise ValueError("Hostname internet conflicts with the reserved Internet cloud endpoint")
    unknown = {name for connection in connections for name in connection if name != "internet"} - names
    if unknown:
        raise ValueError(f"Connection devices not found in inventory: {', '.join(sorted(unknown))}")
    write_drawio(nodes, destination, vlan_rules, devices, connections)
    return len(nodes)
