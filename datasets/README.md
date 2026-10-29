# Large randomized network datasets

Ten synthetic datasets replace the previous junk and small scenario samples.
Each has at least 100 unique devices and at least one-third marked KCT.
Addresses, hostnames, roles, and declared connections are synthetic.

## Naming convention

Each scenario uses a zero-padded name, `large_01` through `large_10`:

- `inventory_<scenario>.csv`: Elastic-format inventory.
- `configuration_<scenario>.json`: randomized VLANs, device roles, KCT, and connections.
- `network_<scenario>.drawio`: generated diagram.

Hostnames follow `large-<scenario number>-<device type>-<three-digit number>`.
CSV headers retain the original sample format:
`local_metadata.host.ip,local_metadata.host.name,local_metadata.os.full`.
Multiple interfaces are stored in a quoted comma-separated field.

| Scenario | Devices | KCT | VLANs | Routers | Firewalls |
| --- | ---: | ---: | ---: | ---: | ---: |
| `large_01` | 239 | 103 | 5 | 4 | 3 |
| `large_02` | 178 | 64 | 5 | 3 | 3 |
| `large_03` | 153 | 62 | 4 | 4 | 2 |
| `large_04` | 180 | 94 | 8 | 4 | 1 |
| `large_05` | 123 | 44 | 8 | 4 | 1 |
| `large_06` | 194 | 73 | 5 | 3 | 3 |
| `large_07` | 142 | 48 | 7 | 5 | 1 |
| `large_08` | 226 | 109 | 4 | 4 | 1 |
| `large_09` | 165 | 67 | 5 | 4 | 2 |
| `large_10` | 142 | 70 | 6 | 3 | 2 |

Configurations vary device counts, VLAN IDs and ranges, operating systems,
multi-IP/dual-stack hosts, KCT assignments, and acyclic inline connection paths.
Every dataset includes firewalls, routers, switches, PCs, Exchange, web, file,
database, DNS, backup, monitoring, and application servers. Some paths branch,
and VLANs may have multiple router attachments. Cloud links enter through
firewalls; all connectivity is explicitly declared rather than discovered.

## Generate and export

Run from the repository root after installing the project into `.venv`:

```sh
.venv/bin/python datasets/generate.py
```

This overwrites the ten matching CSV/configuration/diagram sets, invokes the
export CLI for each, and verifies rendered host counts, KCT badges/highlights,
connection endpoints, and the centered router core.
`manifest.json` records each scenario's seed and counts.

Diagrams use radial branches around the routers. Firewalls, switches, servers,
and VLAN groups follow declared connectivity; links use separate ports and
obstacle-aware tracks, with bridge arcs where dense graphs require crossings.

The default base seed is `20261029`; scenario seeds are base seed + scenario
number. Reusing a seed reproduces the same input data. To create another batch
in a separate directory:

```sh
.venv/bin/python datasets/generate.py --seed 12345 --output datasets/alternate
```

The table above describes the default batch; consult the generated manifest for
custom seeds. To regenerate only the diagrams from existing, possibly edited inputs:

```sh
for csv in datasets/inventory_large_*.csv; do
  scenario="${csv##*/inventory_}"
  scenario="${scenario%.csv}"
  .venv/bin/python -m elastic_netmap_gen \
    --csv "$csv" \
    --config "datasets/configuration_${scenario}.json" \
    --output "datasets/network_${scenario}.drawio" || break
done
```
