"""Configuration validation and rendered device annotations."""

import csv
import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from elastic_netmap_gen.cli import main
from elastic_netmap_gen.configuration import load_configuration
from elastic_netmap_gen.drawio import _device_image
from elastic_netmap_gen.vlan import DEFAULT_RULES


class ConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / "configuration.json"

    def load(self, data):
        self.config.write_text(json.dumps(data), encoding="utf-8")
        return load_configuration(self.config)

    def test_defaults_and_explicit_empty_rules(self) -> None:
        self.assertEqual(self.load({}).vlan_rules, DEFAULT_RULES)
        self.assertEqual(self.load({"vlans": []}).vlan_rules, ())

    def test_comments_and_trailing_commas_rejected(self) -> None:
        for text in ('{// comment\n"devices": {}}',
                     '{/* comment */"devices": {}}', '{"devices": {},}'):
            self.config.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_configuration(self.config)

    def test_invalid_configuration(self) -> None:
        invalid = [[], {"unknown": 1}, {"vlans": {}}, {"vlans": [{}]},
                   {"vlans": [{"range": "10.0.0.0/16", "vlan_id": True}]},
                   {"vlans": [{"range": "10.0.0.0/16", "vlan_id": 4095}]},
                   {"vlans": [{"range": "::/0", "vlan_id": 10}]},
                   {"vlans": [{"range": "10.0.10.1/24", "vlan_id": 10}]},
                   {"vlans": [{"range": "10.0.0.0/16", "vlan_id": 10}] * 2},
                   {"devices": []}, {"devices": {"host": {"kct": "true"}}},
                   {"devices": {"host": {"type": "invalid"}}},
                   {"devices": {"host": {"kctt": True}}},
                   {"devices": {"HOST": {}, "host": {}}},
                   {"devices": {"host": {"vlans": [10]}}},
                   {"devices": {"host": {"type": "router", "vlans": [True]}}},
                   {"devices": {"host": {"type": "router", "vlans": [0]}}},
                   {"devices": {"host": {"type": "router", "vlans": [10, 10]}}},
                   {"devices": {"host": {"type": "router", "vlans": "10"}}},
                   {"devices": {"host": {"type": "router", "internet": "true"}}},
                   {"devices": {"host": {"type": "pc", "internet": True}}}]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)
        self.config.write_text('{"devices": {}, "devices": {}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_configuration(self.config)

    def test_configured_export_and_csv_preservation(self) -> None:
        source = self.root / "agents.csv"
        with source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["local_metadata.host.ip", "local_metadata.host.name", "local_metadata.os.full"])
            writer.writerows([
                ["10.0.10.1/24, 10.0.20.1/24", "gateway", "Linux"],
                ["10.0.10.2/24", "critical", "Linux"],
                ["10.0.10.3/24", "ordinary", "Linux"],
            ])
        original = source.read_bytes()
        self.load({"vlans": [{"range": "10.0.10.0/24", "vlan_id": 110}],
                   "devices": {"GATEWAY": {"type": "router", "kct": True},
                               "critical": {"kct": True}}})
        args = ["--csv", str(source), "--config", str(self.config)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        self.assertEqual(source.read_bytes(), original)
        destination = source.with_suffix(".drawio")
        cells = ET.parse(destination).findall(".//mxCell")
        hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
        self.assertEqual(len(hosts), 3)
        self.assertIn("VLAN 110 (configured/inferred)", [cell.get("value") for cell in cells])
        for host in hosts:
            children = [cell for cell in cells if cell.get("parent") == host.get("id")]
            device = next(cell for cell in children if cell.get("id").startswith("device-"))
            gateway = host.get("value").startswith("gateway\n")
            self.assertIn(_device_image("router" if gateway else "pc"), device.get("style"))
            marked = not host.get("value").startswith("ordinary\n")
            self.assertEqual(any(cell.get("value") == "KCT" for cell in children), marked)
            self.assertEqual("fillColor=#fff4cc" in host.get("style"), marked)
            if gateway:
                self.assertIn("10.0.20.1/24", host.get("value"))
                self.assertIn("Router (configured)", host.get("value"))
        with redirect_stdout(io.StringIO()):
            main(args + ["--vlan-map", "10.0.10.0/24=210"])
        self.assertIn("VLAN 210", destination.read_text())

        self.load({"devices": {"missing-host": {"kct": True}}})
        before = destination.read_bytes()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(args)
        self.assertEqual(destination.read_bytes(), before)

    def test_config_cli_validation(self) -> None:
        self.load({})
        before = self.config.read_bytes()
        for args in [["--config", str(self.config)],
                     ["--csv", "unused.csv", "--config", str(self.config), "--output", str(self.config)]]:
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                main(args)
        self.assertEqual(self.config.read_bytes(), before)

    def test_firewall_and_custom_server_types(self) -> None:
        source = self.root / "roles.csv"
        roles = ["Exchange", "Web", "File", "Database / PostgreSQL", "Custom <application> & API"]
        devices = {"firewall": {"type": "firewall", "vlans": [10], "internet": True, "kct": True}}
        devices.update({f"server-{index}": {"type": "server", "server_type": role, "kct": True}
                        for index, role in enumerate(roles)})
        config = self.load({"vlans": [{"range": "10.0.10.0/24", "vlan_id": 10}], "devices": devices})
        self.assertEqual(config.devices["server-0"].server_type, "Exchange")
        with source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["local_metadata.host.ip", "local_metadata.host.name", "local_metadata.os.full"])
            for index, hostname in enumerate(devices):
                writer.writerow([f"10.0.10.{index + 1}/24", hostname, "Test OS"])
        with redirect_stdout(io.StringIO()):
            main(["--csv", str(source), "--config", str(self.config)])
        cells = ET.parse(source.with_suffix(".drawio")).findall(".//mxCell")
        hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
        self.assertEqual(len(hosts), 6)
        self.assertEqual(len([cell for cell in cells if cell.get("id", "").startswith("kct-")]), 6)
        firewall = next(cell for cell in hosts if cell.get("value").startswith("firewall\n"))
        inventory = next(cell for cell in cells if cell.get("id") == firewall.get("parent"))
        geometry = firewall.find("mxGeometry")
        self.assertEqual(int(geometry.get("x")) * 2 + int(geometry.get("width")),
                         int(inventory.find("mxGeometry").get("width")))
        icon = next(cell for cell in cells if cell.get("parent") == firewall.get("id")
                    and cell.get("id", "").startswith("device-"))
        self.assertIn(_device_image("firewall"), icon.get("style"))
        self.assertEqual(len([cell for cell in cells if cell.get("edge") == "1"]), 2)
        for index, role in enumerate(roles):
            host = next(cell for cell in hosts if cell.get("value").startswith(f"server-{index}\n"))
            details = next(cell for cell in cells if cell.get("parent") == host.get("id")
                           and cell.get("id", "").startswith("details-"))
            self.assertIn(f"Server type: {role}", details.get("value").replace("\n", " "))
            icon = next(cell for cell in cells if cell.get("parent") == host.get("id")
                        and cell.get("id", "").startswith("device-"))
            self.assertIn(_device_image("server"), icon.get("style"))

    def test_server_type_validation(self) -> None:
        for settings in ({"type": "server", "server_type": value} for value in (None, "", "  ", 1, [], True)):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, "server_type"):
                self.load({"devices": {"host": settings}})
        for kind in (None, "router", "firewall", "switch", "pc"):
            settings = {"server_type": "Web"}
            if kind is not None:
                settings["type"] = kind
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "server_type"):
                self.load({"devices": {"host": settings}})
        config = self.load({"devices": {"host": {"type": "server", "server_type": "  Exchange  "}}})
        self.assertEqual(config.devices["host"].server_type, "Exchange")

    def test_notes_validation_and_optional_settings(self) -> None:
        for value in (None, True, 123, [], {}):
            with self.subTest(notes=value), self.assertRaisesRegex(ValueError, "notes must be a string"):
                self.load({"devices": {"host": {"notes": value}}})
        self.assertEqual(self.load({"devices": {"host": {}}}).devices["host"].notes, "")
        self.assertEqual(self.load({"devices": {"host": {"notes": " \n "}}}).devices["host"].notes, "")
        settings = self.load({"devices": {"HOST": {"notes": " First line\n\nSecond line "}}}).devices["host"]
        self.assertEqual(settings.notes, "First line\n\nSecond line")
        self.assertIsNone(settings.device_type)

    def test_notes_render_to_right_and_move_with_host(self) -> None:
        source = self.root / "notes.csv"
        with source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["local_metadata.host.ip", "local_metadata.host.name", "local_metadata.os.full"])
            writer.writerows([["10.0.10.1/24", "router", "Linux"],
                              ["10.0.10.2/24", "workstation", "Linux"],
                              ["10.0.10.3/24", "plain", "Linux"]])
        original = source.read_bytes()
        long_notes = "Owner: <Ops> & team\n\n" + "\n".join(f"Line {index}: " + "very-long-description-" * 5 for index in range(15))
        self.load({"devices": {"router": {"type": "router", "kct": True, "notes": long_notes},
                               "workstation": {"notes": "Connects to the server\nKeep this note with the device"},
                               "plain": {"notes": " "}}})
        with redirect_stdout(io.StringIO()):
            main(["--csv", str(source), "--config", str(self.config)])
        self.assertEqual(source.read_bytes(), original)
        cells = ET.parse(source.with_suffix(".drawio")).findall(".//mxCell")
        hosts = {cell.get("value").splitlines()[0]: cell for cell in cells if cell.get("id", "").startswith("host-")}
        self.assertEqual(len(hosts), 3)
        self.assertEqual(sum(cell.get("id", "").startswith("notes-") for cell in cells), 2)
        for hostname in ("router", "workstation"):
            host = hosts[hostname]
            parts = [cell for cell in cells if cell.get("parent") == host.get("id")]
            notes = next(cell for cell in parts if cell.get("id").startswith("notes-"))
            geo = notes.find("mxGeometry")
            for part in parts:
                if part is notes:
                    continue
                other = part.find("mxGeometry")
                self.assertLessEqual(int(other.get("x")) + int(other.get("width")), int(geo.get("x")))
            parent = host.find("mxGeometry")
            self.assertLessEqual(int(geo.get("x")) + int(geo.get("width")), int(parent.get("width")))
            self.assertLessEqual(int(geo.get("y")) + int(geo.get("height")), int(parent.get("height")))
            self.assertIn("html=0", notes.get("style"))
            if hostname == "router":
                self.assertIn("Owner: <Ops> & team\n\n", notes.get("value"))
                self.assertIn("Line 14:", notes.get("value"))
                self.assertTrue(any(cell.get("value") == "KCT" for cell in parts))
            else:
                icon = next(cell for cell in parts if cell.get("id").startswith("device-"))
                self.assertIn(_device_image("pc"), icon.get("style"))
        self.assertEqual(int(hosts["plain"].find("mxGeometry").get("width")), 320)

    def test_inline_connection_validation(self) -> None:
        invalid = [None, {}, [None], [{}], [{"from": "a", "to": " "}],
                   [{"from": 1, "to": "b"}], [{"from": "a", "to": "A"}],
                   [{"from": "a", "to": "internet"}],
                   [{"from": "a", "to": "b", "extra": True}],
                   [{"from": "a", "to": "b"}, {"from": "A", "to": "B"}],
                   [{"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "a"}]]
        for connections in invalid:
            with self.subTest(connections=connections), self.assertRaises(ValueError):
                self.load({"connections": connections})

    def test_inline_chain_preserves_devices_and_orders_connections(self) -> None:
        source = self.root / "chain.csv"
        with source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["local_metadata.host.ip", "local_metadata.host.name", "local_metadata.os.full"])
            # Deliberately reverse inventory order relative to the path.
            writer.writerows([["10.0.10.5/24", "web", "Linux"],
                              ["10.0.10.4/24", "switch", "Linux"],
                              ["10.0.10.3/24", "router", "Linux"],
                              ["10.0.10.2/24, 10.0.20.2/24", "firewall", "Linux"],
                              ["10.0.10.6/24", "endpoint", "Linux"]])
        data = {"vlans": [{"range": "10.0.10.0/24", "vlan_id": 10}],
                "devices": {"firewall": {"type": "firewall", "internet": True, "kct": True},
                            "router": {"type": "router", "vlans": [10]},
                            "switch": {"type": "switch"},
                            "web": {"type": "server", "server_type": "Web", "kct": True}},
                "connections": [{"from": " Router ", "to": "SWITCH"},
                                {"from": "internet", "to": "firewall"},
                                {"from": "switch", "to": "web"},
                                {"from": "firewall", "to": "router"},
                                {"from": "router", "to": "web"}]}
        self.load(data)
        args = ["--csv", str(source), "--config", str(self.config)]
        with redirect_stdout(io.StringIO()):
            main(args)
        destination = source.with_suffix(".drawio")
        cells = ET.parse(destination).findall(".//mxCell")
        hosts = {cell.get("value").splitlines()[0]: cell for cell in cells
                 if cell.get("id", "").startswith("host-")}
        self.assertEqual(len(hosts), 5)
        path = [hosts[name] for name in ("firewall", "router", "switch", "web")]
        self.assertEqual(len({cell.get("parent") for cell in path}), 1)
        self.assertNotEqual(hosts["endpoint"].get("parent"), path[0].get("parent"))
        router_geo = hosts["router"].find("mxGeometry")
        inventory = next(cell for cell in cells if cell.get("id") == hosts["router"].get("parent"))
        inventory_geo = inventory.find("mxGeometry")
        self.assertEqual(int(router_geo.get("x")) * 2 + int(router_geo.get("width")), int(inventory_geo.get("width")))
        self.assertEqual(int(router_geo.get("y")) * 2 + int(router_geo.get("height")), int(inventory_geo.get("height")))
        # The firewall, switch and server now fan out from the router, rather
        # than forcing a vertical topological stack for every path.
        self.assertGreater(len({cell.find("mxGeometry").get("x") for cell in path}), 1)
        self.assertIn("10.0.20.2/24", hosts["firewall"].get("value"))
        edges = [cell for cell in cells if cell.get("edge") == "1"]
        self.assertEqual(len(edges), 6)  # Five declared path links + one VLAN attachment.
        self.assertEqual(sum("endArrow=block;" in cell.get("style") for cell in edges), 5)
        self.assertEqual(sum(cell.get("id", "").startswith("kct-") for cell in cells), 2)
        self.assertEqual(sum(cell.get("source") == "internet" for cell in edges), 1)
        before = destination.read_bytes()
        data["connections"].append({"from": "web", "to": "missing"})
        self.load(data)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(args)
        self.assertEqual(destination.read_bytes(), before)

    def test_explicit_router_topology(self) -> None:
        source = self.root / "agents.csv"
        with source.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["local_metadata.host.ip", "local_metadata.host.name", "local_metadata.os.full"])
            writer.writerows([
                ["10.0.10.1/24, 10.0.20.1/24", "gateway", "Linux"],
                ["10.0.10.3/24", "backup", "Linux"],
                ["10.0.10.2/24", "endpoint", "Linux"],
            ])
        self.load({"vlans": [{"range": "10.0.10.0/24", "vlan_id": 10}],
                   "devices": {"GATEWAY": {"type": "router", "vlans": [10, 20], "internet": True, "kct": True},
                               "backup": {"type": "router", "vlans": [10]}}})
        with redirect_stdout(io.StringIO()):
            main(["--csv", str(source), "--config", str(self.config)])
        cells = ET.parse(source.with_suffix(".drawio")).findall(".//mxCell")
        by_id = {cell.get("id"): cell for cell in cells}
        hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
        self.assertEqual(len(hosts), 3)
        gateway = next(cell for cell in hosts if cell.get("value").startswith("gateway\n"))
        backup = next(cell for cell in hosts if cell.get("value").startswith("backup\n"))
        endpoint = next(cell for cell in hosts if cell.get("value").startswith("endpoint\n"))
        vlan10 = next(cell for cell in cells if cell.get("value", "").startswith("VLAN 10"))
        vlan20 = next(cell for cell in cells if cell.get("value", "").startswith("VLAN 20"))
        self.assertEqual(endpoint.get("parent"), vlan10.get("id"))
        self.assertEqual(gateway.get("parent"), vlan10.get("parent"))
        self.assertIn("10.0.20.1/24", gateway.get("value"))
        self.assertTrue(any(cell.get("value") == "KCT" and cell.get("parent") == gateway.get("id") for cell in cells))
        edges = [cell for cell in cells if cell.get("edge") == "1"]
        self.assertEqual({(cell.get("source"), cell.get("target")) for cell in edges}, {
            ("internet", gateway.get("id")), (gateway.get("id"), vlan10.get("id")),
            (gateway.get("id"), vlan20.get("id")), (backup.get("id"), vlan10.get("id")),
        })
        for edge in edges:
            self.assertIn(edge.get("source"), by_id)
            self.assertIn(edge.get("target"), by_id)
            self.assertIn("jumpStyle=arc", edge.get("style"))
            self.assertIn("noEdgeStyle=1", edge.get("style"))
        devices = [cell for cell in cells if cell.get("id", "").startswith("device-")]
        self.assertEqual(len(devices), 3)
        self.assertTrue(all(cell.get("parent", "").startswith("host-") for cell in devices))
        cloud = by_id["internet"].find("mxGeometry")
        router = gateway.find("mxGeometry")
        inventory = by_id[gateway.get("parent")].find("mxGeometry")
        center = int(inventory.get("width")) / 2
        for host in (gateway, backup):
            geo = host.find("mxGeometry")
            self.assertEqual(int(geo.get("x")) + int(geo.get("width")) / 2, center)
        for edge in edges:
            self.assertTrue(edge.findall(".//mxPoint"))
