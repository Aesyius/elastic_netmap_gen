"""Geometric regression tests for radial placement and independent link tracks."""

import math
import unittest

from elastic_netmap_gen.topology import Link, Vertex, radial_layout, route_links
from elastic_netmap_gen.configuration import DeviceSettings
from elastic_netmap_gen.drawio import _hierarchy, _topology_layout
from elastic_netmap_gen.inventory import Node


class TopologyTests(unittest.TestCase):
    def test_notes_are_included_in_routing_obstacles(self):
        nodes = [Node("router", ["10.0.0.1/24"], ["Linux"]),
                 Node("firewall", ["10.0.0.2/24"], ["Linux"]),
                 Node("server", ["10.0.0.3/24"], ["Linux"])]
        settings = {"router": DeviceSettings("router", notes="Primary gateway\n" * 20),
                    "firewall": DeviceSettings("firewall", notes="Inspection policy"),
                    "server": DeviceSettings("server", notes="Application owner\nOperations")}
        connections = (("internet", "firewall"), ("firewall", "router"), ("router", "server"))
        inventory = _hierarchy(nodes, devices=settings, connections=connections)
        vertices, links = _topology_layout(inventory, settings, connections)
        for node in nodes:
            self.assertGreater(vertices["host:" + node.hostname].width, 320)
        self.check_routes(vertices, route_links(vertices, links, inventory.width, inventory.height))

    def check_routes(self, vertices, routes):
        segments = []
        ports = set()
        for number, route in enumerate(routes):
            for endpoint in (route.points[0], route.points[-1]):
                self.assertNotIn(endpoint, ports, "Different links must use distinct attachment points")
                ports.add(endpoint)
            for a, b in zip(route.points, route.points[1:]):
                self.assertNotEqual(a, b)
                self.assertTrue(a[0] == b[0] or a[1] == b[1])
                for vertex in vertices.values():
                    if a[0] == b[0]:
                        collision = (vertex.x < a[0] < vertex.x + vertex.width
                                     and max(min(a[1], b[1]), vertex.y) < min(max(a[1], b[1]), vertex.y + vertex.height))
                    else:
                        collision = (vertex.y < a[1] < vertex.y + vertex.height
                                     and max(min(a[0], b[0]), vertex.x) < min(max(a[0], b[0]), vertex.x + vertex.width))
                    self.assertFalse(collision, f"Link crosses device or label: {vertex.key}")
                for previous, c, d in segments:
                    if previous == number:
                        continue
                    if a[0] == b[0] == c[0] == d[0]:
                        self.assertLessEqual(min(max(a[1], b[1]), max(c[1], d[1])), max(min(a[1], b[1]), min(c[1], d[1])))
                    if a[1] == b[1] == c[1] == d[1]:
                        self.assertLessEqual(min(max(a[0], b[0]), max(c[0], d[0])), max(min(a[0], b[0]), min(c[0], d[0])))
                segments.append((number, a, b))

    def test_branches_grow_outward_from_router(self):
        vertices = {key: Vertex(key, 320, 240, kind) for key, kind in (
            ("router", "router"), ("firewall", "firewall"), ("internet", "cloud"),
            ("switch", "switch"), ("server", "server"))}
        links = [Link("internet", "firewall", True), Link("firewall", "router", True),
                 Link("router", "switch", True), Link("switch", "server", True)]
        width, height = radial_layout(vertices, links)
        center = (width / 2, height / 2)
        self.assertEqual(vertices["router"].center, center)
        distance = {key: math.dist(vertex.center, center) for key, vertex in vertices.items()}
        self.assertGreater(distance["internet"], distance["firewall"])
        self.assertGreater(distance["server"], distance["switch"])
        self.assertNotEqual(vertices["firewall"].center[1], vertices["switch"].center[1])
        self.check_routes(vertices, route_links(vertices, links, width, height))

    def test_shared_vlans_and_nonplanar_links_do_not_share_tracks(self):
        vertices = {f"router-{index}": Vertex(f"router-{index}", 320, 240, "router") for index in range(3)}
        vertices.update({f"vlan-{index}": Vertex(f"vlan-{index}", 1200 + index * 100, 600, "group") for index in range(3)})
        links = [Link(f"router-{router}", f"vlan-{vlan}") for router in range(3) for vlan in range(3)]
        width, height = radial_layout(vertices, links)
        routes = route_links(vertices, links, width, height)
        self.assertEqual(len(routes), 9)
        self.check_routes(vertices, routes)
        self.assertEqual([route.points for route in routes],
                         [route.points for route in route_links(vertices, links, width, height)])

    def test_multiple_roots_unconnected_groups_and_unequal_sizes(self):
        vertices = {f"router-{index}": Vertex(f"router-{index}", 320, 200 + index * 40, "router") for index in range(4)}
        vertices.update({"large": Vertex("large", 2200, 1600), "small": Vertex("small", 640, 380),
                         "detached": Vertex("detached", 1200, 800)})
        links = [Link("router-0", "large"), Link("router-1", "small")]
        width, height = radial_layout(vertices, links)
        roots = [vertex for vertex in vertices.values() if vertex.kind == "router"]
        self.assertAlmostEqual(sum(vertex.center[0] for vertex in roots) / len(roots), width / 2, delta=1)
        self.assertAlmostEqual(sum(vertex.center[1] for vertex in roots) / len(roots), height / 2, delta=1)
        for index, vertex in enumerate(vertices.values()):
            self.assertGreaterEqual(vertex.x, 0)
            self.assertGreaterEqual(vertex.y, 110)
            self.assertLessEqual(vertex.x + vertex.width, width)
            self.assertLessEqual(vertex.y + vertex.height, height)
            for other in list(vertices.values())[index + 1:]:
                self.assertTrue(vertex.x + vertex.width <= other.x or other.x + other.width <= vertex.x
                                or vertex.y + vertex.height <= other.y or other.y + other.height <= vertex.y)
        self.check_routes(vertices, route_links(vertices, links, width, height))
