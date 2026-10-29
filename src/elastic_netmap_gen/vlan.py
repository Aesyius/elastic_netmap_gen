"""Configurable IPv4 VLAN assignment rules."""

import ipaddress
from dataclasses import dataclass


@dataclass(frozen=True)
class VlanRule:
    network: ipaddress.IPv4Network
    vlan_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.network, ipaddress.IPv4Network):
            raise ValueError("VLAN rules require IPv4 networks")
        if self.vlan_id is not None and not 1 <= self.vlan_id <= 4094:
            raise ValueError("VLAN IDs must be between 1 and 4094")


DEFAULT_RULES: tuple[VlanRule, ...] = ()


def build_rules(ranges: list[str], mappings: list[str],
                base: tuple[VlanRule, ...] = DEFAULT_RULES) -> tuple[VlanRule, ...]:
    """Extend configured rules; explicit mappings override conventions at equal prefix."""
    rules = {rule.network: rule for rule in base}
    for value in ranges:
        rule = VlanRule(ipaddress.IPv4Network(value))
        rules[rule.network] = rule
    explicit = {}
    for value in mappings:
        subnet, separator, identifier = value.partition("=")
        if not separator:
            raise ValueError("VLAN mappings must use CIDR=VLAN_ID")
        rule = VlanRule(ipaddress.IPv4Network(subnet), int(identifier))
        if rule.network in explicit and explicit[rule.network] != rule:
            raise ValueError(f"Conflicting VLAN mappings for {rule.network}")
        explicit[rule.network] = rule
    rules.update(explicit)
    return tuple(rules.values())


def vlan_for(address: ipaddress.IPv4Address | ipaddress.IPv6Address,
             rules: tuple[VlanRule, ...]) -> int | None:
    """Use the most specific matching rule; octet zero is not a usable VLAN ID."""
    if address.version != 4 or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified:
        return None
    matches = [rule for rule in rules if address in rule.network]
    if not matches:
        return None
    rule = max(matches, key=lambda item: item.network.prefixlen)
    identifier = rule.vlan_id if rule.vlan_id is not None else int(str(address).split(".")[2])
    return identifier or None
