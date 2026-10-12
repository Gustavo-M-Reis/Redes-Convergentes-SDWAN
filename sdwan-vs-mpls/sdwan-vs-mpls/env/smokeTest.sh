#!/usr/bin/env bash
set -u
repoRoot="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
failures=0
nsA="swsmokea"
nsB="swsmokeb"

report() {
  if [ "$2" -eq 0 ]; then
    echo "OK      $1"
  else
    echo "FALHOU  $1"
    failures=$((failures + 1))
  fi
}

cleanup() {
  ip netns del "$nsA" 2>/dev/null
  ip netns del "$nsB" 2>/dev/null
}

if [ "$(id -u)" -ne 0 ]; then
  echo "precisa de root: sudo bash env/smokeTest.sh"
  exit 1
fi
trap cleanup EXIT
cleanup

for tool in mn mnexec ip tc iperf3 ping ethtool ifconfig python3; do
  command -v "$tool" >/dev/null 2>&1
  report "ferramenta $tool" $?
done
python3 -c "import mininet.net, mininet.link, mininet.clean, mininet.cli" 2>/dev/null
report "modulo python do Mininet" $?
python3 -c "import numpy, scipy, pandas, matplotlib" 2>/dev/null
report "bibliotecas python (numpy, scipy, pandas, matplotlib)" $?

ip netns add "$nsA" && ip netns add "$nsB"
report "criar namespaces de rede" $?
ip link add a0 netns "$nsA" type veth peer name b0 netns "$nsB"
report "criar par veth" $?
ip netns exec "$nsA" tc qdisc add dev a0 root handle 5:0 htb default 1 \
  && ip netns exec "$nsA" tc class add dev a0 parent 5:0 classid 5:1 htb rate 10Mbit burst 15k
report "tc htb" $?
ip netns exec "$nsA" tc qdisc add dev a0 parent 5:1 handle 10: netem delay 10ms limit 100
netemStatus=$?
report "tc netem" $netemStatus
if [ "$netemStatus" -ne 0 ]; then
  echo "        sem sch_netem: tente modprobe sch_netem ou instale linux-modules-extra-$(uname -r)"
fi
ip netns exec "$nsA" tc qdisc change dev a0 parent 5:1 handle 10: netem delay 100ms 20ms loss 5% limit 100
report "tc netem change (delay, jitter, loss)" $?
cleanup

if [ "$failures" -eq 0 ]; then
  python3 "$repoRoot/topo/topo.py" --validate
  report "validacao da topologia no Mininet (banda e atraso)" $?
fi

echo "kernel $(uname -r) | $(ip -V) | $(iperf3 --version | head -1) | $(python3 --version)"
if [ "$failures" -eq 0 ]; then
  echo "smoke test: tudo certo"
else
  echo "smoke test: $failures falha(s)"
fi
exit "$failures"
