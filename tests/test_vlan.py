"""VLAN rule scope, precedence, and validation."""

import ipaddress
import unittest

from elastic_netmap_gen.vlan import DEFAULT_RULES, build_rules, vlan_for


class VlanTests(unittest.TestCase):
    def test_no_default_vlan_assignments(self) -> None:
        self.assertEqual(DEFAULT_RULES, ())
        self.assertIsNone(vlan_for(ipaddress.ip_address("10.0.10.1"), DEFAULT_RULES))

    def test_explicit_convention_scope_and_reserved_zero(self) -> None:
        rules = build_rules(["10.0.0.0/16"], [])
        for address, expected in [("10.0.10.1", 10), ("10.0.255.1", 255),
                                  ("10.0.0.1", None), ("10.1.10.1", None),
                                  ("192.168.10.1", None), ("::1", None)]:
            with self.subTest(address=address):
                self.assertEqual(vlan_for(ipaddress.ip_address(address), rules), expected)

    def test_extensions_and_longest_prefix(self) -> None:
        rules = build_rules(["172.20.0.0/16"], ["172.20.30.0/24=300", "10.0.0.0/16=100"])
        for address, expected in [("10.0.10.1", 100), ("172.20.20.1", 20), ("172.20.30.1", 300)]:
            self.assertEqual(vlan_for(ipaddress.ip_address(address), rules), expected)

    def test_invalid_and_conflicting_rules(self) -> None:
        for ranges, mappings in [(["::/0"], []), (["10.1.2.3/16"], []),
                                 ([], ["10.0.0.0/16"]), ([], ["10.0.0.0/16=0"]),
                                 ([], ["10.0.0.0/16=4095"]), ([], ["10.0.0.0/16=abc"]),
                                 ([], ["10.0.0.0/16=10", "10.0.0.0/16=20"])]:
            with self.subTest(ranges=ranges, mappings=mappings), self.assertRaises(ValueError):
                build_rules(ranges, mappings)
