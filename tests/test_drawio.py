"""CSV grouping and diagrams.net serialization tests."""

import csv
import io
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import unquote

from elastic_netmap_gen.cli import main
from elastic_netmap_gen.drawio import _hierarchy, _layout, export_drawio
from elastic_netmap_gen.configuration import DeviceSettings
from elastic_netmap_gen.inventory import Node
from elastic_netmap_gen.inventory import HOST_FIELD, IP_FIELD, OS_FIELD, read_nodes
from elastic_netmap_gen.vlan import build_rules


class DrawioTests(unittest.TestCase):
    def test_router_core_stays_centered_with_unbalanced_vlan_groups(self) -> None:
        for router_count, vlan_count in ((1, 0), (1, 1), (1, 6), (3, 5)):
            with self.subTest(routers=router_count, vlans=vlan_count):
                nodes = [Node(f"router-{index}", ["10.0.0.1/24"], ["Linux"])
                         for index in range(router_count)]
                devices = {node.hostname: DeviceSettings("router") for node in nodes}
                for vlan in range(1, vlan_count + 1):
                    nodes.extend(Node(f"host-{vlan}-{index}", [f"10.0.{vlan}.{index + 1}/24"], ["Linux"])
                                 for index in range(vlan * 3))
                box = _hierarchy(nodes, build_rules(["10.0.0.0/16"], []), devices)
                _layout(box, top=True)
                routers = [child for child in box.children if child.is_host]
                self.assertAlmostEqual(sum(router.x + router.width / 2 for router in routers) / len(routers), box.width / 2, delta=1)
                self.assertAlmostEqual(sum(router.y + router.height / 2 for router in routers) / len(routers), box.height / 2, delta=1)
                for index, child in enumerate(box.children):
                    self.assertGreaterEqual(child.x, 0)
                    self.assertGreaterEqual(child.y, 110)
                    self.assertLessEqual(child.x + child.width, box.width)
                    self.assertLessEqual(child.y + child.height, box.height)
                    for other in box.children[index + 1:]:
                        self.assertTrue(child.x + child.width <= other.x or other.x + other.width <= child.x
                                        or child.y + child.height <= other.y or other.y + other.height <= child.y)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "agents.csv"
        self.destination = self.source.with_suffix(".drawio")

    def write_csv(self, rows: list[list[str]]) -> None:
        with self.source.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow([IP_FIELD, HOST_FIELD, OS_FIELD])
            writer.writerows(rows)

    def test_grouping_and_xml(self) -> None:
        self.write_csv([
            ["10.1.0.1/24, ::1/128, 127.0.0.1/8", "host<&>", "Windows & Linux"],
            ["10.1.0.1/24, 192.168.1.2/24", "HOST<&>", "Windows & Linux"],
            ["172.16.0.1/24", "other", "Linux"],
        ])
        self.assertEqual(export_drawio(self.source, self.destination), 2)
        document = ET.parse(self.destination)
        self.assertEqual(document.getroot().tag, "mxfile")
        cells = [cell for cell in document.findall(".//mxCell[@vertex='1']")
                 if cell.get("id", "").startswith("host-")]
        self.assertEqual(len(cells), 2)
        label = cells[0].get("value", "")
        for value in ("host<&>", "10.1.0.1/24", "::1/128", "127.0.0.1/8",
                      "192.168.1.2/24", "Windows & Linux"):
            self.assertIn(value, label)
        self.assertEqual(label.count("10.1.0.1/24"), 1)
        self.assertFalse(document.findall(".//mxCell[@edge='1']"))

    def test_inventory_icons_without_illustrative_devices_or_cables(self) -> None:
        self.write_csv([
            ["10.1.0.1/24", "workstation", "Windows 10"],
            ["10.1.0.2/24", "application", "Windows Server 2022"],
        ])
        export_drawio(self.source, self.destination)
        document = ET.parse(self.destination)
        cells = document.findall(".//mxCell")
        by_id = {cell.get("id"): cell for cell in cells}
        hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
        devices = [cell for cell in cells if cell.get("id", "").startswith("device-")]
        edges = document.findall(".//mxCell[@edge='1']")
        self.assertEqual(len(hosts), 2)
        self.assertEqual(len(devices), len(hosts))
        self.assertEqual(edges, [])
        for device in devices:
            style = device.get("style")
            self.assertIn("shape=image;", style)
            image = style.split("image=", 1)[1].split(";", 1)[0]
            self.assertTrue(image.startswith("data:image/svg+xml,"))
            svg = ET.fromstring(unquote(image.split(",", 1)[1]))
            self.assertEqual(svg.tag, "{http://www.w3.org/2000/svg}svg")
        for edge in edges:
            self.assertIn(edge.get("source"), by_id)
            self.assertIn(edge.get("target"), by_id)
            self.assertTrue(edge.get("source").startswith("device-"))
            self.assertTrue(edge.get("target").startswith("device-"))
            self.assertIn("endArrow=none", edge.get("style"))
        self.assertIn("user-declared", by_id["topology-legend"].get("value"))
        self.assertIn("not discovered", by_id["topology-legend"].get("value"))
        # Workstation and server use distinct endpoint illustrations.
        endpoint_icons = [device for device in devices
                          if device.get("parent", "").startswith("host-")]
        self.assertNotEqual(endpoint_icons[0].get("style"), endpoint_icons[1].get("style"))

    def test_hostnames_above_icons_and_horizontal_layout(self) -> None:
        self.write_csv([
            ["10.1.0.1/24", "workstation", "Windows 10"],
            ["172.20.0.1/24", "long-hostname-that-needs-wrapping.example.invalid", "Linux"],
            ["192.168.1.1/24", "server", "Windows Server"],
        ])
        export_drawio(self.source, self.destination)
        document = ET.parse(self.destination)
        cells = document.findall(".//mxCell[@vertex='1']")
        children = {}
        for cell in cells:
            children.setdefault(cell.get("parent"), []).append(cell)
        hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
        for host in hosts:
            parts = children[host.get("id")]
            heading = next(cell for cell in parts if cell.get("id").startswith("hostname-"))
            addresses = next(cell for cell in parts if cell.get("id").startswith("addresses-"))
            icon = next(cell for cell in parts if cell.get("id").startswith("device-"))
            details = next(cell for cell in parts if cell.get("id").startswith("details-"))
            heading_geo, icon_geo, details_geo = (
                cell.find("mxGeometry") for cell in (heading, icon, details))
            address_geo = addresses.find("mxGeometry")
            self.assertEqual(int(heading_geo.get("y")) + int(heading_geo.get("height")),
                             int(address_geo.get("y")))
            self.assertLessEqual(int(address_geo.get("y")) + int(address_geo.get("height")),
                                 int(icon_geo.get("y")))
            self.assertIn("/24", addresses.get("value"))
            self.assertLessEqual(int(heading_geo.get("y")) + int(heading_geo.get("height")),
                                 int(icon_geo.get("y")))
            self.assertLessEqual(int(icon_geo.get("y")) + int(icon_geo.get("height")),
                                 int(details_geo.get("y")))
            self.assertIn("fontStyle=1", heading.get("style"))
            self.assertIn("noLabel=1", host.get("style"))
        inventory = next(cell for cell in cells if cell.get("parent") == "1")
        ranges = [cell for cell in children[inventory.get("id")]
                  if cell.get("id").startswith("group-")]
        positions = [cell.find("mxGeometry") for cell in ranges]
        self.assertEqual(len({geo.get("y") for geo in positions}), 1)
        self.assertEqual(len(positions), 1)  # All unmatched private hosts share Unknown VLAN.
        inventory_geo = inventory.find("mxGeometry")
        self.assertGreater(int(inventory_geo.get("width")), int(inventory_geo.get("height")))
        for edge in document.findall(".//mxCell[@edge='1']"):
            self.assertIn("exitX=1;exitY=0.5;entryX=0;entryY=0.5", edge.get("style"))

    def test_flat_groups_and_single_multihomed_node(self) -> None:
        self.write_csv([
            ["127.0.0.1/8, 172.20.0.10/16", "broad", "Linux"],
            ["172.20.0.20/24, 10.0.0.2/24", "multi", "Linux"],
            ["172.20.0.21/24", "peer", "Linux"],
        ])
        export_drawio(self.source, self.destination)
        document = ET.parse(self.destination)
        cells = document.findall(".//mxCell[@vertex='1']")
        groups = {cell.get("value"): cell for cell in cells
                  if cell.get("id", "").startswith("group-")}
        self.assertNotIn("172.20.0.0/16", groups)
        self.assertNotIn("172.20.0.0/24", groups)
        hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
        self.assertEqual(len(hosts), 3)
        multi = next(cell for cell in hosts if cell.get("value", "").startswith("multi\n"))
        self.assertEqual(multi.get("parent"), groups["Unknown VLAN"].get("id"))
        self.assertIn("10.0.0.2/24", multi.get("value"))
        self.assertNotIn("127.0.0.0/8", groups)

        # Every nested cell fits; group and host cards clear the title bar.
        by_id = {cell.get("id"): cell for cell in cells}
        for cell in cells:
            parent = by_id.get(cell.get("parent"))
            if parent is None:
                continue
            geometry = cell.find("mxGeometry")
            parent_geometry = parent.find("mxGeometry")
            x, y, width, height = (int(geometry.get(key)) for key in ("x", "y", "width", "height"))
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 40 if cell.get("id").startswith(("host-", "group-")) else 0)
            self.assertLessEqual(x + width, int(parent_geometry.get("width")))
            self.assertLessEqual(y + height, int(parent_geometry.get("height")))

    def test_local_only_ipv6_and_public_fallbacks(self) -> None:
        self.write_csv([
            ["::1/128, 127.0.0.1/8, 169.254.1.1/16", "local", "Linux"],
            ["2001:db8::1/64", "ipv6", "Linux"],
            ["8.8.8.8", "public", "Linux"],
            ["bad-address", "invalid", "Linux"],
            ["8.8.4.4/32, 10.5.0.1/24", "private-preferred", "Linux"],
        ])
        self.assertEqual(export_drawio(self.source, self.destination), 5)
        cells = ET.parse(self.destination).findall(".//mxCell[@vertex='1']")
        groups = {cell.get("value"): cell.get("id") for cell in cells
                  if cell.get("id", "").startswith("group-")}
        for hostname, group in (("local", "Unassigned / local-only addresses"),
                                ("invalid", "Unassigned / local-only addresses"),
                                ("ipv6", "Other IPv6"), ("public", "Other IPv4"),
                                ("private-preferred", "Unknown VLAN")):
            with self.subTest(hostname=hostname):
                host = next(cell for cell in cells if cell.get("value", "").startswith(hostname + "\n"))
                self.assertEqual(host.get("parent"), groups[group])

    def test_unnamed_hosts_stay_separate(self) -> None:
        self.write_csv([["10.0.0.1", "", "Linux"], ["10.0.0.2", "", "Linux"]])
        self.assertEqual(len(read_nodes(self.source)), 2)

    def test_missing_fields(self) -> None:
        self.source.write_text("ip,hostname\n10.0.0.1,host\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing required fields"):
            export_drawio(self.source, self.destination)
        self.assertFalse(self.destination.exists())

    def test_empty_inventory(self) -> None:
        self.write_csv([])
        with self.assertRaisesRegex(ValueError, "no host records"):
            export_drawio(self.source, self.destination)

    def test_prevent_overwriting_input(self) -> None:
        self.write_csv([["10.0.0.1", "host", "Linux"]])
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "different files"):
            export_drawio(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_cli_export(self) -> None:
        self.write_csv([["10.0.0.1", "host", "Linux"]])
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--csv", str(self.source)]), 0)
        self.assertIn("Wrote 1 nodes", output.getvalue())
        self.assertTrue(self.destination.exists())

    def test_vlan_grouping_preserves_multihomed_hosts_and_prefixes(self) -> None:
        self.write_csv([
            ["10.0.10.1/16, 10.0.20.2/24", "multi", "Linux"],
            ["10.0.10.2", "bare", "Linux"],
            ["10.0.20.3/24", "other-vlan", "Linux"],
            ["10.1.10.1/24", "outside", "Linux"],
        ])
        export_drawio(self.source, self.destination, build_rules(["10.0.0.0/16"], []))
        cells = ET.parse(self.destination).findall(".//mxCell")
        by_id = {cell.get("id"): cell for cell in cells}
        hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
        self.assertEqual(len(hosts), 4)
        for host, vlan in zip(hosts, [10, 10, 20, None]):
            parent = by_id[host.get("parent")]
            self.assertEqual(parent.get("value"), f"VLAN {vlan} (configured/inferred)" if vlan else "Unknown VLAN")
        self.assertIn("10.0.20.2/24", hosts[0].get("value"))
        self.assertIn("10.0.10.1/16", hosts[0].get("value"))
        self.assertIn("10.0.10.2", hosts[1].get("value"))
        for host in hosts:
            self.assertNotIn("VLAN", host.get("value"))

    def test_cli_manual_vlan_rules(self) -> None:
        self.write_csv([["172.20.30.1/24", "manual", "Linux"]])
        with redirect_stdout(io.StringIO()):
            main(["--csv", str(self.source), "--vlan-range", "172.20.0.0/16",
                  "--vlan-map", "172.20.30.0/24=300"])
        labels = [cell.get("value") for cell in ET.parse(self.destination).findall(".//mxCell")]
        self.assertIn("VLAN 300 (configured/inferred)", labels)
