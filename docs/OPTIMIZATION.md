# Route Optimization Benchmark

This note documents the data-science comparison behind SwiftRoute's route
optimizer. The goal is to show that the route planner is not just drawing nicer
lines on a map; it measurably reduces courier distance while preserving a clear
runtime trade-off between simple heuristics and stronger graph/metaheuristic
techniques.

## Techniques Compared

The benchmark evaluates every sequencing strategy registered in
`app/routing/optimizer.py`:

| Key | Technique | Role in the comparison |
| --- | --- | --- |
| `fifo` | As received | Baseline: visit stops in creation order. |
| `nearest` | Nearest Neighbour | Fast greedy heuristic. |
| `two_opt` | Nearest Neighbour + 2-opt | Local search that removes crossing/expensive edges. |
| `or_opt` | 2-opt + Or-opt | More thorough local search using short segment relocations. |
| `christofides` | Christofides (NetworkX) | Metric-TSP approximation with a 1.5x cycle guarantee; adapted to open courier routes. |
| `annealing` | Simulated Annealing (NetworkX) | Metaheuristic seeded from a greedy tour. |

The NetworkX-backed strategies are graceful optional enhancements. If NetworkX is
missing, the app falls back to the pure-Python 2-opt route sequence so dispatch
still works.

## Methodology

Run the benchmark with:

```bash
python scripts/benchmark_optimizer.py
```

The script generates synthetic delivery scenarios around the default Maadi
service center. It uses a fixed random seed so results are reproducible.

Default sweep:

| Parameter | Value |
| --- | --- |
| Random seed | `42` |
| Scenarios per size | `40` |
| Stops per route | `8`, `15`, `25`, `40` |
| Exact optimum | Brute force for routes with up to `8` stops |

Metrics:

| Metric | Meaning |
| --- | --- |
| Distance (km) | Total open-route distance from depot through all stops. |
| Improvement vs FIFO | Percent distance saved compared with creation-order dispatch. |
| Optimality gap | Percent above exact optimum, only where brute force is feasible. |
| Runtime (ms) | Wall-clock time spent sequencing a route. |

Outputs are written to `docs/benchmarks/`:

| File | Purpose |
| --- | --- |
| `results.csv` | Per-instance raw data. |
| `summary.json` | Aggregated means by strategy and route size. |
| `distance_by_strategy.png` | Average distance comparison. |
| `improvement_by_strategy.png` | Savings over FIFO baseline. |
| `optimality_gap.png` | Gap to exact optimum on small instances. |
| `runtime_by_strategy.png` | Average runtime comparison. |
| `runtime_scaling.png` | Runtime growth as route size increases. |
| `distance_distribution.png` | Distance spread for the largest routes. |

## Results

Current generated summary:

| Technique | Avg distance km | Improvement vs FIFO | Optimality gap | Runtime ms |
| --- | ---: | ---: | ---: | ---: |
| As received (FIFO) | 105.756 | 0.000% | 84.979% | 0.002 |
| Nearest Neighbour | 37.003 | 58.303% | 7.574% | 0.162 |
| Nearest Neighbour + 2-opt | 33.800 | 61.529% | 0.936% | 15.594 |
| 2-opt + Or-opt (thorough) | 33.425 | 61.921% | 0.098% | 54.296 |
| Christofides (NetworkX) | 37.292 | 57.701% | 9.277% | 7.426 |
| Simulated Annealing (NetworkX) | 33.800 | 61.529% | 0.936% | 19.966 |

The main finding is that all optimization techniques beat FIFO by a wide margin,
with the strongest local/metaheuristic approaches saving about 61-62% distance on
average. On exact 8-stop instances, Or-opt is closest to the optimum at about
0.1% above optimal, while 2-opt and simulated annealing are below 1% above
optimal.

Christofides is useful as a graph-theory benchmark and scales well, but its
classic guarantee is for a closed TSP cycle. SwiftRoute couriers run open routes
that do not return to the hub, so the implementation cuts the depot-anchored
cycle in the better orientation. It still trails the tuned local-search methods
on this workload, which is a useful honest comparison for a defense.

## Defense Takeaway

For daily dispatch, `two_opt` remains the default because it is strong and
predictable. `or_opt` is the best quality mode when a dispatcher can spend a few
extra milliseconds, and the NetworkX strategies broaden the thesis comparison
with a formal approximation algorithm and a metaheuristic baseline. The generated
plots provide the presentation-ready evidence: savings, near-optimality, and
runtime scaling.
