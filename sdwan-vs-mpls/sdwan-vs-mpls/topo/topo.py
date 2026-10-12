import argparse
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

repoRoot = Path(__file__).resolve().parents[1]
namespaceDir = Path("/var/run/netns")
namespacePrefix = "sw"
mtuBytes = 1500
netemParent = "5:1"
netemHandle = "10:"
qdiscPattern = re.compile(r"qdisc (\w+) [^\n]*\n\s*Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+)")

nodeNames = ["h1", "r1", "r2", "r3", "r4", "h2"]
routers = ["r1", "r2", "r3", "r4"]
h1Address = "10.0.1.2"
h2Address = "10.0.2.2"

accessLinks = [
    {"a": "h1", "b": "r1", "subnet": "10.0.1", "aHost": 2, "bHost": 1},
    {"a": "h2", "b": "r4", "subnet": "10.0.2", "aHost": 2, "bHost": 1},
]

coreLinks = [
    {"name": "r1r2", "a": "r1", "b": "r2", "linkClass": "premium", "subnet": "10.0.12", "aHost": 1, "bHost": 2},
    {"name": "r2r4", "a": "r2", "b": "r4", "linkClass": "premium", "subnet": "10.0.24", "aHost": 2, "bHost": 4},
    {"name": "r1r3", "a": "r1", "b": "r3", "linkClass": "backup", "subnet": "10.0.13", "aHost": 1, "bHost": 3},
    {"name": "r3r4", "a": "r3", "b": "r4", "linkClass": "backup", "subnet": "10.0.34", "aHost": 3, "bHost": 4},
]

paths = {
    "premium": {
        "r1Via": "10.0.12.2",
        "r4Via": "10.0.24.2",
        "probeTarget": "10.0.24.4",
        "links": ["r1r2", "r2r4"],
        "r4Interface": "r4-r2",
    },
    "backup": {
        "r1Via": "10.0.13.3",
        "r4Via": "10.0.34.3",
        "probeTarget": "10.0.34.4",
        "links": ["r1r3", "r3r4"],
        "r4Interface": "r4-r3",
    },
}

staticRoutes = [
    ("h1", "default", "10.0.1.1"),
    ("h2", "default", "10.0.2.1"),
    ("r2", "10.0.1.0/24", "10.0.12.1"),
    ("r2", "10.0.2.0/24", "10.0.24.4"),
    ("r3", "10.0.1.0/24", "10.0.13.1"),
    ("r3", "10.0.2.0/24", "10.0.34.4"),
    ("r1", "10.0.24.0/24", "10.0.12.2"),
    ("r1", "10.0.34.0/24", "10.0.13.3"),
    ("r4", "10.0.12.0/24", "10.0.24.2"),
    ("r4", "10.0.13.0/24", "10.0.34.3"),
]


def loadConfig(configPath=None):
    return json.loads(Path(configPath or repoRoot / "config.json").read_text())


def run(command, check=True):
    result = subprocess.run(command, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def namespaceName(node):
    return f"{namespacePrefix}{node}"


def namespaceCommand(node, command):
    return ["ip", "netns", "exec", namespaceName(node)] + command


def inNamespace(node, command, check=True):
    return run(namespaceCommand(node, command), check)


def interfaceName(node, peer):
    return f"{node}-{peer}"


def macAddress(link, host):
    return f"02:00:00:00:{link['subnet'].split('.')[-1]}:{host:02d}"


def linkByName(linkName):
    return next(link for link in coreLinks if link["name"] == linkName)


def nominalParams(config, link):
    return {"delayMs": config["links"][link["linkClass"]]["delayMs"], "jitterMs": 0, "lossPct": 0}


def queueLimit(config, link, params):
    rateBits = config["links"][link["linkClass"]]["rateMbit"] * 1e6
    heldMs = config["queueLatencyMs"] + params["delayMs"] + params["jitterMs"]
    return max(10, math.ceil(rateBits * heldMs / 1000 / (mtuBytes * 8)))


def netemArguments(config, link, params):
    arguments = ["delay", f"{params['delayMs']}ms"]
    if params["jitterMs"] > 0:
        arguments.append(f"{params['jitterMs']}ms")
    if params["lossPct"] > 0:
        arguments += ["loss", f"{params['lossPct']}%"]
    return arguments + ["limit", str(queueLimit(config, link, params))]


def applyNetem(config, link, node, peer, params):
    inNamespace(node, ["tc", "qdisc", "change", "dev", interfaceName(node, peer), "parent", netemParent,
                       "handle", netemHandle, "netem"] + netemArguments(config, link, params))


def applyLinkState(config, linkName, ab, ba):
    link = linkByName(linkName)
    applyNetem(config, link, link["a"], link["b"], ab)
    appliedAt = time.time()
    applyNetem(config, link, link["b"], link["a"], ba)
    return appliedAt


def removeAliases():
    for node in nodeNames:
        aliasPath = namespaceDir / namespaceName(node)
        if aliasPath.is_symlink():
            aliasPath.unlink()
        elif aliasPath.exists():
            run(["ip", "netns", "del", namespaceName(node)], check=False)


def createAliases(nodes):
    namespaceDir.mkdir(parents=True, exist_ok=True)
    removeAliases()
    for name, node in nodes.items():
        (namespaceDir / namespaceName(name)).symlink_to(f"/proc/{node.pid}/ns/net")


def clean():
    from mininet.clean import cleanup
    from mininet.log import setLogLevel

    setLogLevel("warning")
    removeAliases()
    cleanup()


def killStrays(network):
    shellPids = {node.pid for node in network.hosts}
    for node in nodeNames:
        if not (namespaceDir / namespaceName(node)).is_symlink():
            continue
        for pid in run(["ip", "netns", "pids", namespaceName(node)], check=False).split():
            if int(pid) in shellPids:
                continue
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def teardown(network):
    if network is None:
        removeAliases()
        return
    killStrays(network)
    removeAliases()
    try:
        network.stop()
    except Exception:
        clean()


def configure(config, nodes):
    createAliases(nodes)
    interfaces = []
    for link in accessLinks + coreLinks:
        aInterface, bInterface = interfaceName(link["a"], link["b"]), interfaceName(link["b"], link["a"])
        inNamespace(link["a"], ["ip", "addr", "replace", f"{link['subnet']}.{link['aHost']}/24", "dev", aInterface])
        inNamespace(link["b"], ["ip", "addr", "replace", f"{link['subnet']}.{link['bHost']}/24", "dev", bInterface])
        interfaces += [(link["a"], aInterface), (link["b"], bInterface)]
    for node, interface in interfaces:
        inNamespace(node, ["ethtool", "-K", interface, "tso", "off", "gso", "off", "gro", "off"], check=False)
        inNamespace(node, ["ip", "link", "set", interface, "up"])
    for node in nodeNames:
        inNamespace(node, ["sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=1"], check=False)
    for node in routers:
        inNamespace(node, ["sysctl", "-qw", "net.ipv4.ip_forward=1"])
        inNamespace(node, ["sysctl", "-qw", "net.ipv4.conf.all.rp_filter=0"], check=False)
        inNamespace(node, ["sysctl", "-qw", "net.ipv4.conf.default.rp_filter=0"], check=False)
    for link in coreLinks:
        aInterface, bInterface = interfaceName(link["a"], link["b"]), interfaceName(link["b"], link["a"])
        aAddress, bAddress = f"{link['subnet']}.{link['aHost']}", f"{link['subnet']}.{link['bHost']}"
        inNamespace(link["a"], ["ip", "neigh", "replace", bAddress, "lladdr", macAddress(link, link["bHost"]),
                                "dev", aInterface, "nud", "permanent"])
        inNamespace(link["b"], ["ip", "neigh", "replace", aAddress, "lladdr", macAddress(link, link["aHost"]),
                                "dev", bInterface, "nud", "permanent"])
        for node, peer in ((link["a"], link["b"]), (link["b"], link["a"])):
            kinds = parseQdiscs(inNamespace(node, ["tc", "-s", "qdisc", "show", "dev", interfaceName(node, peer)]))
            if "htb" not in kinds or "netem" not in kinds:
                raise RuntimeError(f"{interfaceName(node, peer)} sem htb + netem; o kernel tem sch_netem?")
    for node, destination, gateway in staticRoutes:
        inNamespace(node, ["ip", "route", "replace", destination, "via", gateway])
    setRoute("premium")


def build(config):
    from mininet.link import Link, TCLink
    from mininet.log import setLogLevel
    from mininet.net import Mininet

    setLogLevel("warning")
    removeAliases()
    network = Mininet(topo=None, controller=None, link=TCLink, build=False)
    try:
        nodes = {name: network.addHost(name, ip=None) for name in nodeNames}
        for link in accessLinks:
            network.addLink(nodes[link["a"]], nodes[link["b"]], cls=Link,
                            intfName1=interfaceName(link["a"], link["b"]),
                            intfName2=interfaceName(link["b"], link["a"]))
        for link in coreLinks:
            params = nominalParams(config, link)
            network.addLink(nodes[link["a"]], nodes[link["b"]], cls=TCLink,
                            intfName1=interfaceName(link["a"], link["b"]),
                            intfName2=interfaceName(link["b"], link["a"]),
                            addr1=macAddress(link, link["aHost"]), addr2=macAddress(link, link["bHost"]),
                            bw=config["links"][link["linkClass"]]["rateMbit"], delay=f"{params['delayMs']}ms",
                            max_queue_size=queueLimit(config, link, params))
        network.start()
        configure(config, nodes)
    except Exception:
        teardown(network)
        raise
    return network


def setRoute(pathName):
    path = paths[pathName]
    inNamespace("r1", ["ip", "route", "replace", "10.0.2.0/24", "via", path["r1Via"]])
    inNamespace("r4", ["ip", "route", "replace", "10.0.1.0/24", "via", path["r4Via"]])
    return time.time()


def parseQdiscs(text):
    return {match[1]: {"bytes": int(match[2]), "packets": int(match[3]), "drops": int(match[4]),
                       "overlimits": int(match[5])} for match in qdiscPattern.finditer(text)}


def readTcStats():
    stats = []
    empty = {"bytes": 0, "packets": 0, "drops": 0, "overlimits": 0}
    for link in coreLinks:
        for node, peer, direction in ((link["a"], link["b"], "forward"), (link["b"], link["a"], "reverse")):
            interface = interfaceName(node, peer)
            kinds = parseQdiscs(inNamespace(node, ["tc", "-s", "qdisc", "show", "dev", interface]))
            shaper, netem = kinds.get("htb", empty), kinds.get("netem", empty)
            stats.append({"link": link["name"], "linkClass": link["linkClass"], "node": node,
                          "interface": interface, "direction": direction,
                          "bytes": shaper["bytes"], "packets": shaper["packets"],
                          "queueDrops": shaper["drops"], "netemDrops": netem["drops"],
                          "lossDrops": max(0, netem["drops"] - shaper["drops"])})
    return stats


def readReceivedBytes(node, interface):
    return int(inNamespace(node, ["cat", f"/sys/class/net/{interface}/statistics/rx_bytes"]).strip())


def firstLine(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True)
        text = (result.stdout or result.stderr).strip()
        return text.split("\n")[0] if text else ""
    except OSError:
        return ""


def mininetVersion():
    try:
        from mininet.net import VERSION

        return VERSION
    except ImportError:
        return ""


def versions():
    return {
        "kernel": platform.release(),
        "python": platform.python_version(),
        "mininet": mininetVersion(),
        "iproute2": firstLine(["ip", "-V"]),
        "iperf3": firstLine(["iperf3", "--version"]),
        "ping": firstLine(["ping", "-V"]),
        "cpuCount": os.cpu_count(),
    }


def measureRtt(count):
    output = inNamespace("h1", ["ping", "-n", "-c", str(count), "-i", "0.1", "-W", "1", h2Address], check=False)
    values = [float(value) for value in re.findall(r"time=([\d.]+) ms", output)]
    values = values[1:] if len(values) > 1 else values
    return sum(values) / len(values) if values else float("nan")


def measureRate(pathName, seconds, congestion):
    server = subprocess.Popen(namespaceCommand("h2", ["iperf3", "-s", "-1"]),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    client = subprocess.Popen(
        namespaceCommand("h1", ["iperf3", "-c", h2Address, "-t", str(seconds), "-C", congestion, "-J"]),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    time.sleep(2.0)
    interface = paths[pathName]["r4Interface"]
    startBytes, startTime = readReceivedBytes("r4", interface), time.time()
    time.sleep(max(1.0, seconds - 3.5))
    endBytes, endTime = readReceivedBytes("r4", interface), time.time()
    output, _ = client.communicate(timeout=seconds + 20)
    server.wait(timeout=10)
    linkRateMbit = (endBytes - startBytes) * 8 / (endTime - startTime) / 1e6
    goodputMbit = json.loads(output)["end"]["sum_received"]["bits_per_second"] / 1e6
    return linkRateMbit, goodputMbit


def validate(config, outputPath=None):
    tolerance = config["validation"]["tolerancePct"]
    results = []
    network = None
    try:
        network = build(config)
        for pathName, path in paths.items():
            setRoute(pathName)
            time.sleep(0.3)
            expectedRtt = 2 * sum(nominalParams(config, linkByName(name))["delayMs"] for name in path["links"])
            expectedRate = min(config["links"][linkByName(name)["linkClass"]]["rateMbit"] for name in path["links"])
            rttMs = measureRtt(config["validation"]["pingCount"])
            linkRateMbit, goodputMbit = measureRate(pathName, config["validation"]["iperfSeconds"],
                                                    config["tcpCongestion"])
            rttErrorPct = 100 * (rttMs - expectedRtt) / expectedRtt
            rateErrorPct = 100 * (linkRateMbit - expectedRate) / expectedRate
            results.append({
                "path": pathName, "expectedRttMs": expectedRtt, "rttMs": rttMs, "rttErrorPct": rttErrorPct,
                "expectedRateMbit": expectedRate, "linkRateMbit": linkRateMbit, "rateErrorPct": rateErrorPct,
                "tcpGoodputMbit": goodputMbit,
                "ok": abs(rttErrorPct) <= tolerance and abs(rateErrorPct) <= tolerance,
            })
    finally:
        teardown(network)
    report = {"time": time.time(), "tolerancePct": tolerance, "ok": all(item["ok"] for item in results),
              "paths": results}
    if outputPath:
        Path(outputPath).parent.mkdir(parents=True, exist_ok=True)
        Path(outputPath).write_text(json.dumps(report, indent=2))
    return report


def printValidation(report):
    for item in report["paths"]:
        print(f"{item['path']:8s} RTT {item['rttMs']:.2f} ms (nominal {item['expectedRttMs']:.0f}, "
              f"{item['rttErrorPct']:+.1f}%) | banda {item['linkRateMbit']:.2f} Mbit/s "
              f"(nominal {item['expectedRateMbit']:.0f}, {item['rateErrorPct']:+.1f}%) | "
              f"TCP {item['tcpGoodputMbit']:.2f} Mbit/s | {'OK' if item['ok'] else 'FORA DA TOLERANCIA'}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--cli", action="store_true")
    parser.add_argument("--down", action="store_true")
    parser.add_argument("--output", default=str(repoRoot / "data" / "validation.json"))
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("precisa de root")
    config = loadConfig(arguments.config)
    if arguments.validate or arguments.cli:
        clean()
    if arguments.validate:
        report = validate(config, arguments.output)
        printValidation(report)
        sys.exit(0 if report["ok"] else 1)
    if arguments.cli:
        from mininet.cli import CLI

        network = build(config)
        try:
            CLI(network)
        finally:
            teardown(network)
    if arguments.down:
        clean()


if __name__ == "__main__":
    main()
