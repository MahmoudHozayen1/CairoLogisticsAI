"""Benchmark harness for SwiftRoute's route-optimization techniques.

This is the *data-science* core of the project: it empirically compares every
sequencing strategy registered in :data:`app.routing.optimizer.STRATEGIES`
(FIFO, Nearest-Neighbour, 2-opt, Or-opt, NetworkX Christofides and Simulated
Annealing) across many randomly generated delivery scenarios, and reports:

* **Total route distance** (km) — the objective we minimise.
* **Improvement over the FIFO baseline** (%) — how much optimisation buys you.
* **Optimality gap** (%) — distance vs. the *exact* optimum (brute force) on
  small instances where the optimum is computable, so we know how close the
  heuristics get to the best possible tour.
* **Runtime** (ms) — the speed/quality trade-off.

It runs with **no Flask app and no database** — instances are synthetic points
around a service centre, so it is fully reproducible (fixed random seed).

Usage::

    python scripts/benchmark_optimizer.py                  # default sweep
    python scripts/benchmark_optimizer.py --instances 60 --sizes 8 15 25 40
    python scripts/benchmark_optimizer.py --no-plots       # skip matplotlib

Outputs (under ``docs/benchmarks/``):

* ``results.csv``   — one row per (size, instance, strategy).
* ``summary.json``  — aggregated means per strategy and per size.
* ``*.png``         — charts embedded by the report and the slide deck.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import os
import random
import statistics
import sys
import time
from typing import List

# Make the project importable when run as ``python scripts/benchmark_optimizer.py``.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.routing.optimizer import (  # noqa: E402
    STRATEGIES,
    STRATEGY_ORDER,
    haversine_km,
    _route_distance,
)

# Service centre (Maadi, Cairo) — matches the app's default SERVICE_CENTER.
CENTER = [29.9602, 31.2569]
SPREAD_DEG = 0.045  # ~5 km box around the centre
OUT_DIR = os.path.join(ROOT, "docs", "benchmarks")


class _Stop:
    """Minimal stand-in for a Shipment: the strategies only read ``.coords``."""

    __slots__ = ("coords",)

    def __init__(self, coords: List[float]):
        self.coords = coords


# --------------------------------------------------------------------------- #
#  Instance generation + exact optimum
# --------------------------------------------------------------------------- #
def make_instance(n: int, rng: random.Random):
    """A depot (service centre) and ``n`` random drop-off stops around it."""
    stops = [
        _Stop([
            CENTER[0] + rng.uniform(-SPREAD_DEG, SPREAD_DEG),
            CENTER[1] + rng.uniform(-SPREAD_DEG, SPREAD_DEG),
        ])
        for _ in range(n)
    ]
    return CENTER, stops


def exact_optimum(start, stops) -> float:
    """Shortest open route by brute force (only viable for small ``n``)."""
    best = float("inf")
    for perm in itertools.permutations(stops):
        d = _route_distance(start, list(perm))
        if d < best:
            best = d
    return best


# --------------------------------------------------------------------------- #
#  Benchmark sweep
# --------------------------------------------------------------------------- #
def run(instances: int, sizes: List[int], seed: int, exact_max: int):
    rng = random.Random(seed)
    rows = []  # per (size, instance, strategy)
    for n in sizes:
        compute_exact = n <= exact_max
        for inst in range(instances):
            start, stops = make_instance(n, rng)
            opt = exact_optimum(start, stops) if compute_exact else None
            baseline = None
            per_strategy = {}
            for key in STRATEGY_ORDER:
                func = STRATEGIES[key]["func"]
                t0 = time.perf_counter()
                ordered = func(start, stops)
                runtime_ms = (time.perf_counter() - t0) * 1000.0
                dist = _route_distance(start, ordered)
                per_strategy[key] = (dist, runtime_ms)
                if key == "fifo":
                    baseline = dist
            for key, (dist, runtime_ms) in per_strategy.items():
                improvement = (1 - dist / baseline) * 100 if baseline else 0.0
                gap = ((dist - opt) / opt * 100) if opt else None
                rows.append({
                    "size": n,
                    "instance": inst,
                    "strategy": key,
                    "distance_km": round(dist, 4),
                    "improvement_pct": round(improvement, 3),
                    "optimality_gap_pct": round(gap, 3) if gap is not None else "",
                    "runtime_ms": round(runtime_ms, 4),
                })
    return rows


def aggregate(rows):
    """Mean metrics per strategy (overall) and per (size, strategy)."""
    def _mean(values):
        values = [v for v in values if v != "" and v is not None]
        return round(statistics.mean(values), 3) if values else None

    overall = {}
    by_size = {}
    for key in STRATEGY_ORDER:
        krows = [r for r in rows if r["strategy"] == key]
        overall[key] = {
            "label": STRATEGIES[key]["label"],
            "distance_km": _mean([r["distance_km"] for r in krows]),
            "improvement_pct": _mean([r["improvement_pct"] for r in krows]),
            "optimality_gap_pct": _mean([r["optimality_gap_pct"] for r in krows]),
            "runtime_ms": _mean([r["runtime_ms"] for r in krows]),
        }
    sizes = sorted({r["size"] for r in rows})
    for n in sizes:
        by_size[n] = {}
        for key in STRATEGY_ORDER:
            krows = [r for r in rows if r["strategy"] == key and r["size"] == n]
            by_size[n][key] = {
                "distance_km": _mean([r["distance_km"] for r in krows]),
                "improvement_pct": _mean([r["improvement_pct"] for r in krows]),
                "optimality_gap_pct": _mean([r["optimality_gap_pct"] for r in krows]),
                "runtime_ms": _mean([r["runtime_ms"] for r in krows]),
            }
    return {"overall": overall, "by_size": by_size, "sizes": sizes,
            "order": STRATEGY_ORDER}


# --------------------------------------------------------------------------- #
#  Plots (optional — needs matplotlib)
# --------------------------------------------------------------------------- #
def make_plots(rows, summary):
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless / no display
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - optional dependency
        print(f"  (plots skipped: matplotlib unavailable — {exc})")
        return []

    os.makedirs(OUT_DIR, exist_ok=True)
    order = STRATEGY_ORDER
    short = {
        "fifo": "FIFO",
        "nearest": "Nearest\nNeighbour",
        "two_opt": "NN + 2-opt",
        "or_opt": "2-opt +\nOr-opt",
        "christofides": "Christofides",
        "annealing": "Simulated\nAnnealing",
    }
    labels = [short.get(k, STRATEGIES[k]["label"]) for k in order]
    accent = "#0D3B66"
    highlight = "#EE6C4D"
    written = []

    def _bar(values, title, ylabel, fname, fmt="{:.1f}", best="min"):
        vals = [v if v is not None else 0 for v in values]
        fig, ax = plt.subplots(figsize=(8, 4.5))
        target = (min if best == "min" else max)(vals)
        colors = [highlight if v == target else accent for v in vals]
        bars = ax.bar(labels, vals, color=colors)
        ax.set_title(title, fontsize=13, fontweight="bold", color="#1F2933")
        ax.set_ylabel(ylabel)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, v, fmt.format(v),
                    ha="center", va="bottom", fontsize=9)
        fig.tight_layout()
        path = os.path.join(OUT_DIR, fname)
        fig.savefig(path, dpi=130)
        plt.close(fig)
        written.append(path)
        return path

    ov = summary["overall"]
    _bar([ov[k]["distance_km"] for k in order],
         "Average total route distance by technique", "Distance (km)",
         "distance_by_strategy.png", best="min")
    _bar([ov[k]["improvement_pct"] for k in order],
         "Average improvement over the FIFO baseline", "Improvement (%)",
         "improvement_by_strategy.png", fmt="{:.1f}%", best="max")
    _bar([ov[k]["runtime_ms"] for k in order],
         "Average runtime per route by technique", "Runtime (ms)",
         "runtime_by_strategy.png", fmt="{:.2f}", best="min")

    # Optimality gap (only strategies/sizes where the exact optimum was computed).
    gaps = [ov[k]["optimality_gap_pct"] for k in order]
    if any(g is not None for g in gaps):
        _bar(gaps, "Average optimality gap vs. exact optimum (small instances)",
             "Gap above optimum (%)", "optimality_gap.png", fmt="{:.1f}%", best="min")

    # Distance distribution (box plot) at the largest instance size.
    biggest = max(summary["sizes"])
    data = [[r["distance_km"] for r in rows
             if r["strategy"] == k and r["size"] == biggest] for k in order]
    if all(data):
        fig, ax = plt.subplots(figsize=(8, 4.5))
        bp = ax.boxplot(data, tick_labels=labels, patch_artist=True)
        for patch in bp["boxes"]:
            patch.set_facecolor("#CFDDEC")
            patch.set_edgecolor(accent)
        ax.set_title(f"Distance distribution at {biggest} stops",
                     fontsize=13, fontweight="bold", color="#1F2933")
        ax.set_ylabel("Distance (km)")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        fig.tight_layout()
        path = os.path.join(OUT_DIR, "distance_distribution.png")
        fig.savefig(path, dpi=130)
        plt.close(fig)
        written.append(path)

    # Scaling: runtime vs. number of stops (log scale).
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for k in order:
        ys = [summary["by_size"][n][k]["runtime_ms"] for n in summary["sizes"]]
        ax.plot(summary["sizes"], ys, marker="o", label=STRATEGIES[k]["label"])
    ax.set_yscale("log")
    ax.set_title("Runtime scaling with number of stops", fontsize=13,
                 fontweight="bold", color="#1F2933")
    ax.set_xlabel("Stops per route")
    ax.set_ylabel("Runtime (ms, log scale)")
    ax.legend(fontsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    path = os.path.join(OUT_DIR, "runtime_scaling.png")
    fig.savefig(path, dpi=130)
    plt.close(fig)
    written.append(path)

    return written


# --------------------------------------------------------------------------- #
#  Output + CLI
# --------------------------------------------------------------------------- #
def write_outputs(rows, summary):
    os.makedirs(OUT_DIR, exist_ok=True)
    csv_path = os.path.join(OUT_DIR, "results.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    json_path = os.path.join(OUT_DIR, "summary.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return csv_path, json_path


def print_table(summary):
    ov = summary["overall"]
    header = f"{'Technique':28s}{'Dist km':>10s}{'vs FIFO':>10s}{'Opt gap':>10s}{'ms':>10s}"
    print("\n" + header)
    print("-" * len(header))
    for key in STRATEGY_ORDER:
        s = ov[key]
        gap = f"{s['optimality_gap_pct']:.1f}%" if s["optimality_gap_pct"] is not None else "  n/a"
        print(f"{STRATEGIES[key]['label']:28s}"
              f"{s['distance_km']:>10.2f}"
              f"{s['improvement_pct']:>9.1f}%"
              f"{gap:>10s}"
              f"{s['runtime_ms']:>10.2f}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark route-optimization techniques.")
    parser.add_argument("--instances", type=int, default=40,
                        help="random scenarios per route size (default: 40)")
    parser.add_argument("--sizes", type=int, nargs="+", default=[8, 15, 25, 40],
                        help="route sizes (stops per route) to test")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    parser.add_argument("--exact-max", type=int, default=8,
                        help="compute exact optimum for sizes <= this (default: 8)")
    parser.add_argument("--no-plots", action="store_true", help="skip chart generation")
    args = parser.parse_args()

    print(f"Benchmarking {len(STRATEGY_ORDER)} techniques over "
          f"{args.instances} instances x sizes {args.sizes} (seed {args.seed}) ...")
    t0 = time.perf_counter()
    rows = run(args.instances, args.sizes, args.seed, args.exact_max)
    summary = aggregate(rows)
    csv_path, json_path = write_outputs(rows, summary)
    print_table(summary)
    print(f"\nWrote {csv_path}")
    print(f"Wrote {json_path}")
    if not args.no_plots:
        figs = make_plots(rows, summary)
        for p in figs:
            print(f"Wrote {p}")
    print(f"\nDone in {time.perf_counter() - t0:.1f}s.")


if __name__ == "__main__":
    main()
