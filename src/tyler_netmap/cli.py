"""Command-line entry point."""

import argparse
import re
from collections.abc import Sequence
from pathlib import Path

from .drawio import export_drawio


_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_IPV4 = re.compile(rf"{_OCTET}(?:\.{_OCTET}){{3}}")
_INTERNAL_SUBNETS = (
    (re.compile(rf"10(?:\.{_OCTET}){{3}}"), "10.0.0.0/8"),
    (re.compile(rf"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.{_OCTET}){{2}}"), "172.16.0.0/12"),
    (re.compile(rf"192\.168(?:\.{_OCTET}){{2}}"), "192.168.0.0/16"),
)


def identify_internal_subnet(address: str) -> str | None:
    """Return the matching RFC 1918 range, or None for other valid IPv4 addresses."""
    if not _IPV4.fullmatch(address):
        raise ValueError(f"Invalid IPv4 address: {address}")
    for pattern, subnet in _INTERNAL_SUBNETS:
        if pattern.fullmatch(address):
            return subnet
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the application."""
    parser = argparse.ArgumentParser(description="Identify internal IPv4 ranges or export an agent CSV to draw.io.")
    parser.add_argument("address", nargs="?", help="IPv4 address to classify")
    parser.add_argument("--csv", type=Path, help="Agent inventory CSV to export")
    parser.add_argument("--output", type=Path, help="Output .drawio file (default: CSV filename with .drawio extension)")
    args = parser.parse_args(argv)
    if args.csv is not None:
        if args.address is not None:
            parser.error("Use either an IPv4 address or --csv, not both")
        destination = args.output if args.output is not None else args.csv.with_suffix(".drawio")
        try:
            count = export_drawio(args.csv, destination)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        print(f"Wrote {count} nodes to {destination}")
        return 0
    if args.output is not None:
        parser.error("--output requires --csv")
    if args.address is None:
        print("Tyler Netmap is ready.")
        return 0
    try:
        subnet = identify_internal_subnet(args.address)
    except ValueError as error:
        parser.error(str(error))
    if subnet is not None:
        print(f"{args.address} is internal ({subnet}).")
    else:
        print(f"{args.address} is not in an RFC 1918 internal subnet.")
    return 0
