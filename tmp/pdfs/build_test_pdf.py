"""Build a deterministic chart extraction fixture PDF."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Patch


OUT = Path("output/pdf/chart_extraction_test_fixture.pdf")


def style(ax, title: str, subtitle: str | None = None) -> None:
    """Apply the fixture's consistent chart styling."""
    ax.set_title(title, loc="left", fontsize=17, fontweight="bold", pad=26)
    if subtitle:
        ax.text(
            0,
            1.005,
            subtitle,
            transform=ax.transAxes,
            fontsize=9,
            color="#64748b",
            va="bottom",
        )
    ax.grid(axis="y", color="#e2e8f0", linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#cbd5e1")
    ax.spines["bottom"].set_color("#cbd5e1")


def page_header(fig, page: int, label: str) -> None:
    """Add a small fixture identifier to each chart page."""
    fig.text(0.06, 0.03, "Chart extraction test fixture | exact values are on the final page", fontsize=8, color="#64748b")
    fig.text(0.94, 0.03, f"Page {page}", fontsize=8, color="#64748b", ha="right")
    fig.text(0.94, 0.965, label, fontsize=8, color="#2563eb", ha="right", va="top", fontweight="bold")


def build() -> None:
    """Render all fixture pages into one PDF."""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with PdfPages(OUT) as pdf:
        metadata = pdf.infodict()
        metadata["Title"] = "Chart extraction test fixture"
        metadata["Author"] = "Figure parser test data"
        metadata["Subject"] = "Deterministic charts with known values"

        # Cover page.
        fig = plt.figure(figsize=(11.69, 8.27), facecolor="#f8fafc")
        fig.text(0.08, 0.73, "Chart extraction", fontsize=34, fontweight="bold", color="#0f172a")
        fig.text(0.08, 0.64, "Deterministic visual test fixture", fontsize=22, color="#2563eb")
        fig.text(
            0.08,
            0.48,
            "This PDF contains deliberately labeled charts for comparing\n"
            "Docling classification, VLM observations, formatting, and SQL rows.\n"
            "All source values are printed on the charts and repeated on the final page.",
            fontsize=14,
            color="#334155",
            linespacing=1.7,
        )
        fig.text(0.08, 0.16, "Fixture ID: chart-extraction-test-v1", fontsize=11, color="#64748b")
        pdf.savefig(fig, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)

        # Bar chart with two series and explicit labels.
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        months = ["Jan", "Feb", "Mar", "Apr", "May"]
        north = [120, 150, 135, 180, 210]
        south = [95, 125, 145, 155, 190]
        x = range(len(months))
        width = 0.36
        bars_a = ax.bar([i - width / 2 for i in x], north, width, label="North", color="#2563eb")
        bars_b = ax.bar([i + width / 2 for i in x], south, width, label="South", color="#f97316")
        for bars in (bars_a, bars_b):
            ax.bar_label(bars, padding=3, fontsize=9)
        ax.set_xticks(list(x), months)
        ax.set_ylabel("Revenue (thousand USD)")
        ax.set_ylim(0, 245)
        ax.legend(frameon=False, ncols=2, loc="upper left")
        style(ax, "Monthly revenue by region", "Grouped bar chart - values are in thousand USD")
        page_header(fig, 2, "BAR_CHART")
        fig.tight_layout(rect=(0.04, 0.07, 0.98, 0.94))
        pdf.savefig(fig)
        plt.close(fig)

        # Line chart with a secondary series and a logarithmic-looking but linear scale.
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        years = [2020, 2021, 2022, 2023, 2024, 2025]
        observed = [42, 48, 57, 55, 68, 76]
        target = [40, 45, 50, 58, 65, 72]
        ax.plot(years, observed, marker="o", linewidth=2.6, color="#2563eb", label="Observed")
        ax.plot(years, target, marker="s", linewidth=2.2, linestyle="--", color="#16a34a", label="Target")
        for year, value in zip(years, observed):
            ax.annotate(str(value), (year, value), textcoords="offset points", xytext=(0, 9), ha="center", fontsize=9)
        ax.set_ylabel("Index (base year = 2020)")
        ax.set_xlabel("Year")
        ax.set_xticks(years)
        ax.set_ylim(30, 85)
        ax.legend(frameon=False, ncols=2, loc="upper left")
        style(ax, "Performance index over time", "Line chart - observed values and planned target")
        page_header(fig, 3, "LINE_CHART")
        fig.tight_layout(rect=(0.04, 0.07, 0.98, 0.94))
        pdf.savefig(fig)
        plt.close(fig)

        # Pie chart.
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        labels = ["Product A", "Product B", "Product C", "Other"]
        shares = [40, 30, 20, 10]
        colors = ["#2563eb", "#f97316", "#16a34a", "#94a3b8"]
        wedges, texts, autotexts = ax.pie(
            shares,
            labels=labels,
            colors=colors,
            autopct="%1.0f%%",
            startangle=90,
            pctdistance=0.72,
            textprops={"fontsize": 11},
            wedgeprops={"linewidth": 2, "edgecolor": "white"},
        )
        for text in autotexts:
            text.set_color("white")
            text.set_fontweight("bold")
        ax.set_title("2025 market share", loc="left", fontsize=17, fontweight="bold", pad=26)
        ax.text(0, 1.005, "Pie chart - percentages sum to 100%", transform=ax.transAxes, fontsize=9, color="#64748b", va="bottom")
        page_header(fig, 4, "PIE_CHART")
        fig.tight_layout(rect=(0.04, 0.07, 0.98, 0.94))
        pdf.savefig(fig)
        plt.close(fig)

        # Scatter plot with two groups.
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        group_a_x = [1, 2, 3, 4, 5, 6]
        group_a_y = [2.1, 2.8, 3.6, 4.2, 5.1, 5.8]
        group_b_x = [1, 2, 3, 4, 5, 6]
        group_b_y = [5.9, 5.2, 4.8, 4.0, 3.3, 2.7]
        ax.scatter(group_a_x, group_a_y, s=80, color="#2563eb", label="Treatment")
        ax.scatter(group_b_x, group_b_y, s=80, color="#dc2626", marker="^", label="Control")
        ax.set_xlabel("Dose (mg)")
        ax.set_ylabel("Response (units)")
        ax.set_xlim(0.5, 6.5)
        ax.set_ylim(0, 7)
        ax.set_xticks(range(1, 7))
        ax.legend(frameon=False, ncols=2, loc="upper right")
        style(ax, "Dose and response", "Scatter plot - two labeled cohorts")
        page_header(fig, 5, "SCATTER_PLOT")
        fig.tight_layout(rect=(0.04, 0.07, 0.98, 0.94))
        pdf.savefig(fig)
        plt.close(fig)

        # Box plot.
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        samples = [
            [12, 14, 15, 15, 16, 18, 21],
            [18, 19, 20, 22, 24, 25, 29],
            [9, 11, 11, 13, 14, 15, 17],
        ]
        bp = ax.boxplot(samples, patch_artist=True, labels=["Method A", "Method B", "Method C"], showmeans=True)
        for patch, color in zip(bp["boxes"], ["#bfdbfe", "#fed7aa", "#bbf7d0"]):
            patch.set_facecolor(color)
            patch.set_edgecolor("#475569")
        ax.set_ylabel("Latency (ms)")
        ax.set_ylim(0, 35)
        style(ax, "Latency distribution by method", "Box plot - mean markers are shown as black triangles")
        page_header(fig, 6, "BOX_PLOT")
        fig.tight_layout(rect=(0.04, 0.07, 0.98, 0.94))
        pdf.savefig(fig)
        plt.close(fig)

        # Heatmap.
        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        matrix = [[1, 2, 3, 4], [2, 4, 6, 8], [3, 6, 9, 12]]
        im = ax.imshow(matrix, cmap="Blues", vmin=0, vmax=12)
        ax.set_xticks(range(4), ["Q1", "Q2", "Q3", "Q4"])
        ax.set_yticks(range(3), ["North", "Central", "South"])
        for row in range(3):
            for col in range(4):
                ax.text(col, row, str(matrix[row][col]), ha="center", va="center", color="white" if matrix[row][col] > 6 else "#0f172a", fontsize=12, fontweight="bold")
        ax.set_xlabel("Quarter")
        ax.set_ylabel("Region")
        fig.colorbar(im, ax=ax, label="Score")
        style(ax, "Regional score matrix", "Heatmap - integer cell values")
        page_header(fig, 7, "HEATMAP")
        fig.tight_layout(rect=(0.04, 0.07, 0.98, 0.94))
        pdf.savefig(fig)
        plt.close(fig)

        # Mixed panel: bar plus line, useful for multi-panel and dual-series extraction.
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.69, 8.27))
        categories = ["Low", "Medium", "High"]
        counts = [18, 31, 24]
        ax1.bar(categories, counts, color="#7c3aed")
        ax1.set_ylabel("Count")
        ax1.bar_label(ax1.containers[0], padding=3)
        style(ax1, "Panel A: category counts")
        ax1.text(0, 1.02, "Bar chart", transform=ax1.transAxes, fontsize=9, color="#64748b", va="bottom")
        weeks = [1, 2, 3, 4, 5]
        rate = [0.22, 0.28, 0.31, 0.37, 0.41]
        ax2.plot(weeks, rate, marker="o", color="#0891b2", linewidth=2.6)
        ax2.set_xlabel("Week")
        ax2.set_ylabel("Rate")
        ax2.set_xticks(weeks)
        ax2.set_ylim(0, 0.5)
        for week, value in zip(weeks, rate):
            ax2.annotate(f"{value:.2f}", (week, value), textcoords="offset points", xytext=(0, 9), ha="center", fontsize=9)
        style(ax2, "Panel B: weekly rate")
        ax2.text(0, 1.02, "Line chart", transform=ax2.transAxes, fontsize=9, color="#64748b", va="bottom")
        fig.suptitle("Operational summary", x=0.06, ha="left", fontsize=18, fontweight="bold")
        page_header(fig, 8, "MIXED_PANEL")
        fig.tight_layout(rect=(0.04, 0.07, 0.98, 0.90))
        pdf.savefig(fig)
        plt.close(fig)

        # Ground truth page, intentionally plain and searchable.
        fig = plt.figure(figsize=(11.69, 8.27), facecolor="white")
        fig.text(0.07, 0.93, "Ground truth for comparison", fontsize=23, fontweight="bold", color="#0f172a")
        fig.text(0.07, 0.885, "Use this page to compare normalized formatter output with the printed chart values.", fontsize=11, color="#475569")
        truth = [
            ("BAR_CHART", "Jan North=120, South=95; Feb North=150, South=125; Mar North=135, South=145; Apr North=180, South=155; May North=210, South=190."),
            ("LINE_CHART", "Observed: 2020=42, 2021=48, 2022=57, 2023=55, 2024=68, 2025=76. Target: 2020=40, 2021=45, 2022=50, 2023=58, 2024=65, 2025=72."),
            ("PIE_CHART", "Product A=40%, Product B=30%, Product C=20%, Other=10%."),
            ("SCATTER_PLOT", "Treatment points: (1,2.1), (2,2.8), (3,3.6), (4,4.2), (5,5.1), (6,5.8). Control points: (1,5.9), (2,5.2), (3,4.8), (4,4.0), (5,3.3), (6,2.7)."),
            ("BOX_PLOT", "Method A samples: 12,14,15,15,16,18,21. Method B: 18,19,20,22,24,25,29. Method C: 9,11,11,13,14,15,17."),
            ("HEATMAP", "Rows North/Central/South; columns Q1/Q2/Q3/Q4: North=[1,2,3,4], Central=[2,4,6,8], South=[3,6,9,12]."),
            ("MIXED_PANEL", "Panel A counts: Low=18, Medium=31, High=24. Panel B rates: Week 1=0.22, 2=0.28, 3=0.31, 4=0.37, 5=0.41."),
        ]
        y = 0.82
        for label, value in truth:
            fig.text(0.07, y, label, fontsize=10, fontweight="bold", color="#2563eb", va="top")
            fig.text(0.20, y, value, fontsize=9.5, color="#1e293b", va="top", wrap=True)
            y -= 0.105
        fig.text(0.07, 0.07, "No values are implied beyond those printed above. Preserve uncertainty or missing fields as null.", fontsize=9, color="#64748b")
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)


if __name__ == "__main__":
    build()
    print(OUT)
