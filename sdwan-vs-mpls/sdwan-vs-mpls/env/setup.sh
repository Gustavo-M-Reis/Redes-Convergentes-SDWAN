#!/usr/bin/env bash
set -euo pipefail
repoRoot="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then
  echo "precisa de root: sudo bash env/setup.sh"
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update || true
apt-get install -y --no-install-recommends mininet iproute2 iperf3 iputils-ping ethtool net-tools python3 python3-pip
if ! modprobe sch_netem 2>/dev/null; then
  apt-get install -y "linux-modules-extra-$(uname -r)" || true
  modprobe sch_netem 2>/dev/null || true
fi
modprobe sch_htb 2>/dev/null || true
if ! python3 -c "import numpy, scipy, pandas, matplotlib" 2>/dev/null; then
  pip3 install --break-system-packages numpy scipy pandas matplotlib || pip3 install numpy scipy pandas matplotlib
fi
bash "$repoRoot/env/smokeTest.sh"
