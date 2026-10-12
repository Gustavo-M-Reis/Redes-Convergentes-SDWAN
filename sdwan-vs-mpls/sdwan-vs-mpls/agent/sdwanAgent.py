import argparse
import json
import math
import re
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

repoRoot = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repoRoot))

from topo import topo

stopRequested = False


def onSignal(signalNumber, frame):
    global stopRequested
    stopRequested = True


def startProbe(target, agentConfig):
    command = ["ping", "-n", "-c", str(agentConfig["pingCount"]), "-i", str(agentConfig["pingIntervalS"]),
               "-W", str(agentConfig["pingTimeoutS"]), target]
    return subprocess.Popen(topo.namespaceCommand("r1", command), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)


def finishProbe(process, agentConfig):
    limit = agentConfig["pingCount"] * agentConfig["pingIntervalS"] + agentConfig["pingTimeoutS"] + 2
    try:
        output, _ = process.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
    rtts = [float(value) for value in re.findall(r"time=([\d.]+) ms", output)]
    summary = re.search(r"(\d+) packets transmitted, (\d+) received", output)
    sent = int(summary.group(1)) if summary else agentConfig["pingCount"]
    received = min(int(summary.group(2)), sent) if summary else 0
    return {"sent": sent, "received": received, "rtts": rtts[:received] if summary else []}


def windowStats(window, sla):
    sent = sum(probe["sent"] for probe in window)
    received = sum(probe["received"] for probe in window)
    rtts = [value for probe in window for value in probe["rtts"]]
    jitters = [abs(probe["rtts"][index] - probe["rtts"][index - 1])
               for probe in window for index in range(1, len(probe["rtts"]))]
    lossPct = 100.0 * (sent - received) / sent if sent else 100.0
    rttMs = sum(rtts) / len(rtts) if rtts else None
    jitterMs = sum(jitters) / len(jitters) if jitters else None
    slaOk = rttMs is not None and rttMs < sla["rttMs"] and lossPct < sla["lossPct"]
    score = math.inf if rttMs is None else rttMs / sla["rttMs"] + lossPct / sla["lossPct"]
    return {"rttMs": rttMs, "jitterMs": jitterMs, "lossPct": lossPct, "slaOk": slaOk, "score": score}


def jsonSafe(stats):
    return {key: (None if isinstance(value, float) and math.isinf(value) else value) for key, value in stats.items()}


def decide(active, stats, okStreak, agentConfig):
    premium, backup = stats["premium"], stats["backup"]
    if active == "premium":
        if premium["slaOk"]:
            return "premium", 0, None
        better = backup["score"] < premium["score"] * (1 - agentConfig["hysteresis"])
        if backup["slaOk"] or better:
            return "backup", 0, "premiumViolatesSla"
        return "premium", 0, None
    okStreak = okStreak + 1 if premium["slaOk"] else 0
    if okStreak >= agentConfig["failbackCount"]:
        return "premium", 0, "premiumRecovered"
    return "backup", okStreak, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--config", default=None)
    arguments = parser.parse_args()
    config = topo.loadConfig(arguments.config)
    agentConfig, sla = config["agent"], config["sla"]
    signal.signal(signal.SIGTERM, onSignal)
    signal.signal(signal.SIGINT, onSignal)

    windows = {name: deque(maxlen=agentConfig["windowProbes"]) for name in topo.paths}
    active, okStreak = "premium", 0
    with open(arguments.log, "w", buffering=1) as log:
        log.write(json.dumps({"time": time.time(), "event": "start", "active": active}) + "\n")
        nextTick = time.time()
        while not stopRequested:
            probeStart = time.time()
            processes = {name: startProbe(path["probeTarget"], agentConfig) for name, path in topo.paths.items()}
            probes = {name: finishProbe(process, agentConfig) for name, process in processes.items()}
            if stopRequested:
                break
            for name, probe in probes.items():
                windows[name].append(probe)
            stats = {name: windowStats(windows[name], sla) for name in topo.paths}
            decidedAt = time.time()
            target, okStreak, reason = decide(active, stats, okStreak, agentConfig)
            log.write(json.dumps({
                "time": decidedAt, "event": "probe", "probeStart": probeStart, "active": active,
                "okStreak": okStreak,
                "probes": {name: {"sent": probe["sent"], "received": probe["received"], "rtts": probe["rtts"]}
                           for name, probe in probes.items()},
                "window": {name: jsonSafe(stats[name]) for name in stats},
            }) + "\n")
            if target != active:
                appliedAt = topo.setRoute(target)
                log.write(json.dumps({"time": appliedAt, "event": "switch", "from": active, "to": target,
                                      "reason": reason, "decidedAt": decidedAt}) + "\n")
                active = target
            nextTick += agentConfig["probeIntervalS"]
            now = time.time()
            if nextTick < now:
                nextTick = now
            while not stopRequested and time.time() < nextTick:
                time.sleep(min(0.05, max(0.0, nextTick - time.time())))
        log.write(json.dumps({"time": time.time(), "event": "stop", "active": active}) + "\n")


if __name__ == "__main__":
    main()
