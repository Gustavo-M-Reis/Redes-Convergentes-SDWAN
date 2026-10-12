import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

repoRoot = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repoRoot))

from scenarios import degrade
from topo import topo

agentScript = repoRoot / "agent" / "sdwanAgent.py"
analysisScript = repoRoot / "analysis" / "analyze.py"
intervalPattern = re.compile(r"\[\s*\d+\]\s+[\d.]+-[\d.]+\s+sec")


def say(message):
    print(message, flush=True)


def buildJobs(config, repetitions, scenarios, policies, loads):
    jobs = []
    for rep in range(1, repetitions + 1):
        for scenario in config["scenarios"]:
            for load in config["loadsMbit"]:
                for policy in config["policies"]:
                    if scenario not in scenarios or policy not in policies or load not in loads:
                        continue
                    jobs.append({"runId": f"{scenario}{policy.capitalize()}L{load}R{rep:02d}",
                                 "scenario": scenario, "policy": policy, "loadMbit": load, "rep": rep})
    return jobs


def sleepUntil(moment):
    while True:
        remaining = moment - time.time()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.2))


def readClient(process, logPath, intervalS, started):
    with open(logPath, "w") as log:
        for line in process.stdout:
            stamp = time.time()
            log.write(f"{stamp:.6f} {line}")
            if not started["event"].is_set() and intervalPattern.search(line):
                started["time"] = stamp - intervalS
                started["event"].set()


def stopProcess(process, stopSignal, timeout):
    if process is None or process.poll() is not None:
        return
    process.send_signal(stopSignal)
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def runOnce(job, config, configPath, outDir, toolVersions):
    runDir = outDir / job["runId"]
    if runDir.exists():
        shutil.rmtree(runDir)
    runDir.mkdir(parents=True)
    timeline, measure = config["timeline"], config["measure"]
    interval = str(measure["iperfIntervalS"])
    network = agent = ping = client = server = pingLog = None
    startedAt = time.time()
    try:
        network = topo.build(config)
        reply = topo.inNamespace("h1", ["ping", "-n", "-c", "3", "-i", "0.2", "-W", "1", topo.h2Address], check=False)
        if " 0 received" in reply or "received" not in reply:
            raise RuntimeError("sem conectividade entre h1 e h2")

        server = subprocess.Popen(
            topo.namespaceCommand("h2", ["iperf3", "-s", "-1", "-i", interval, "-J",
                                         "--logfile", str(runDir / "iperfServer.json")]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if job["policy"] == "sdwan":
            agent = subprocess.Popen([sys.executable, str(agentScript), "--log", str(runDir / "agent.log"),
                                      "--config", str(configPath)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(config["agent"]["warmupS"])
        pingLog = open(runDir / "ping.log", "w")
        ping = subprocess.Popen(
            topo.namespaceCommand("h1", ["ping", "-D", "-n", "-i", str(measure["pingIntervalS"]), "-W", "1",
                                         topo.h2Address]),
            stdout=pingLog, stderr=subprocess.DEVNULL)
        client = subprocess.Popen(
            topo.namespaceCommand("h1", ["iperf3", "-c", topo.h2Address, "-t", str(timeline["endAt"]),
                                         "-i", interval, "--fq-rate", f"{job['loadMbit']}M",
                                         "-l", str(measure["iperfBlockBytes"]),
                                         "-C", config["tcpCongestion"], "-f", "k", "--forceflush"]),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        started = {"event": threading.Event(), "time": None}
        reader = threading.Thread(target=readClient, daemon=True,
                                  args=(client, runDir / "iperfClient.log", measure["iperfIntervalS"], started))
        reader.start()
        if not started["event"].wait(15):
            raise RuntimeError("iperf3 nao iniciou")
        streamStart = started["time"]

        sleepUntil(streamStart + timeline["degradeAt"])
        degradeTime = degrade.degrade(config, job["scenario"])
        sleepUntil(streamStart + timeline["recoverAt"])
        recoverTime = degrade.restore(config)

        clientCode = client.wait(timeout=timeline["endAt"] - timeline["recoverAt"] + 40)
        reader.join(5)
        server.wait(timeout=15)
        if clientCode != 0:
            raise RuntimeError(f"iperf3 terminou com codigo {clientCode}")
        stopProcess(ping, signal.SIGINT, 3)
        stopProcess(agent, signal.SIGTERM, 5)
        tcStats = topo.readTcStats()

        (runDir / "events.json").write_text(json.dumps({
            "streamStart": streamStart, "degradeTime": degradeTime, "recoverTime": recoverTime,
            "endTime": streamStart + timeline["endAt"], "timeline": timeline}, indent=2))
        (runDir / "tcStats.json").write_text(json.dumps(tcStats, indent=2))
        (runDir / "meta.json").write_text(json.dumps({
            **job, "backend": "Mininet (TCLink: htb + netem)", "startedAt": startedAt, "finishedAt": time.time(),
            "versions": toolVersions, "config": config}, indent=2))
    finally:
        for process in (client, server, ping, agent):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
        if pingLog is not None:
            pingLog.close()
        topo.teardown(network)


def runJob(job, config, configPath, outDir, toolVersions, attempts):
    began = time.time()
    error = None
    for attempt in range(1, attempts + 1):
        try:
            runOnce(job, config, configPath, outDir, toolVersions)
            (outDir / job["runId"] / "status.json").write_text(json.dumps({"ok": True, "attempts": attempt}))
            return True, time.time() - began, None
        except Exception as failure:
            error = f"{type(failure).__name__}: {failure}"
    runDir = outDir / job["runId"]
    runDir.mkdir(parents=True, exist_ok=True)
    (runDir / "status.json").write_text(json.dumps({"ok": False, "attempts": attempts, "error": error}))
    return False, time.time() - began, error


def isDone(outDir, job):
    try:
        return json.loads((outDir / job["runId"] / "status.json").read_text()).get("ok", False)
    except (OSError, ValueError):
        return False


def parseList(value, cast, default):
    return [cast(item) for item in value.split(",")] if value else default


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(repoRoot / "config.json"))
    parser.add_argument("--reps", type=int, default=None)
    parser.add_argument("--dataDir", default=None)
    parser.add_argument("--reportDir", default=None)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--scenarios", default=None)
    parser.add_argument("--policies", default=None)
    parser.add_argument("--loads", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skipValidation", action="store_true")
    parser.add_argument("--noAnalysis", action="store_true")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("precisa de root: sudo python3 run/runAll.py")

    configPath = Path(arguments.config).resolve()
    config = topo.loadConfig(configPath)
    if arguments.quick:
        config["timeline"] = config["quickTimeline"]
    repetitions = arguments.reps or (1 if arguments.quick else config["repetitions"])
    dataDir = Path(arguments.dataDir or repoRoot / "data" / ("check" if arguments.quick else "")).resolve()
    reportDir = Path(arguments.reportDir or (dataDir / "report" if arguments.quick else repoRoot / "report")).resolve()
    outDir = dataDir / "raw"
    outDir.mkdir(parents=True, exist_ok=True)

    toolVersions = topo.versions()
    topo.clean()
    if not arguments.skipValidation:
        say("validando a topologia...")
        report = topo.validate(config, dataDir / "validation.json")
        topo.printValidation(report)
        if not report["ok"]:
            sys.exit("topologia fora da tolerancia; use --skipValidation para ignorar")

    jobs = buildJobs(config, repetitions,
                     parseList(arguments.scenarios, str, list(config["scenarios"])),
                     parseList(arguments.policies, str, config["policies"]),
                     parseList(arguments.loads, int, config["loadsMbit"]))
    pending = [job for job in jobs if arguments.force or not isDone(outDir, job)]
    perRun = config["timeline"]["endAt"] + config["agent"]["warmupS"] + 8
    say(f"{len(jobs)} execucoes, {len(pending)} pendentes, estimativa {len(pending) * perRun / 60:.0f} min")

    failures = []
    began = time.time()
    for index, job in enumerate(pending, start=1):
        ok, elapsed, error = runJob(job, config, configPath, outDir, toolVersions, 2)
        remaining = (time.time() - began) / index * (len(pending) - index)
        say(f"[{index}/{len(pending)}] {job['runId']} {'ok' if ok else 'FALHOU'} "
            f"({elapsed:.0f} s, restam ~{remaining / 60:.0f} min){'' if ok else ' ' + error}")
        if not ok:
            failures.append(job["runId"])

    say(f"concluido em {(time.time() - began) / 60:.1f} min; falhas: {len(failures)}")
    for runId in failures:
        say(f"  {runId}")
    if not arguments.noAnalysis:
        subprocess.run([sys.executable, str(analysisScript), "--dataDir", str(dataDir),
                        "--reportDir", str(reportDir)], check=False)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
