# Project guidance

- This project uses a `src` layout: install it with `.\.venv\Scripts\python.exe -m pip install -e .` before running the CLI or tests. See `README.md` for virtual-environment setup.
- Both `python -m tyler_netmap` and the installed `tyler-netmap` command call `src/tyler_netmap/cli.py:main`.
- CSV export lives in `src/tyler_netmap/drawio.py`; preserve multi-IP hosts as single nodes. Packet Tracer-style routers, switches, and cables are decorative: retain the explicit illustrative/not-discovered legend and captions. See `README.md` for the export command and exact CSV headers.
- Diagram hierarchy uses real draw.io parent containers: private range → observed CIDR subnet (nested by containment) → host. Multi-subnet hosts live under their first eligible private interface, with all IPs retained; do not assume `/24` when a prefix is absent.
- The topology is landscape/left-to-right; hostnames are separate bold labels above endpoint icons, with IP/OS details below. Keep cable routing clear of these labels.
- Run tests with `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`; default discovery from the root does not find this test directory.
