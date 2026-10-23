
import duckdb
import pandas
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

time_bucket_size = 1
def main():
    con = duckdb.connect()
    MARKETS = "data/two_team_markets_clean.parquet"
    TRADES = "data/two_team_trades_clean.parquet"
    # determine which team was being implicitly bet on for each trade, and through which route (yes/no)
    # the 3 - g.side_idx inverts the team in question (represented as 1 or 2) if the taker took the no route
    con.execute(f"""--sql
    CREATE TABLE trades_routed AS
    SELECT
        g.event_ticker AS event,
        CASE WHEN t.taker_side = 'yes' THEN g.side_idx ELSE 3 - g.side_idx END AS team,
        t.taker_side AS route,
        CASE WHEN t.taker_side = 'yes' THEN t.yes_price ELSE t.no_price END AS price,
        t.count AS n,
        epoch(t.created_time) AS timestamp
    FROM read_parquet('{TRADES}') t
    JOIN read_parquet('{MARKETS}') g ON t.ticker = g.ticker
    """)

    # group the trades into buckets based on their timestamp and the time_bucket_size parameter
    con.execute(f"""--sql
    CREATE TABLE trades_bucketed AS
        SELECT event, team, route, 
            sum(n*price)/sum(n) AS avg_price, 
            sum(n) as num_contracts, 
            floor(timestamp / {time_bucket_size}) AS bucket_index
        FROM trades_routed
        GROUP BY event, team, bucket_index, route
    """)

    # create a bunch of comparisons: for trades in the same bucket_index on the same event, betting on the same team through different routes, compare the prices paid
    con.execute(f"""--sql
    CREATE TABLE trades_bucketed_compared AS
        SELECT yes_side.event, yes_side.team, 
            yes_side.avg_price AS yes_price, no_side.avg_price AS no_price, 
            yes_side.avg_price - no_side.avg_price AS yes_no_price_gap, 
            (yes_side.avg_price + no_side.avg_price) / 2 AS yes_no_midpoint_price, 
            yes_side.num_contracts AS yes_contracts, no_side.num_contracts AS no_contracts, 
            yes_side.bucket_index as bucket_index

        FROM trades_bucketed yes_side
        JOIN trades_bucketed no_side
        ON yes_side.event = no_side.event
        AND yes_side.team = no_side.team
        AND yes_side.bucket_index = no_side.bucket_index
        AND yes_side.route = 'yes'
        AND no_side.route = 'no'
    """)

    # convert the comparisons to pandas dataFrame and extract only the relevant info
    comparisons = con.sql("""--sql
    SELECT yes_no_price_gap, yes_no_midpoint_price
    FROM trades_bucketed_compared
    """).df()

    # group the comparisons on two axes: the price band from 1-99 cents, and the size of the price gap between the yes and no routes
    comparisons["price_band"] = (comparisons["yes_no_midpoint_price"] // 10) * 10
    gap = comparisons["yes_no_price_gap"]
    conditions = [gap < -2, gap < -1, gap < 0, gap == 0, gap <= 1, gap <=2, gap > 2]
    labels = ["< −2", "−2 to −1", "−1 to 0", "0", "0 to +1", "+1 to +2", "> +2"]
    comparisons["gap_size_band"] = np.select(conditions, labels, default = "0")

    # collect comparisons with the same value on both axes, and fill zero for value combinations that don't exist
    comparison_counts = comparisons.groupby(["price_band", "gap_size_band"]).size()
    comparison_counts = comparison_counts.unstack(fill_value = 0)
    comparison_counts = comparison_counts.reindex(columns = labels, fill_value = 0)

    # convert to percentages of each price_band that fall into each gap_size_band
    total_per_price_band = comparison_counts.sum(axis = "columns")
    comparison_percentages = comparison_counts.div(total_per_price_band, axis = "index") * 100

    # plot a chart, using the machinery defined in the function below
    fig = plot_gap_chart(comparison_percentages, labels, total_per_price_band, time_bucket_size)
    fig.savefig("chart-1.png", dpi=200)
    plt.show()


# chart-plotting machinery, written by claude (thanks claude!)
def plot_gap_chart(data, labels, n_per_band=None, bucket_size=None):
    """Diverging stacked bar chart of Yes-route minus No-route price gaps, by price band.
    data        -- table of counts or of percentages (rows = price bands, columns = gap categories)
    labels      -- the 7 gap categories, most negative first, zero in the middle
    n_per_band  -- optional: cells per band, printed under each band
    bucket_size -- optional: time window in seconds, shown in the subtitle
    """
    is_percent = (data.sum(axis="columns").round() == 100).all()

    below = [labels[2], labels[1], labels[0]]   # stacked downwards, nearest zero first
    zero = labels[3]
    above = [labels[4], labels[5], labels[6]]   # stacked upwards, nearest zero first
    # the 0-1¢ shades are deliberately faint: gaps that small can't be exploited
    colours = {
        labels[2]: "#c2d2ff", labels[1]: "#6aa0f1", labels[0]: "#3778bd",   # blue: Yes route cheaper
        zero: "#e3e3e3",                                                     # grey: no gap
        labels[4]: "#f4ccdd", labels[5]: "#ec93ba", labels[6]: "#c9508a",   # pink: Yes route dearer
    }

    # San Francisco is registered as "System Font" on macOS; fall back for glyphs it lacks (e.g. ▲▼).
    # sizes match charts 2 and 3 in cross-book-profit.py; fontsize= arguments below override the default
    plt.rcParams["font.family"] = ["System Font", "Helvetica Neue", "DejaVu Sans"]
    plt.rcParams["font.size"] = 12          # matplotlib's default is 10
    plt.rcParams["legend.fontsize"] = 13    # legends a notch bigger still

    fig, ax = plt.subplots(figsize=(11, 6))
    x = range(len(data))
    bar = dict(width=0.7, edgecolor="white", linewidth=1)

    half_zero = data[zero] / 2
    ax.bar(x, half_zero, color=colours[zero], label=zero, **bar)
    ax.bar(x, -half_zero, color=colours[zero], **bar)

    top = half_zero.copy()
    for cat in above:
        ax.bar(x, data[cat], bottom=top, color=colours[cat], label=cat, **bar)
        top = top + data[cat]

    bottom = -half_zero
    for cat in below:
        ax.bar(x, -data[cat], bottom=bottom, color=colours[cat], label=cat, **bar)
        bottom = bottom - data[cat]

    ax.axhline(0, color="black", linewidth=1)
    m = max(top.max(), -bottom.min()) * 1.2
    ax.set_ylim(-m, m)

    dearer = data[above].sum(axis="columns")
    cheaper = data[below].sum(axis="columns")
    share_dearer = dearer / (dearer + cheaper) * 100

    for xi, t, s in zip(x, top, share_dearer):
        ax.text(xi, t + m * 0.02, f"{s:.1f}%", ha="center", va="bottom", fontsize=10)

    unit = "%" if is_percent else ""
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, pos: f"{abs(v):,.0f}{unit}"))
    ax.set_ylabel("% of paired cells in band" if is_percent else "paired cells")

    # Kalshi prices run 1-99¢, so clamp each band's edges to that range: the outer bands read 1–10¢ and 90–99¢
    tick_text = [f"{max(int(b), 1)}–{min(int(b) + 10, 99)}¢" for b in data.index]
    if n_per_band is not None:
        tick_text = [f"{t}\nn={n:,}" for t, n in zip(tick_text, n_per_band)]
    ax.set_xticks(list(x))
    ax.set_xticklabels(tick_text, fontsize=10)
    ax.set_xlabel("price band (average of the Yes-route and No-route prices)")

    ax.text(1.02, 0.98, "▲ Yes-route costs more", transform=ax.transAxes, ha="left", va="top", fontsize=12)
    ax.text(1.02, 0.93, "% over each bar: share of\nnon-zero gaps where the\nYes route costs more",
            transform=ax.transAxes, ha="left", va="top", fontsize=10, color="#52514e")
    ax.text(1.02, 0.02, "▼ No-route costs more", transform=ax.transAxes, ha="left", va="bottom", fontsize=12)
    # note between the bottom of the legend and the ▼ label; the window size comes from bucket_size
    window_note = f"a {bucket_size}s window" if bucket_size is not None else "one window"
    ax.text(1.02, 0.14, f"Fractional cents come from\nweighted averages of multiple\ntrades within {window_note}",
            transform=ax.transAxes, ha="left", va="center", fontsize=10, color="#52514e")

    # bold title + grey italic subtitle, like charts 2 and 3; bold via Helvetica Neue since matplotlib can't make
    # San Francisco bold
    fig.suptitle("Two routes to the same bet; the Yes-route costs more?", x=0.01, y=0.985, ha="left", va="top",
                 fontsize=15, fontweight="bold", fontfamily="Helvetica Neue")
    subtitle = "Yes-route minus No-route price, by price band"
    if bucket_size is not None:
        #subtitle += f" ({bucket_size}s windows)"
        pass
    fig.text(0.01, 0.94, subtitle, ha="left", va="top", fontsize=12, fontstyle="italic", color="#52514e")
    ax.spines[["top", "right"]].set_visible(False)
    ax.yaxis.grid(True, color="#e1e0d9", linewidth=0.6)
    ax.set_axisbelow(True)

    handles, names = ax.get_legend_handles_labels()
    order = [3, 2, 1, 0, 4, 5, 6]
    ax.legend([handles[i] for i in order],
              [names[i] + ("" if names[i] == zero else "¢") for i in order],
              title="price gap", frameon=False, loc="center left", bbox_to_anchor=(1.0, 0.5))

    window = f"{bucket_size}-second" if bucket_size is not None else "time"
    footnote = (f"Kalshi two-team sports games, Apr–Nov 2025 · trades paired within {window} windows"
                " · each window counted equally, not weighted by volume")
    fig.text(0.01, 0.01, footnote, ha="left", va="bottom", fontsize=10, color="#52514e")

    fig.tight_layout(rect=(0, 0.03, 1, 0.96))   # leave room at the bottom for the footnote, and the top for the titles
    return fig

    
            
if __name__ == "__main__":
    main()