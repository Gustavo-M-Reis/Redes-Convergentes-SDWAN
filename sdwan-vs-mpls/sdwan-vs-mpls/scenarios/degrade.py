import argparse
import sys
from pathlib import Path

repoRoot = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repoRoot))

from topo import topo


def scenarioParams(config, scenarioName):
    link = topo.linkByName(config["degradedLink"])
    nominal = topo.nominalParams(config, link)
    scenario = config["scenarios"][scenarioName]
    degraded = {"delayMs": scenario["delayMs"], "jitterMs": scenario["jitterMs"], "lossPct": scenario["lossPct"]}
    return degraded, degraded if scenario["bothDirections"] else nominal


def degrade(config, scenarioName):
    ab, ba = scenarioParams(config, scenarioName)
    return topo.applyLinkState(config, config["degradedLink"], ab, ba)


def restore(config):
    nominal = topo.nominalParams(config, topo.linkByName(config["degradedLink"]))
    return topo.applyLinkState(config, config["degradedLink"], nominal, nominal)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--config", default=None)
    arguments = parser.parse_args()
    config = topo.loadConfig(arguments.config)
    if arguments.scenario == "restore":
        appliedAt = restore(config)
    elif arguments.scenario in config["scenarios"]:
        appliedAt = degrade(config, arguments.scenario)
    else:
        sys.exit(f"cenario desconhecido: {arguments.scenario}")
    print(f"{arguments.scenario} aplicado em {appliedAt:.6f}")


if __name__ == "__main__":
    main()
