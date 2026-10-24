"""Pool the replay benchmark worlds, print tables, save plots and CSVs ("before calibration" reference).

Error and coverage of the simulation engine's median and bands at fixed horizons, against a
flat "balance stays where it is" baseline, plus the payment-timing model's range for one buyer.
Pairs within a world overlap in time (consecutive as_of days), so n overstates independent evidence.

    uv run --project intelligence --with matplotlib python eval/models/replay/report.py SRC_DIR eval/models/replay
"""
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import polars as pl

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
dst.mkdir(parents=True, exist_ok=True)
SEEDS = (0, 1, 2)
H = (1, 7, 14, 30, 60, 90)
BLUE, ORANGE, GRAY = "#2a78d6", "#eb6834", "#8a8984"
SURFACE, INK, INK2 = "#fcfcfb", "#0b0b0b", "#52514e"
plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "axes.edgecolor": "#d6d5d0",
                     "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "text.color": INK,
                     "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
                     "grid.color": "#ebeae6", "grid.linewidth": 0.8, "font.size": 10})

rows = []
for s in SEEDS:
    truth = pl.read_parquet(src / f"truth_bal_{s}.parquet").select("date", pl.col("closing_paise").alias("actual"))
    bq = pl.read_parquet(src / f"bq_{s}.parquet").join(truth, on="date")
    start = truth.rename({"date": "as_of", "actual": "flat"})
    rows.append(bq.join(start, on="as_of").with_columns(
        (pl.col("date") - pl.col("as_of")).dt.total_days().alias("h"), pl.lit(s).alias("world")))
df = pl.concat(rows).filter(pl.col("h").is_in(H))


def agg(g):
    return g.agg(
        pl.len().alias("n"),
        ((pl.col("q50") - pl.col("actual")) / 1e7).mean().alias("bias_lakh"),
        ((pl.col("q50") - pl.col("actual")).abs() / 1e7).mean().alias("mae_lakh"),
        ((pl.col("flat") - pl.col("actual")).abs() / 1e7).mean().alias("flat_mae_lakh"),
        pl.col("actual").is_between(pl.col("q25"), pl.col("q75")).mean().alias("cov50"),
        pl.col("actual").is_between(pl.col("q05"), pl.col("q95")).mean().alias("cov90"),
    )


pooled = agg(df.group_by("h")).sort("h").with_columns((pl.col("mae_lakh") / pl.col("flat_mae_lakh")).alias("mae_ratio"))
per_world = agg(df.group_by("world", "h")).sort("world", "h")
with pl.Config(tbl_rows=-1, tbl_cols=-1, float_precision=3, tbl_width_chars=200):
    print("POOLED (3 worlds)"); print(pooled)
    print("PER WORLD"); print(per_world)
pooled.write_csv(dst / "replay_scores_pooled.csv")
per_world.write_csv(dst / "replay_scores_per_world.csv")

# 1. error by horizon
fig, ax = plt.subplots(figsize=(7, 4))
x = pooled["h"].to_list()
ax.plot(x, pooled["mae_lakh"], color=BLUE, lw=2, marker="o", ms=6, label="Forecast median (q50)")
ax.plot(x, pooled["flat_mae_lakh"], color=ORANGE, lw=2, marker="o", ms=6, label="Flat baseline (balance today)")
for xi, n in zip(x, pooled["n"]):
    ax.annotate(f"n={n}", (xi, 0), xytext=(0, 4), textcoords="offset points", ha="center", color=INK2, fontsize=8)
ax.set_xticks(x); ax.set_xlabel("Horizon (days)"); ax.set_ylabel("Mean absolute error (Rs lakh)")
ax.set_ylim(bottom=0); ax.legend(frameon=False, loc="center right")
ax.set_title("Balance forecast error by horizon (before calibration)\n3 borderline worlds x 60 daily steps",
             loc="left", fontsize=11)
fig.tight_layout(); fig.savefig(dst / "replay_error_by_horizon.png", dpi=160); plt.close(fig)

# 2. coverage
fig, ax = plt.subplots(figsize=(7, 4))
ax.axhline(0.9, color=GRAY, lw=1, ls="--"); ax.axhline(0.5, color=GRAY, lw=1, ls="--")
ax.text(x[-1], 0.9, " nominal 90%", va="bottom", ha="right", color=INK2, fontsize=8)
ax.text(x[-1], 0.5, " nominal 50%", va="bottom", ha="right", color=INK2, fontsize=8)
ax.plot(x, pooled["cov90"], color=ORANGE, lw=2, marker="o", ms=6, label="90% band (q05-q95)")
ax.plot(x, pooled["cov50"], color=BLUE, lw=2, marker="o", ms=6, label="50% band (q25-q75)")
ax.set_xticks(x); ax.set_ylim(0, 1.02); ax.set_xlabel("Horizon (days)"); ax.set_ylabel("Share of actuals inside band")
ax.legend(frameon=False, loc="lower left")
ax.set_title("Interval coverage by horizon (before calibration, pooled; n as in error chart)", loc="left", fontsize=11)
fig.tight_layout(); fig.savefig(dst / "replay_coverage_by_horizon.png", dpi=160); plt.close(fig)

# 3. buyer payment-time range
fig, axes = plt.subplots(1, 3, figsize=(11, 3.6), sharey=False)
summary = []
for ax, s in zip(axes, SEEDS):
    b = pl.read_parquet(src / f"buyer_{s}.parquet")
    d = b["as_of"].to_list()
    ax.fill_between(d, b["weeks_q05"], b["weeks_q95"], color=BLUE, alpha=0.18, lw=0, label="90% range")
    ax.plot(d, b["weeks_q50"], color=BLUE, lw=2, label="Median")
    ax.axhline(b["true_weeks"][0], color=GRAY, lw=1, ls="--", label="Planted truth")
    ax.set_title(f"World {s}: buyer {b['buyer_id'][0][:12]}", loc="left", fontsize=10)
    ax.set_ylabel("Weeks to pay once due (1/p)") if s == 0 else None
    ax.tick_params(axis="x", rotation=30, labelsize=8)
    ax.annotate(f"{b['n_paid_labels'][0]} paid labels", (d[0], b["weeks_q95"][0]), xytext=(2, 4),
                textcoords="offset points", fontsize=8, color=INK2)
    ax.annotate(f"{b['n_paid_labels'][-1]}", (d[-1], b["weeks_q95"][-1]), xytext=(-2, 4),
                textcoords="offset points", fontsize=8, color=INK2, ha="right")
    summary.append({"world": s, "buyer_id": b["buyer_id"][0], "true_weeks": b["true_weeks"][0],
                    "labels_day1": b["n_paid_labels"][0], "labels_day60": b["n_paid_labels"][-1],
                    "q05_day1": b["weeks_q05"][0], "q95_day1": b["weeks_q95"][0], "q50_day1": b["weeks_q50"][0],
                    "q05_day60": b["weeks_q05"][-1], "q95_day60": b["weeks_q95"][-1], "q50_day60": b["weeks_q50"][-1],
                    "width_day1": b["weeks_q95"][0] - b["weeks_q05"][0],
                    "width_day60": b["weeks_q95"][-1] - b["weeks_q05"][-1],
                    "truth_inside_day60": b["weeks_q05"][-1] <= b["true_weeks"][0] <= b["weeks_q95"][-1]})
h_, l_ = axes[0].get_legend_handles_labels()
fig.legend(h_, l_, frameon=False, fontsize=8, loc="upper right", ncol=3)
fig.suptitle("Tracked buyer's payment-time range as labels arrive (before calibration, 60 daily steps)",
             x=0.01, ha="left", fontsize=11)
fig.tight_layout(rect=(0, 0, 1, 0.97)); fig.savefig(dst / "replay_buyer_range.png", dpi=160); plt.close(fig)
bs = pl.DataFrame(summary)
with pl.Config(tbl_cols=-1, float_precision=2, tbl_width_chars=250):
    print("BUYER"); print(bs)
bs.write_csv(dst / "replay_buyer_range.csv")
