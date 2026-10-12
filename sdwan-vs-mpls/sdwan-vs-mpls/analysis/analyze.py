import argparse
import json
import math
import re
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.transforms import blended_transform_factory
from scipy import stats

repoRoot = Path(__file__).resolve().parents[1]

policyLabels = {"static": "Estático", "sdwan": "SD-WAN"}
policyColors = {"static": "#2a78d6", "sdwan": "#eb6834"}
scenarioLabels = {"degrade": "Degradação", "cut": "Corte"}
phaseNames = ["baseline", "degraded", "recovered"]
phaseLabels = {"baseline": "Antes", "degraded": "Degradação", "recovered": "Recuperação"}
ink = {"surface": "#fcfcfb", "primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781",
       "grid": "#e1e0d9", "axis": "#c3c2b7"}

pingPattern = re.compile(r"\[([\d.]+)\] \d+ bytes from [\d.]+: icmp_seq=(\d+) ttl=\d+ time=([\d.]+) ms")
clientPattern = re.compile(
    r"^[\d.]+ \[\s*\d+\]\s+([\d.]+)-([\d.]+)\s+sec\s+[\d.]+\s+\w?Bytes\s+[\d.]+\s+\w?bits/sec\s+(\d+)\s+[\d.]+\s+\w?Bytes",
    re.MULTILINE)

aggregatedMetrics = [
    "baselineGoodputMbps", "baselineEta", "baselineRttMeanMs", "baselineRttP95Ms", "baselineSlaViolation",
    "degradedGoodputMbps", "degradedEta", "degradedRttMeanMs", "degradedRttP95Ms", "degradedRttP99Ms",
    "degradedJitterMs", "degradedPingLossPct", "degradedSlaViolation",
    "recoveredGoodputMbps", "recoveredEta", "recoveredRttMeanMs", "recoveredRttP95Ms", "recoveredSlaViolation",
    "goodputMbps", "eta", "slaViolation", "zeroGoodputS", "tFoS", "tFbS", "routeChanges", "retransmits",
    "degradationDrops", "degradationLossPct", "congestionDropsPremium", "congestionDropsBackup",
    "congestionLossPct",
]
pairedMetrics = [
    ("degradedSlaViolation", "V na degradação"),
    ("degradedEta", "η na degradação"),
    ("degradedRttP95Ms", "RTT p95 na degradação (ms)"),
    ("zeroGoodputS", "Tempo sem vazão (s)"),
    ("slaViolation", "V na execução inteira"),
    ("eta", "η na execução inteira"),
]


def parsePing(path, origin, interval):
    text = path.read_text()
    received = {}
    for match in pingPattern.finditer(text):
        stamp, sequence, rtt = float(match[1]), int(match[2]), float(match[3])
        received.setdefault(sequence, (stamp - origin - rtt / 1000, rtt))
    summary = re.search(r"(\d+) packets transmitted", text)
    transmitted = int(summary[1]) if summary else (max(received) if received else 0)
    if transmitted == 0 or not received:
        return np.array([]), np.array([])
    sequences = np.array(sorted(received))
    sendTimes = np.array([received[sequence][0] for sequence in sequences])
    if len(sequences) >= 2:
        slope, intercept = np.polyfit(sequences, sendTimes, 1)
    else:
        slope, intercept = interval, sendTimes[0] - interval * sequences[0]
    times = slope * np.arange(1, transmitted + 1) + intercept
    rtts = np.full(transmitted, np.nan)
    for sequence, (sendTime, rtt) in received.items():
        if 1 <= sequence <= transmitted:
            times[sequence - 1] = sendTime
            rtts[sequence - 1] = rtt
    return times, rtts


def parseServer(path):
    data = json.loads(path.read_text())
    return np.array([[item["sum"]["start"], item["sum"]["end"], item["sum"]["bytes"]]
                     for item in data.get("intervals", [])], dtype=float).reshape(-1, 3)


def parseClient(path):
    return np.array([[float(match[1]), float(match[2]), int(match[3])]
                     for match in clientPattern.finditer(path.read_text())], dtype=float).reshape(-1, 3)


def parseAgent(path):
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [event for event in events if event["event"] == "switch"]


def overlap(intervals, start, end):
    return np.clip(np.minimum(intervals[:, 1], end) - np.maximum(intervals[:, 0], start), 0, None)


def weightedSum(intervals, start, end):
    if len(intervals) == 0:
        return 0.0
    duration = np.maximum(intervals[:, 1] - intervals[:, 0], 1e-9)
    return float(np.sum(intervals[:, 2] * overlap(intervals, start, end) / duration))


def goodputMbps(intervals, start, end):
    return weightedSum(intervals, start, end) * 8 / (end - start) / 1e6


def zeroGoodputSeconds(intervals, start, end):
    if len(intervals) == 0:
        return float(end - start)
    return float(np.sum(overlap(intervals, start, end)[intervals[:, 2] == 0]))


def pingMetrics(times, rtts, start, end):
    mask = (times >= start) & (times < end)
    selected = rtts[mask]
    answered = selected[~np.isnan(selected)]
    if len(selected) == 0:
        return {"RttMeanMs": np.nan, "RttP95Ms": np.nan, "RttP99Ms": np.nan, "JitterMs": np.nan,
                "PingLossPct": np.nan}
    differences = np.abs(np.diff(selected))
    differences = differences[~np.isnan(differences)]
    return {
        "RttMeanMs": float(np.mean(answered)) if len(answered) else np.nan,
        "RttP95Ms": float(np.percentile(answered, 95)) if len(answered) else np.nan,
        "RttP99Ms": float(np.percentile(answered, 99)) if len(answered) else np.nan,
        "JitterMs": float(np.mean(differences)) if len(differences) else np.nan,
        "PingLossPct": 100.0 * (len(selected) - len(answered)) / len(selected),
    }


def slaViolation(times, rtts, start, end, binS, sla):
    violated = total = 0
    for binStart in np.arange(start, end - 1e-9, binS):
        selected = rtts[(times >= binStart) & (times < min(binStart + binS, end))]
        if len(selected) == 0:
            continue
        answered = selected[~np.isnan(selected)]
        lossPct = 100.0 * (len(selected) - len(answered)) / len(selected)
        total += 1
        if len(answered) == 0 or lossPct >= sla["lossPct"] or np.mean(answered) >= sla["rttMs"]:
            violated += 1
    return violated / total if total else np.nan


def binnedMean(times, values, edges):
    result = np.full(len(edges) - 1, np.nan)
    indexes = np.digitize(times, edges) - 1
    for index in range(len(edges) - 1):
        selected = values[indexes == index]
        selected = selected[~np.isnan(selected)]
        if len(selected):
            result[index] = selected.mean()
    return result


def analyzeRun(runDir):
    meta = json.loads((runDir / "meta.json").read_text())
    events = json.loads((runDir / "events.json").read_text())
    config = meta["config"]
    origin = events["streamStart"]
    endAt = events["timeline"]["endAt"]
    bounds = {"baseline": (0.0, events["degradeTime"] - origin),
              "degraded": (events["degradeTime"] - origin, events["recoverTime"] - origin),
              "recovered": (events["recoverTime"] - origin, float(endAt))}
    times, rtts = parsePing(runDir / "ping.log", origin, config["measure"]["pingIntervalS"])
    received = parseServer(runDir / "iperfServer.json")
    sent = parseClient(runDir / "iperfClient.log")
    switches = parseAgent(runDir / "agent.log")
    tcStats = json.loads((runDir / "tcStats.json").read_text())
    load = meta["loadMbit"]

    row = {"runId": meta["runId"], "scenario": meta["scenario"], "policy": meta["policy"], "loadMbit": load,
           "rep": meta["rep"]}
    for phase, (start, end) in bounds.items():
        row[f"{phase}GoodputMbps"] = goodputMbps(received, start, end)
        row[f"{phase}Eta"] = row[f"{phase}GoodputMbps"] / load
        for key, value in pingMetrics(times, rtts, start, end).items():
            row[f"{phase}{key}"] = value
        row[f"{phase}SlaViolation"] = slaViolation(times, rtts, start, end, config["measure"]["binS"], config["sla"])
        row[f"{phase}Retransmits"] = weightedSum(sent, start, end)
    row["goodputMbps"] = goodputMbps(received, 0.0, endAt)
    row["eta"] = row["goodputMbps"] / load
    row["slaViolation"] = slaViolation(times, rtts, 0.0, endAt, config["measure"]["binS"], config["sla"])
    row["zeroGoodputS"] = zeroGoodputSeconds(received, bounds["degraded"][0], endAt)
    row["retransmits"] = float(np.sum(sent[:, 2])) if len(sent) else np.nan

    failovers = [event["time"] for event in switches
                 if event["to"] == "backup" and event["time"] >= events["degradeTime"]]
    failbacks = [event["time"] for event in switches
                 if event["to"] == "premium" and event["time"] >= events["recoverTime"]]
    row["tFoS"] = failovers[0] - events["degradeTime"] if failovers else np.nan
    row["tFbS"] = failbacks[0] - events["recoverTime"] if failbacks else np.nan
    row["routeChanges"] = len(switches)

    premium = [entry for entry in tcStats if entry["linkClass"] == "premium"]
    forward = [entry for entry in tcStats if entry["direction"] == "forward"]
    lossDrops = sum(entry["lossDrops"] for entry in premium)
    offered = sum(entry["packets"] + entry["netemDrops"] for entry in premium)
    row["degradationDrops"] = lossDrops
    row["degradationLossPct"] = 100.0 * lossDrops / offered if offered else 0.0
    row["congestionDropsPremium"] = sum(entry["queueDrops"] for entry in forward if entry["linkClass"] == "premium")
    row["congestionDropsBackup"] = sum(entry["queueDrops"] for entry in forward if entry["linkClass"] == "backup")
    ingress = [entry for entry in forward if entry["node"] == "r1"]
    ingressPackets = sum(entry["packets"] + entry["netemDrops"] for entry in ingress)
    ingressDrops = sum(entry["queueDrops"] for entry in ingress)
    row["congestionLossPct"] = 100.0 * ingressDrops / ingressPackets if ingressPackets else 0.0
    row["backupForwardPackets"] = sum(entry["packets"] for entry in ingress if entry["linkClass"] == "backup")

    step = config["measure"]["iperfIntervalS"]
    edges = np.arange(0, endAt + step / 2, step)
    series = {
        "grid": edges[:-1] + step / 2,
        "goodput": np.array([goodputMbps(received, edges[index], edges[index + 1]) for index in range(len(edges) - 1)]),
        "rtt": binnedMean(times, rtts, edges),
        "degradedRtts": rtts[(times >= bounds["degraded"][0]) & (times < bounds["degraded"][1])],
        "timeline": events["timeline"],
        "sla": config["sla"],
    }
    return row, series, meta


def meanCi(values):
    values = np.array([value for value in values if value is not None and not (isinstance(value, float) and math.isnan(value))],
                      dtype=float)
    count = len(values)
    if count == 0:
        return np.nan, np.nan, 0
    if count < 2:
        return float(values.mean()), np.nan, count
    half = stats.t.ppf(0.975, count - 1) * values.std(ddof=1) / math.sqrt(count)
    return float(values.mean()), float(half), count


def formatMeanCi(mean, half, digits=2):
    if isinstance(mean, float) and math.isnan(mean):
        return "–"
    if isinstance(half, float) and math.isnan(half):
        return f"{mean:.{digits}f}"
    return f"{mean:.{digits}f} ± {half:.{digits}f}"


def formatNumber(value, digits=3):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    return f"{value:.{digits}f}"


def aggregate(summary):
    rows = []
    for (scenario, load, policy), group in summary.groupby(["scenario", "loadMbit", "policy"], sort=False):
        row = {"scenario": scenario, "loadMbit": load, "policy": policy, "n": len(group)}
        for metric in aggregatedMetrics:
            mean, half, count = meanCi(group[metric].tolist())
            row[f"{metric}Mean"], row[f"{metric}Ci95"], row[f"{metric}N"] = mean, half, count
        rows.append(row)
    return pd.DataFrame(rows)


def pairedTests(summary):
    rows = []
    for (scenario, load), group in summary.groupby(["scenario", "loadMbit"], sort=False):
        for metric, label in pairedMetrics:
            pivot = group.pivot_table(index="rep", columns="policy", values=metric, aggfunc="first")
            if "static" not in pivot or "sdwan" not in pivot:
                continue
            pivot = pivot.dropna(subset=["static", "sdwan"])
            staticValues, sdwanValues = pivot["static"].to_numpy(float), pivot["sdwan"].to_numpy(float)
            differences = sdwanValues - staticValues
            meanDifference, halfDifference, count = meanCi(differences.tolist())
            nonZero = differences[differences != 0]
            pValue = wilcoxonW = welchP = np.nan
            rankBiserial = np.nan if count == 0 else 0.0
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                if count >= 2 and len(nonZero):
                    try:
                        result = stats.wilcoxon(sdwanValues, staticValues)
                        wilcoxonW, pValue = float(result.statistic), float(result.pvalue)
                    except ValueError:
                        pass
                    ranks = stats.rankdata(np.abs(nonZero))
                    rankBiserial = float((ranks[nonZero > 0].sum() - ranks[nonZero < 0].sum()) / ranks.sum())
                if count >= 2 and (staticValues.std() > 0 or sdwanValues.std() > 0):
                    welchP = float(stats.ttest_ind(sdwanValues, staticValues, equal_var=False).pvalue)
            rows.append({"scenario": scenario, "loadMbit": load, "metric": metric, "label": label, "n": count,
                         "staticMean": float(staticValues.mean()) if count else np.nan,
                         "sdwanMean": float(sdwanValues.mean()) if count else np.nan,
                         "meanDifference": meanDifference, "ci95Difference": halfDifference,
                         "wilcoxonW": wilcoxonW, "wilcoxonP": pValue, "rankBiserial": rankBiserial,
                         "welchP": welchP})
    return pd.DataFrame(rows)


def setStyle():
    plt.rcParams.update({
        "font.family": "sans-serif", "font.size": 10, "figure.facecolor": ink["surface"],
        "axes.facecolor": ink["surface"], "savefig.facecolor": ink["surface"], "text.color": ink["primary"],
        "axes.labelcolor": ink["secondary"], "axes.titlesize": 11, "axes.titleweight": "bold",
        "axes.titlelocation": "left", "legend.frameon": False, "legend.fontsize": 9,
    })


def styleAxes(axes):
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(ink["axis"])
        axes.spines[side].set_linewidth(1)
    axes.grid(axis="y", color=ink["grid"], linewidth=1)
    axes.set_axisbelow(True)
    axes.tick_params(colors=ink["muted"], labelsize=9, length=0)


def configLabel(scenario, load, separator=" · "):
    return f"{scenarioLabels.get(scenario, scenario)}{separator}{load} Mbit/s"


def configKeys(summary):
    return list(summary.groupby(["scenario", "loadMbit"], sort=False).groups.keys())


def quantiles(stack):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return np.nanmedian(stack, axis=0), np.nanpercentile(stack, 25, axis=0), np.nanpercentile(stack, 75, axis=0)


def plotTimeSeries(summary, seriesByRun, figureDir):
    files = []
    for scenario, load in configKeys(summary):
        group = summary[(summary.scenario == scenario) & (summary.loadMbit == load)]
        reference = seriesByRun[group.runId.iloc[0]]
        timeline, sla = reference["timeline"], reference["sla"]
        figure, (top, bottom) = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
        maxGoodput, maxRtt = float(load), float(sla["rttMs"])
        for policy in policyLabels:
            runIds = group[group.policy == policy].runId.tolist()
            if not runIds:
                continue
            grid = seriesByRun[runIds[0]]["grid"]
            for axes, key in ((top, "goodput"), (bottom, "rtt")):
                median, lower, upper = quantiles(np.vstack([seriesByRun[runId][key] for runId in runIds]))
                axes.fill_between(grid, lower, upper, color=policyColors[policy], alpha=0.12, linewidth=0)
                axes.plot(grid, median, color=policyColors[policy], linewidth=2, solid_capstyle="round",
                          label=policyLabels[policy])
                peak = np.nanmax(upper) if np.any(~np.isnan(upper)) else 0
                if key == "goodput":
                    maxGoodput = max(maxGoodput, peak)
                else:
                    maxRtt = max(maxRtt, peak)
        for axes in (top, bottom):
            styleAxes(axes)
            for moment in (timeline["degradeAt"], timeline["recoverAt"]):
                axes.axvline(moment, color=ink["muted"], linewidth=1)
        top.set_ylim(0, maxGoodput * 1.3)
        bottom.set_ylim(0, maxRtt * 1.3)
        top.set_xlim(0, timeline["endAt"])
        topBlend = blended_transform_factory(top.transData, top.transAxes)
        top.text(timeline["degradeAt"] + 0.4, 0.97, "degradação", transform=topBlend, va="top", fontsize=9,
                 color=ink["secondary"])
        top.text(timeline["recoverAt"] + 0.4, 0.97, "recuperação", transform=topBlend, va="top", fontsize=9,
                 color=ink["secondary"])
        top.axhline(load, color=ink["axis"], linewidth=1)
        top.text(timeline["endAt"], load, f"carga {load} Mbit/s ", va="bottom", ha="right", fontsize=9,
                 color=ink["secondary"])
        bottom.axhline(sla["rttMs"], color=ink["axis"], linewidth=1)
        bottom.text(timeline["endAt"], sla["rttMs"], f"SLA {sla['rttMs']} ms ", va="bottom", ha="right", fontsize=9,
                    color=ink["secondary"])
        sdwan = group[group.policy == "sdwan"]
        bottomBlend = blended_transform_factory(bottom.transData, bottom.transAxes)
        for column, base, label in (("tFoS", timeline["degradeAt"], "failover"),
                                    ("tFbS", timeline["recoverAt"], "failback")):
            values = sdwan[column].dropna()
            if len(values):
                moment = base + float(values.median())
                bottom.plot([moment], [0.93], marker="v", markersize=8, color=policyColors["sdwan"],
                            markeredgecolor=ink["surface"], markeredgewidth=1.5, transform=bottomBlend, clip_on=False)
                bottom.text(moment + 0.6, 0.93, f"{label} (mediana)", transform=bottomBlend, va="center", fontsize=9,
                            color=ink["secondary"])
        top.set_title(f"{configLabel(scenario, load)}: mediana e intervalo interquartil", pad=12)
        top.set_ylabel("Vazão recebida (Mbit/s)")
        bottom.set_ylabel("RTT h1→h2 (ms)")
        bottom.set_xlabel("Tempo desde o início do fluxo (s)")
        top.legend(loc="lower right", bbox_to_anchor=(1, 1.01), ncol=2, borderaxespad=0)
        figure.tight_layout()
        fileName = f"timeSeries{scenario.capitalize()}L{load}.png"
        figure.savefig(figureDir / fileName, dpi=140)
        plt.close(figure)
        files.append((scenario, load, fileName))
    return files


def plotCdf(summary, seriesByRun, figureDir):
    scenarios = list(dict.fromkeys(summary.scenario))
    loads = list(dict.fromkeys(summary.loadMbit))
    figure, grid = plt.subplots(len(scenarios), len(loads), figsize=(4.6 * len(loads), 3.4 * len(scenarios)),
                                squeeze=False)
    for rowIndex, scenario in enumerate(scenarios):
        for columnIndex, load in enumerate(loads):
            axes = grid[rowIndex][columnIndex]
            styleAxes(axes)
            group = summary[(summary.scenario == scenario) & (summary.loadMbit == load)]
            slaRtt = None
            for policy in policyLabels:
                runIds = group[group.policy == policy].runId.tolist()
                if not runIds:
                    continue
                slaRtt = seriesByRun[runIds[0]]["sla"]["rttMs"]
                pooled = np.concatenate([seriesByRun[runId]["degradedRtts"] for runId in runIds])
                answered = np.sort(pooled[~np.isnan(pooled)])
                lossPct = 100.0 * (len(pooled) - len(answered)) / len(pooled) if len(pooled) else float("nan")
                label = f"{policyLabels[policy]} (perda {lossPct:.1f}%)"
                if not len(answered):
                    label = f"{policyLabels[policy]}: sem resposta"
                if len(answered):
                    axes.step(answered, np.arange(1, len(answered) + 1) / len(answered), where="post",
                              color=policyColors[policy], linewidth=2, label=label)
                else:
                    axes.plot([], [], color=policyColors[policy], linewidth=2, label=label)
            if slaRtt is not None:
                axes.axvline(slaRtt, color=ink["muted"], linewidth=1)
                axes.text(slaRtt, 0.5, " SLA", fontsize=9, color=ink["secondary"], va="center")
            axes.set_ylim(0, 1.02)
            axes.set_xlim(left=0)
            axes.set_title(configLabel(scenario, load))
            axes.set_xlabel("RTT na fase de degradação (ms)")
            axes.set_ylabel("Fração acumulada")
            axes.legend(loc="upper left", fontsize=8.5, handlelength=1.4)
    figure.tight_layout()
    figure.savefig(figureDir / "cdfRtt.png", dpi=140)
    plt.close(figure)
    return "cdfRtt.png"


def plotBars(summary, figureDir):
    metrics = [("degradedSlaViolation", "Violação de SLA na degradação\n(fração do tempo)"),
               ("degradedEta", "Eficiência de vazão η\nna degradação"),
               ("zeroGoodputS", "Tempo sem vazão\napós a degradação (s)")]
    keys = configKeys(summary)
    figure, grid = plt.subplots(1, len(metrics), figsize=(4.2 * len(metrics), 4.4))
    offsets = {"static": -0.18, "sdwan": 0.18}
    for axes, (metric, title) in zip(grid, metrics):
        styleAxes(axes)
        highest = 0.0
        for policy in policyLabels:
            means, halves = [], []
            for scenario, load in keys:
                group = summary[(summary.scenario == scenario) & (summary.loadMbit == load) & (summary.policy == policy)]
                mean, half, _ = meanCi(group[metric].tolist())
                means.append(0.0 if math.isnan(mean) else mean)
                halves.append(0.0 if math.isnan(half) else half)
            positions = np.arange(len(keys)) + offsets[policy]
            axes.bar(positions, means, width=0.32, color=policyColors[policy], label=policyLabels[policy], linewidth=0)
            axes.errorbar(positions, means, yerr=halves, fmt="none", ecolor=ink["secondary"], elinewidth=1, capsize=3)
            for position, mean, half in zip(positions, means, halves):
                axes.annotate(f"{mean:.2f}", (position, mean + half), xytext=(0, 3), textcoords="offset points",
                              ha="center", va="bottom", fontsize=8, color=ink["secondary"])
                highest = max(highest, mean + half)
        axes.set_ylim(0, max(highest * 1.18, 0.1))
        axes.set_xticks(np.arange(len(keys)))
        axes.set_xticklabels([configLabel(scenario, load, "\n") for scenario, load in keys])
        axes.set_title(title, fontsize=10)
    handles, labels = grid[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.01, 0.995), ncol=2)
    figure.suptitle("Barras: média; hastes: IC95%", x=0.99, y=0.985, ha="right", fontsize=9, color=ink["secondary"])
    figure.tight_layout(rect=(0, 0, 1, 0.93))
    figure.savefig(figureDir / "barsSummary.png", dpi=140)
    plt.close(figure)
    return "barsSummary.png"


def plotFailoverTimes(summary, figureDir):
    sdwan = summary[summary.policy == "sdwan"]
    if sdwan.empty:
        return None
    keys = configKeys(sdwan)
    figure, grid = plt.subplots(1, 2, figsize=(10, 3.8))
    for axes, (metric, title) in zip(grid, (("tFoS", "Failover do SD-WAN: $T_{fo}$ (s)"),
                                            ("tFbS", "Failback do SD-WAN: $T_{fb}$ (s)"))):
        styleAxes(axes)
        highest = 0.0
        for index, (scenario, load) in enumerate(keys):
            values = sdwan[(sdwan.scenario == scenario) & (sdwan.loadMbit == load)][metric].dropna().to_numpy(float)
            if len(values) == 0:
                axes.text(index, 0.05, "sem troca", ha="center", fontsize=9, color=ink["secondary"],
                          transform=blended_transform_factory(axes.transData, axes.transAxes))
                continue
            spread = np.linspace(-0.14, 0.14, len(values)) if len(values) > 1 else np.array([0.0])
            axes.scatter(index + spread, values, s=46, color=policyColors["sdwan"], edgecolor=ink["surface"],
                         linewidth=1.5, zorder=3)
            mean, half, _ = meanCi(values.tolist())
            axes.plot([index - 0.24, index + 0.24], [mean, mean], color=ink["primary"], linewidth=2, zorder=4)
            if not math.isnan(half):
                axes.errorbar([index], [mean], yerr=[half], fmt="none", ecolor=ink["primary"], elinewidth=1,
                              capsize=4, zorder=4)
            axes.text(index + 0.28, mean, f"{mean:.2f}", va="center", fontsize=9, color=ink["secondary"])
            reach = mean if math.isnan(half) else mean + half
            highest = max(highest, float(values.max()), min(reach, 2 * float(values.max())))
        axes.set_ylim(0, max(highest * 1.2, 1.0))
        axes.set_xlim(-0.6, len(keys) - 0.3)
        axes.set_xticks(np.arange(len(keys)))
        axes.set_xticklabels([configLabel(scenario, load, "\n") for scenario, load in keys])
        axes.set_title(title, fontsize=10)
    figure.suptitle("Pontos: execuções; traço: média; hastes: IC95%", x=0.99, y=0.985, ha="right", fontsize=9,
                    color=ink["secondary"])
    figure.tight_layout(rect=(0, 0, 1, 0.95))
    figure.savefig(figureDir / "failoverTimes.png", dpi=140)
    plt.close(figure)
    return "failoverTimes.png"


def markdownTable(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def cell(byConfig, scenario, load, policy, metric, digits=2):
    row = byConfig[(byConfig.scenario == scenario) & (byConfig.loadMbit == load) & (byConfig.policy == policy)]
    if row.empty:
        return "–"
    return formatMeanCi(float(row[f"{metric}Mean"].iloc[0]), float(row[f"{metric}Ci95"].iloc[0]), digits)


def writeReport(reportDir, dataDir, summary, byConfig, tests, figures, metas, skipped):
    meta = metas[0]
    config = meta["config"]
    timeline = config["timeline"]
    counts = summary.groupby(["scenario", "loadMbit", "policy"]).size()
    lines = ["# Estático (tipo MPLS) vs SD-WAN em topologia diamante: resultados", ""]
    lines += ["## 1. Ambiente e backend", "",
              "**Backend usado: Mininet.** Os nós são hosts do Mininet e os enlaces do núcleo são `TCLink`: banda por "
              "`tc htb` e atraso, jitter e perda por `tc netem`. TCP, roteamento, `iperf3` e `ping` são os do kernel "
              "Linux. Não é simulação discreta.", "",
              markdownTable(["Item", "Valor"], [[key, value] for key, value in meta["versions"].items()]
                            + [["controle de congestionamento TCP", config["tcpCongestion"]]]), ""]
    validationPath = dataDir / "validation.json"
    if validationPath.exists():
        validation = json.loads(validationPath.read_text())
        lines += ["## 2. Validação da topologia", "",
                  f"Tolerância: ±{validation['tolerancePct']}%. Resultado geral: "
                  f"**{'dentro' if validation['ok'] else 'fora'} da tolerância**.", "",
                  markdownTable(["Caminho", "RTT medido (ms)", "RTT nominal (ms)", "Erro", "Banda medida (Mbit/s)",
                                 "Banda nominal (Mbit/s)", "Erro", "Vazão TCP (Mbit/s)"],
                                [[item["path"], f"{item['rttMs']:.2f}", f"{item['expectedRttMs']:.0f}",
                                  f"{item['rttErrorPct']:+.1f}%", f"{item['linkRateMbit']:.2f}",
                                  f"{item['expectedRateMbit']:.0f}", f"{item['rateErrorPct']:+.1f}%",
                                  f"{item['tcpGoodputMbit']:.2f}"] for item in validation["paths"]]), ""]
    agent, sla = config["agent"], config["sla"]
    lines += ["## 3. Parâmetros", "",
              f"- Linha do tempo: degradação em $t={timeline['degradeAt']}$ s, recuperação em "
              f"$t={timeline['recoverAt']}$ s, fim em $t={timeline['endAt']}$ s.",
              f"- Repetições por configuração: {int(counts.min())} a {int(counts.max())} "
              f"(total de {len(summary)} execuções válidas).",
              f"- SLA: RTT < {sla['rttMs']} ms e perda < {sla['lossPct']}%, avaliado em janelas de "
              f"{config['measure']['binS']:g} s do `ping` h1→h2.",
              f"- Agente: sondagem a cada {agent['probeIntervalS']:g} s com {agent['pingCount']} pings, janela "
              f"deslizante de {agent['windowProbes']} sondagens, histerese de {agent['hysteresis'] * 100:.0f}%, "
              f"failback após {agent['failbackCount']} sondagens consecutivas dentro do SLA.",
              "- Tráfego: TCP bulk (`iperf3`), com a carga $L$ fixada por pacing no socket (`--fq-rate`).",
              "- $\\eta = \\text{vazão recebida} / L$; $V$ = fração das janelas fora do SLA; "
              "$T_{fo}$ e $T_{fb}$ medidos do log do agente (instante da troca de rota menos o instante do evento).",
              ""]
    keys = configKeys(summary)
    lines += ["## 4. Tabela-resumo (média ± IC95%)", ""]
    rows = []
    for scenario, load in keys:
        for policy in policyLabels:
            if (scenario, load, policy) not in counts.index:
                continue
            rows.append([scenarioLabels.get(scenario, scenario), load, policyLabels[policy],
                         int(counts[(scenario, load, policy)]),
                         cell(byConfig, scenario, load, policy, "degradedSlaViolation"),
                         cell(byConfig, scenario, load, policy, "degradedEta"),
                         cell(byConfig, scenario, load, policy, "degradedRttP95Ms", 1),
                         cell(byConfig, scenario, load, policy, "zeroGoodputS"),
                         cell(byConfig, scenario, load, policy, "tFoS"),
                         cell(byConfig, scenario, load, policy, "tFbS"),
                         cell(byConfig, scenario, load, policy, "routeChanges", 1)])
    lines += [markdownTable(["Cenário", "$L$ (Mbit/s)", "Política", "n", "$V$ na degradação", "$\\eta$ na degradação",
                             "RTT p95 na degradação (ms)", "Tempo sem vazão (s)", "$T_{fo}$ (s)", "$T_{fb}$ (s)",
                             "Trocas de rota"], rows), ""]
    lines += ["### Vazão e violação por fase", ""]
    rows = []
    for scenario, load in keys:
        for policy in policyLabels:
            if (scenario, load, policy) not in counts.index:
                continue
            rows.append([scenarioLabels.get(scenario, scenario), load, policyLabels[policy]]
                        + [cell(byConfig, scenario, load, policy, f"{phase}GoodputMbps") for phase in phaseNames]
                        + [cell(byConfig, scenario, load, policy, f"{phase}SlaViolation") for phase in phaseNames])
    lines += [markdownTable(["Cenário", "$L$", "Política"]
                            + [f"Vazão, {phaseLabels[phase].lower()} (Mbit/s)" for phase in phaseNames]
                            + [f"$V$, {phaseLabels[phase].lower()}" for phase in phaseNames], rows), ""]
    lines += ["### Perdas separadas por causa", ""]
    rows = []
    for scenario, load in keys:
        for policy in policyLabels:
            if (scenario, load, policy) not in counts.index:
                continue
            rows.append([scenarioLabels.get(scenario, scenario), load, policyLabels[policy],
                         cell(byConfig, scenario, load, policy, "degradationDrops", 0),
                         cell(byConfig, scenario, load, policy, "degradationLossPct"),
                         cell(byConfig, scenario, load, policy, "congestionDropsBackup", 0),
                         cell(byConfig, scenario, load, policy, "congestionDropsPremium", 0),
                         cell(byConfig, scenario, load, policy, "congestionLossPct"),
                         cell(byConfig, scenario, load, policy, "retransmits", 0)])
    lines += [markdownTable(["Cenário", "$L$", "Política", "Descartes por degradação (pacotes)",
                             "Perda por degradação (%)", "Descartes por congestionamento no backup (pacotes)",
                             "Descartes por congestionamento no premium (pacotes)",
                             "Perda por congestionamento (%)", "Retransmissões TCP"], rows), ""]
    lines += ["## 5. Comparação pareada (SD-WAN menos estático)", "",
              "Pares formados pelo número da repetição. Tamanho de efeito: correlação rank-bisserial "
              "$r$ dos pares ($-1$ a $1$).", ""]
    rows = []
    for _, test in tests.iterrows():
        rows.append([scenarioLabels.get(test.scenario, test.scenario), test.loadMbit, test.label, int(test.n),
                     formatNumber(test.staticMean), formatNumber(test.sdwanMean),
                     formatMeanCi(test.meanDifference, test.ci95Difference, 3), formatNumber(test.wilcoxonP, 4),
                     formatNumber(test.rankBiserial, 2), formatNumber(test.welchP, 4)])
    lines += [markdownTable(["Cenário", "$L$", "Métrica", "n", "Estático", "SD-WAN", "Diferença média ± IC95%",
                             "Wilcoxon $p$", "$r$", "Welch $p$"], rows), ""]
    lines += ["## 6. Figuras", ""]
    for scenario, load, fileName in figures["timeSeries"]:
        lines += [f"![Séries temporais, {configLabel(scenario, load)}](figs/{fileName})", ""]
    lines += [f"![CDF do RTT na fase de degradação](figs/{figures['cdf']})", "",
              f"![Barras com IC95%](figs/{figures['bars']})", ""]
    if figures["failover"]:
        lines += [f"![Tempos de failover e failback](figs/{figures['failover']})", ""]
    staticRuns, sdwanRuns = summary[summary.policy == "static"], summary[summary.policy == "sdwan"]
    lines += ["## 7. Critérios de aceite", ""]
    if len(staticRuns):
        clean = int((staticRuns.backupForwardPackets == 0).sum())
        lines.append(f"- Estático nunca usa o backup: {clean} de {len(staticRuns)} execuções sem nenhum pacote "
                     "encaminhado pelo R1 ao R3.")
    if len(sdwanRuns):
        lines.append(f"- SD-WAN troca para o backup: {int(sdwanRuns.tFoS.notna().sum())} de {len(sdwanRuns)} execuções.")
        lines.append(f"- SD-WAN volta ao premium: {int(sdwanRuns.tFbS.notna().sum())} de {len(sdwanRuns)} execuções.")
    lines.append("- $T_{fo}$ e $T_{fb}$ vêm dos timestamps do log do agente e do log de eventos.")
    if skipped:
        lines.append(f"- Execuções descartadas por falha: {', '.join(skipped)}.")
    lines += ["", "## 8. Desvios em relação ao documento de projeto", "",
              "- O `netem` com jitter reordena pacotes, e o TCP trata reordenação como sinal de perda.",
              "- O `netem` não recebe seed: o padrão de perda não se repete entre execuções.",
              "- Perda por degradação = descartes do `netem` menos descartes contados pelo `htb` (fila cheia); perda "
              "por congestionamento = descartes contados pelo `htb`.",
              "- Só TCP bulk: sem fluxo UDP de voz e, por isso, sem MOS.",
              "- Sem política por classe de tráfego.",
              "- O baseline (cenário 1) é a fase anterior à degradação e a recuperação (cenário 4) é a fase final de "
              "cada execução, em vez de execuções separadas.",
              "- O controlador troca a rota nas duas bordas (R1 e R4), para que o caminho de volta acompanhe o de ida.",
              f"- Repetições por configuração: {int(counts.min())}, abaixo das 30 pedidas."
              if counts.min() < 30 else "- Repetições por configuração: 30 ou mais.", ""]
    discussionPath = reportDir / "discussao.md"
    lines += ["## 9. Discussão", "",
              discussionPath.read_text().strip() if discussionPath.exists()
              else "_A preencher depois da execução dos experimentos._", ""]
    (reportDir / "relatorio.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataDir", default=str(repoRoot / "data"))
    parser.add_argument("--reportDir", default=str(repoRoot / "report"))
    arguments = parser.parse_args()
    dataDir, reportDir = Path(arguments.dataDir), Path(arguments.reportDir)
    figureDir = reportDir / "figs"
    figureDir.mkdir(parents=True, exist_ok=True)

    rows, seriesByRun, metas, skipped = [], {}, [], []
    for runDir in sorted(path for path in (dataDir / "raw").iterdir() if path.is_dir()):
        try:
            if not json.loads((runDir / "status.json").read_text()).get("ok"):
                raise ValueError("falhou")
            row, series, meta = analyzeRun(runDir)
        except (OSError, ValueError, KeyError) as failure:
            skipped.append(runDir.name)
            print(f"ignorando {runDir.name}: {failure}")
            continue
        rows.append(row)
        seriesByRun[row["runId"]] = series
        metas.append(meta)
    if not rows:
        raise SystemExit("nenhuma execução válida em " + str(dataDir / "raw"))

    summary = pd.DataFrame(rows)
    scenarioOrder = list(metas[0]["config"]["scenarios"])
    summary["scenarioOrder"] = summary.scenario.map(lambda name: scenarioOrder.index(name) if name in scenarioOrder else 99)
    summary["policyOrder"] = summary.policy.map(lambda name: list(policyLabels).index(name))
    summary = summary.sort_values(["scenarioOrder", "loadMbit", "policyOrder", "rep"]).drop(
        columns=["scenarioOrder", "policyOrder"]).reset_index(drop=True)
    byConfig = aggregate(summary)
    tests = pairedTests(summary)
    summary.to_csv(dataDir / "summary.csv", index=False)
    byConfig.to_csv(dataDir / "summaryByConfig.csv", index=False)
    tests.to_csv(dataDir / "stats.csv", index=False)

    setStyle()
    figures = {"timeSeries": plotTimeSeries(summary, seriesByRun, figureDir),
               "cdf": plotCdf(summary, seriesByRun, figureDir),
               "bars": plotBars(summary, figureDir),
               "failover": plotFailoverTimes(summary, figureDir)}
    writeReport(reportDir, dataDir, summary, byConfig, tests, figures, metas, skipped)
    print(f"{len(summary)} execuções analisadas; relatório em {reportDir / 'relatorio.md'}")


if __name__ == "__main__":
    main()
