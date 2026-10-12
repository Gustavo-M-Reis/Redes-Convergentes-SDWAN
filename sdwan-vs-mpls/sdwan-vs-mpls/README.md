# sdwan-vs-mpls

Emulação em Mininet de uma topologia em diamante para comparar duas políticas de encaminhamento sob degradação de link:

- **Estático (tipo MPLS):** tráfego preso ao caminho premium.
- **SD-WAN:** agente que sonda os dois caminhos e troca a rota quando o SLA é violado, com histerese e failback.

```
        ┌── R2: PREMIUM 100 Mbit/s, 10 ms por enlace ──┐
H1 ── R1                                                R4 ── H2
        └── R3: BACKUP   10 Mbit/s, 20 ms por enlace ──┘
```

## Backend

**Mininet.** H1, H2 e R1 a R4 são hosts do Mininet (os roteadores com `ip_forward`), sem switches nem controlador. Os quatro enlaces do núcleo são `TCLink`: banda por `tc htb` e atraso, jitter e perda por `tc netem`. TCP, roteamento, `iperf3` e `ping` são os do kernel Linux.

**Estado do código:** esta versão passou só por checagem de sintaxe. Ela ainda não foi executada, porque o ambiente em que foi escrita não tem `netem`. Rode `env/smokeTest.sh` antes de tudo.

## Como rodar

Requer Linux com `sch_netem`, root e acesso a `apt`/`pip` na primeira vez.

```bash
sudo bash env/setup.sh
sudo python3 run/runAll.py
```

O segundo comando faz a limpeza equivalente a `mn -c`, valida a topologia, roda todas as execuções em série e gera a análise e o relatório. Opções:

| Opção | Efeito |
|---|---|
| `--quick` | Passada curta de verificação: linha do tempo reduzida, 1 repetição, saída em `data/check/` |
| `--reps N` | Repetições por configuração (padrão em `config.json`: 10) |
| `--scenarios degrade,cut` | Filtra cenários |
| `--policies static,sdwan` | Filtra políticas |
| `--loads 5,50` | Filtra cargas (Mbit/s) |
| `--force` | Refaz execuções já concluídas (o padrão é retomar de onde parou) |
| `--skipValidation` | Pula a validação de banda e atraso |
| `--noAnalysis` | Não roda a análise ao final |

Só a análise, sem root:

```bash
python3 analysis/analyze.py
```

Uso manual, com o prompt do Mininet em um terminal e a degradação em outro:

```bash
sudo python3 topo/topo.py --validate
sudo python3 topo/topo.py --cli
sudo python3 scenarios/degrade.py --scenario degrade
sudo python3 scenarios/degrade.py --scenario restore
sudo python3 topo/topo.py --down
```

## Desenho do experimento

Cada execução dura 55 s: baseline até $t=10$ s, degradação do enlace R1–R2 de $t=10$ s a $t=30$ s, recuperação até o fim. Uma rede Mininet nova é criada para cada execução.

| Cenário | `netem` no R1–R2 durante a degradação |
|---|---|
| `degrade` | `delay 100ms 20ms loss 5%` na interface do R1 (sentido R1→R2) |
| `cut` | `loss 100%` nas interfaces do R1 e do R2 |

Fatores: 2 cenários × 2 políticas × 2 cargas ($L = 5$ e $50$ Mbit/s) × 10 repetições = 80 execuções, cerca de 85 min em série.

Tráfego: TCP bulk com `iperf3`, CUBIC, e a carga $L$ fixada por pacing no socket (`--fq-rate`). RTT contínuo com `ping -D` de H1 para H2 a cada 100 ms.

### Agente SD-WAN

- A cada 1 s, `ping -c5 -i0.2` do R1 para o R4 por cada caminho.
- Janela deslizante das últimas 10 sondagens (50 pings) para RTT médio e perda.
- SLA: RTT < 150 ms e perda < 1%.
- Failover: premium viola o SLA e o backup está dentro do SLA ou é melhor por margem de 20%.
- Failback: 5 sondagens consecutivas com a janela do premium dentro do SLA.
- A troca é aplicada nas duas bordas (R1 e R4), para o caminho de volta acompanhar o de ida.

### Métricas

- $\eta = \dfrac{\text{vazão recebida}}{L}$
- $V$: fração das janelas de 1 s fora do SLA (perda ≥ 1% ou RTT médio ≥ 150 ms)
- $T_{fo}$ e $T_{fb}$: instante da troca de rota no log do agente menos o instante do evento
- RTT (média, p95, p99), jitter, perda, tempo sem vazão, retransmissões, trocas de rota
- Perda por degradação: descartes do `netem` menos os descartes contados pelo `htb`
- Perda por congestionamento: descartes contados pelo `htb` (fila cheia)

## Estrutura

```
sdwan-vs-mpls/
├── config.json     parâmetros do experimento
├── env/            setup.sh, smokeTest.sh
├── topo/           topo.py (rede Mininet, netem, validação)
├── agent/          sdwanAgent.py
├── scenarios/      degrade.py
├── run/            runAll.py (orquestrador)
├── analysis/       analyze.py (métricas, estatística, gráficos, relatório)
├── data/raw/       uma pasta por execução
└── report/         relatorio.md e figs/
```

## Saídas

Por execução, em `data/raw/<runId>/`:

| Arquivo | Conteúdo |
|---|---|
| `iperfServer.json` | bytes recebidos por intervalo de 0,5 s |
| `iperfClient.log` | saída do cliente com timestamp por linha (retransmissões, cwnd) |
| `ping.log` | RTT com timestamp |
| `agent.log` | sondagens, decisões e trocas de rota (JSON por linha; só SD-WAN) |
| `events.json` | instantes de início do fluxo, degradação e recuperação |
| `tcStats.json` | contadores de `htb` e `netem` por interface do núcleo |
| `meta.json` | parâmetros e versões |

Agregados: `data/summary.csv` (uma linha por execução), `data/summaryByConfig.csv` (média e IC95%), `data/stats.csv` (Wilcoxon pareado, rank-bisserial, Welch), `data/validation.json`, `report/relatorio.md` e `report/figs/`.

O texto da seção de discussão do relatório vem de `report/discussao.md`, se existir.

## Detalhes de implementação

- Os comandos dentro dos nós rodam por `ip netns exec`: `topo.py` cria em `/var/run/netns/` um link `sw<nó>` para o namespace de cada host do Mininet e o remove ao final.
- O `limit` do `netem` é calculado para comportar 50 ms de fila mais os pacotes retidos pelo atraso.
- A degradação usa `tc qdisc change` no `netem` criado pelo `TCLink` (`parent 5:1 handle 10:`), o que preserva os contadores.
- TSO, GSO e GRO ficam desligados nas interfaces, para que cada pacote na fila seja um segmento.
- ARP fixo (vizinhos permanentes) nos enlaces do núcleo.

## Desvios em relação ao documento de projeto

- Só TCP bulk: sem UDP de voz e sem MOS.
- Sem política por classe de tráfego.
- Baseline e recuperação são fases de cada execução, não cenários separados.
- 10 repetições por configuração em vez de 30 (ajustável com `--reps`).
- O `netem` com jitter reordena pacotes, e o TCP trata reordenação como sinal de perda.
- O `netem` não recebe seed: o padrão de perda não se repete entre execuções; os pares do teste de Wilcoxon são formados pelo número da repetição.
