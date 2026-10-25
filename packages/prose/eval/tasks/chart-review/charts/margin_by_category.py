import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

df = pd.read_csv("data/margin.csv").sort_values("category")

fig, ax = plt.subplots(figsize=(9, 5.5))
colors = plt.cm.jet(np.linspace(0, 1, len(df)))
bars = ax.bar(df["category"], df["gross_margin_pct"], color=colors, edgecolor="black", linewidth=1.5,
              label="Gross margin %")
for b, v in zip(bars, df["gross_margin_pct"]):
    ax.text(b.get_x() + b.get_width() / 2, v + 0.3, f"{v:.4f}", ha="center", fontsize=8)
ax.set_ylim(30, 46)
ax.set_ylabel("Gross margin %")
ax.grid(True, which="both", linestyle="-", linewidth=0.8)
ax.minorticks_on()

ax2 = ax.twinx()
ax2.plot(df["category"], df["revenue_meur"], color="red", marker="o", linewidth=3, label="Revenue (mEUR)")
ax2.set_ylabel("Revenue (mEUR)")

fig.legend(loc="lower center", ncol=2, frameon=True, shadow=True)
plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
ax.set_title("Gross margin by category", fontsize=16, fontweight="bold")
fig.patch.set_facecolor("#eef3fb")
plt.tight_layout(rect=(0, 0.08, 1, 1))
plt.savefig("charts/margin_by_category.png", dpi=100)
