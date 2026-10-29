"""Smoke tests for the command-line entry point."""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tyler_netmap.cli import identify_internal_subnet, main


class CliTests(unittest.TestCase):
    def test_main(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main([])
        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "Tyler Netmap is ready.\n")

    def test_internal_subnets(self) -> None:
        for address, expected in (
            ("10.0.0.0", "10.0.0.0/8"),
            ("10.255.255.255", "10.0.0.0/8"),
            ("172.16.0.0", "172.16.0.0/12"),
            ("172.31.255.255", "172.16.0.0/12"),
            ("192.168.0.0", "192.168.0.0/16"),
            ("192.168.255.255", "192.168.0.0/16"),
        ):
            with self.subTest(address=address):
                self.assertEqual(identify_internal_subnet(address), expected)

    def test_other_ranges(self) -> None:
        for address in ("8.8.8.8", "172.15.255.255", "172.32.0.0", "192.169.0.1",
                        "127.0.0.1", "169.254.0.1", "100.64.0.1"):
            with self.subTest(address=address):
                self.assertIsNone(identify_internal_subnet(address))

    def test_invalid_addresses(self) -> None:
        for address in ("10.256.0.1", "10.1.2", "10.01.2.3", "10.1.2.3\n",
                        "x10.1.2.3", "::1", "10.1.2.3/8", ""):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    identify_internal_subnet(address)

    def test_classification_output(self) -> None:
        for address, expected in (
            ("192.168.1.20", "192.168.1.20 is internal (192.168.0.0/16).\n"),
            ("8.8.8.8", "8.8.8.8 is not in an RFC 1918 internal subnet.\n"),
        ):
            with self.subTest(address=address), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([address]), 0)
                self.assertEqual(output.getvalue(), expected)

    def test_invalid_address_cli(self) -> None:
        with redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                main(["10.256.0.1"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("Invalid IPv4 address", output.getvalue())


if __name__ == "__main__":
    unittest.main()
