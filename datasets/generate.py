"""Generate ten reproducible large synthetic inventories and run the export CLI."""

import argparse
import csv
import json
import random
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


HEADERS = ["local_metadata.host.ip", "local_metadata.host.name", "local_metadata.os.full"]
SERVER_TYPES = ["Exchange", "Web", "File", "Database / PostgreSQL", "DNS", "Backup", "Monitoring", "Application"]


def generate(output: Path, index: int, seed: int) -> dict:
    rng = random.Random(seed)
    scenario = f"large_{index:02d}"
    count = rng.randint(100, 240)
    vlan_count = rng.randint(4, 9)
    vlan_ids = sorted(rng.sample(range(10, 1000), vlan_count))
    octets = rng.sample(range(10, 220), vlan_count)
    networks = {vlan: f"10.{index}.{octet}" for vlan, octet in zip(vlan_ids, octets)}
    next_ip = dict.fromkeys(vlan_ids, 10)
    firewall_count = rng.randint(1, 3)
    router_count = rng.randint(2, 5)
    switch_count = rng.randint(3, 8)
    server_count = rng.randint(25, count // 2)
    kinds = (["firewall"] * firewall_count + ["router"] * router_count
             + ["switch"] * switch_count + ["server"] * server_count)
    kinds += ["pc"] * (count - len(kinds))
    rng.shuffle(kinds)
    devices = {}
    rows = []
    hosts_by_kind = {kind: [] for kind in set(kinds)}
    roles = SERVER_TYPES.copy()
    rng.shuffle(roles)
    for number, kind in enumerate(kinds, 1):
        hostname = f"large-{index:02d}-{kind}-{number:03d}"
        hosts_by_kind[kind].append(hostname)
        settings = {"type": kind}
        if kind == "server":
            role = roles.pop() if roles else rng.choice(SERVER_TYPES)
            settings["server_type"] = role
            operating_system = "Windows Server 2022" if role == "Exchange" else rng.choice(
                ["Ubuntu 24.04 LTS", "Rocky Linux 9", "Debian GNU/Linux 12", "Windows Server 2022"])
        elif kind == "pc":
            operating_system = rng.choice(["Windows 11 Enterprise", "Ubuntu 24.04 LTS", "macOS 15"])
        else:
            operating_system = rng.choice(["Network appliance OS", "Debian GNU/Linux 12"])
        chosen = rng.sample(vlan_ids, rng.randint(1, min(3, vlan_count)) if kind in ("firewall", "router") else 1)
        if kind == "server" and rng.random() < 0.3:
            chosen = rng.sample(vlan_ids, 2)
        addresses = []
        for vlan in chosen:
            addresses.append(f"{networks[vlan]}.{next_ip[vlan]}/24")
            next_ip[vlan] += 1
        if rng.random() < 0.2:
            addresses.append(f"2001:db8:{index:x}:{octets[vlan_ids.index(chosen[0])]:x}::{number:x}/64")
        if rng.random() < 0.15:
            addresses.append("127.0.0.1/8")
        devices[hostname] = settings
        rows.append([", ".join(addresses), hostname, operating_system])

    kct_count = rng.randint((count + 2) // 3, count * 55 // 100)
    for hostname in rng.sample(list(devices), kct_count):
        devices[hostname]["kct"] = True

    firewalls = hosts_by_kind["firewall"]
    routers = hosts_by_kind["router"]
    links = []
    for number, hostname in enumerate(firewalls):
        upstream = "internet" if number == 0 or rng.random() < 0.5 else firewalls[number - 1]
        links.append({"from": upstream, "to": hostname})
    for number, hostname in enumerate(routers):
        upstream = rng.choice(firewalls + routers[:number])
        links.append({"from": upstream, "to": hostname})
        devices[hostname]["vlans"] = sorted(rng.sample(vlan_ids, rng.randint(1, vlan_count)))
    # Every configured VLAN has an attachment, including shared/router-redundant VLANs.
    for vlan in vlan_ids:
        if not any(vlan in devices[hostname]["vlans"] for hostname in routers):
            devices[rng.choice(routers)]["vlans"].append(vlan)
    inline_switches = rng.sample(hosts_by_kind["switch"], rng.randint(1, 3))
    for hostname in inline_switches:
        links.append({"from": rng.choice(routers), "to": hostname})
    for hostname in rng.sample(hosts_by_kind["server"], rng.randint(1, 3)):
        links.append({"from": rng.choice(routers + inline_switches), "to": hostname})
    rng.shuffle(links)
    rng.shuffle(rows)
    config = {
        "vlans": [{"range": f"{networks[vlan]}.0/24", "vlan_id": vlan} for vlan in vlan_ids],
        "devices": devices,
        "connections": links,
    }
    inventory = output / f"inventory_{scenario}.csv"
    configuration = output / f"configuration_{scenario}.json"
    diagram = output / f"network_{scenario}.drawio"
    with inventory.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADERS)
        writer.writerows(rows)
    configuration.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "elastic_netmap_gen", "--csv", str(inventory),
                    "--config", str(configuration), "--output", str(diagram)], check=True)

    # Check actual rendered output, not only the generated inputs.
    cells = ET.parse(diagram).findall(".//mxCell")
    by_id = {cell.get("id"): cell for cell in cells}
    hosts = [cell for cell in cells if cell.get("id", "").startswith("host-")]
    badges = {cell.get("parent") for cell in cells if cell.get("id", "").startswith("kct-")}
    assert len(hosts) == len({row[1] for row in rows}) == count >= 100
    assert len(badges) == kct_count and kct_count * 3 >= count
    host_ids = {cell.get("value").splitlines()[0]: cell.get("id") for cell in hosts}
    for name, identifier in host_ids.items():
        assert (identifier in badges) == devices[name].get("kct", False)
        assert (identifier in badges) == ("fillColor=#fff4cc" in by_id[identifier].get("style"))
    edges = [cell for cell in cells if cell.get("edge") == "1"]
    assert len(edges) == len(links) + sum(len(devices[name]["vlans"]) for name in routers)
    actual_links = {(cell.get("source"), cell.get("target")) for cell in edges}
    for link in links:
        source = "internet" if link["from"] == "internet" else host_ids[link["from"]]
        target = host_ids[link["to"]]
        assert (source, target) in actual_links
    centers = []
    for hostname in routers:
        host = by_id[host_ids[hostname]]
        geo = host.find("mxGeometry")
        parent = by_id[host.get("parent")].find("mxGeometry")
        centers.append((int(geo.get("x")) + int(geo.get("width")) / 2,
                        int(geo.get("y")) + int(geo.get("height")) / 2))
    assert abs(sum(x for x, _ in centers) / len(centers) - int(parent.get("width")) / 2) <= 1
    assert abs(sum(y for _, y in centers) / len(centers) - int(parent.get("height")) / 2) <= 1
    return {"scenario": scenario, "seed": seed, "devices": count, "kct": kct_count,
            "vlans": vlan_count, "routers": router_count, "firewalls": firewall_count,
            "switches": switch_count, "servers": server_count, "connections": len(links)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20261029, help="Base random seed")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summaries = [generate(args.output, index, args.seed + index) for index in range(1, 11)]
    (args.output / "manifest.json").write_text(json.dumps(summaries, indent=2) + "\n", encoding="utf-8")
    for summary in summaries:
        print(f"{summary['scenario']}: {summary['devices']} devices, {summary['kct']} KCT, "
              f"{summary['vlans']} VLANs, {summary['routers']} routers, {summary['firewalls']} firewalls")


if __name__ == "__main__":
    main()
