
import duckdb
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

max_time_gap = 1 
bot_speed_cutoff = 0.3

def main():
    # save tables to a file (instead of memory) so they survive between runs; the file is named after max_time_gap
    # so changing it starts a fresh file. gotta remember to delete the file after editing a CREATE TABLE query
    con = duckdb.connect(f"cross_book_{max_time_gap}s.duckdb")
    con.execute("SET memory_limit = '4GB'")
    MARKETS = "data/two_team_markets_clean.parquet"
    TRADES = "data/two_team_trades_clean.parquet"


    # determine which team was being implicitly bet on for each trade, and through which route (yes/no)
    # the 3 - g.side_idx inverts the team in question (represented as 1 or 2) if the taker took the no route
    # this table gets saved so we don't waste time generating it from scratch each time
    con.execute(f"""--sql
    CREATE TABLE IF NOT EXISTS trades_routed AS
    SELECT
        row_number() OVER () AS trade_id,       -- we didn't keep kalshi's original IDs, so number the trades ourselves
        g.event_ticker AS event,
        CASE WHEN t.taker_side = 'yes' THEN g.side_idx ELSE 3 - g.side_idx END AS team,
        t.taker_side AS route,
        CASE WHEN t.taker_side = 'yes' THEN t.yes_price ELSE t.no_price END AS price,
        t.count AS n,
        epoch(t.created_time) AS timestamp
    FROM read_parquet('{TRADES}') t
    JOIN read_parquet('{MARKETS}') g ON t.ticker = g.ticker
    """)



# pair each Yes/No leg with the most recent Yes/No leg on the other team
# Yes-Yes pairs are labeled with direction Yes; No-No pairs are labaled with direction No
    con.execute(f"""--sql
    CREATE TABLE IF NOT EXISTS paired_legs AS
    SELECT a.event AS event, a.route AS direction, 
        a.trade_id AS first_trade_id, b.trade_id AS second_trade_id,
        b.timestamp AS timestamp, b.timestamp - a.timestamp AS time_delta, 
        a.n AS first_trade_size, b.n AS second_trade_size,
        a.n = b.n AS matching_size_flag,
        least(a.n, b.n) AS lockable_size,                                       -- lockable size is the min size of the two legs
        100 - (a.price + b.price) AS gross_profit_cents, 
        100 - (a.price + b.price) - 0.0007*a.price*(100-a.price) - 0.0007*b.price*(100-b.price) AS net_profit_cents

    FROM trades_routed b
    ASOF JOIN trades_routed a
    ON a.event = b.event AND a.route = b.route AND a.team = 3 - b.team AND a.timestamp <= b.timestamp
    WHERE b.timestamp - a.timestamp <= {max_time_gap}
    """)


# # sanity check before plotting
#     con.sql(f"""--sql
#         SELECT 
#             direction, 
#             count(*) AS n_pairs, count(DISTINCT event) AS count_distinct_events, 
#             avg((gross_profit_cents > 0)::INT) AS proportion_gross_profitable,
#             avg((net_profit_cents > 0)::INT) AS proportion_net_profitable,
#             min(time_delta) AS min_time_delta, max(time_delta) AS max_time_delta
#         FROM paired_legs
#         GROUP BY direction
#     """).show()


# bring the pairs into pandas
    pairs_df = con.sql("""--sql
    SELECT 
        direction, time_delta, 
        gross_profit_cents, net_profit_cents, 
        first_trade_size, second_trade_size, 
        matching_size_flag
    FROM paired_legs
    """).df()

    profitable_pairs_df = con.sql(f"""--sql
    SELECT 
        direction, time_delta, net_profit_cents, 
        first_trade_id, second_trade_id,
        first_trade_size, second_trade_size, lockable_size,
        matching_size_flag, 
        time_delta < {bot_speed_cutoff} AS inhuman_speed_flag
    FROM paired_legs
    WHERE net_profit_cents > 0
    ORDER BY timestamp
    """).df()

    remaining_contracts = {}                                   # key: trade_id -> value: contracts left
    usable_contracts = []                                      # number of contracts that would actually be traded, ordered by timestamp like the rows in profitable_pairs_df
    for first_trade_id, second_trade_id, first_trade_size, second_trade_size, lockable_size in zip(
        profitable_pairs_df.first_trade_id, profitable_pairs_df.second_trade_id,
        profitable_pairs_df.first_trade_size, profitable_pairs_df.second_trade_size, profitable_pairs_df.lockable_size):

        remaining_contracts_first_trade = remaining_contracts.get(first_trade_id, first_trade_size)   # if seen before, get how many contracts not yet traded away. never seen yet = default to full size
        remaining_contracts_second_trade = remaining_contracts.get(second_trade_id, second_trade_size)
        used_contracts = min(lockable_size, remaining_contracts_first_trade, remaining_contracts_second_trade)

        remaining_contracts[first_trade_id] = remaining_contracts_first_trade - used_contracts
        remaining_contracts[second_trade_id] = remaining_contracts_second_trade - used_contracts
        usable_contracts.append(used_contracts)

    profitable_pairs_df["usable_size"] = usable_contracts



    # dollars per direction: overlapping (each pair counted in full, even when they overlap and double-count) vs actual (each contract only used once, as in real life)
    #profitable_pairs_df["overlapping_dollars"] = profitable_pairs_df["net_profit_cents"] * profitable_pairs_df["lockable_size"] / 100
    profitable_pairs_df["actual_dollars"] = profitable_pairs_df["net_profit_cents"] * profitable_pairs_df["usable_size"] / 100

    
    # convert the flags from earlier into masks (just some pandas True/False series)
    is_same_size = profitable_pairs_df["matching_size_flag"]          # both legs traded the same number of contracts
    is_inhuman_speed = profitable_pairs_df["inhuman_speed_flag"]      # legs closer than bot_speed_cutoff



    # check the share of profitable pairs whose leg sizes match against random chance
    time_delta = profitable_pairs_df["time_delta"]
    conditions = [time_delta < 0.010, time_delta < 0.030, time_delta < 0.100, time_delta < 0.300, time_delta < 1.0001]
    labels = ["under 10ms", "10 to 30ms", "30 to 100ms", "100 to 300ms", "300ms to 1000ms"]
    profitable_pairs_df["time_delta_range"] = np.select(conditions, labels, default = "other")

    # chance two unrelated legs match: for each size, (share of first legs that size) × (share of second legs that size), summed over sizes
    first_leg_size_shares = profitable_pairs_df["first_trade_size"].value_counts(normalize=True)     # index = size, value = share of first legs
    second_leg_size_shares = profitable_pairs_df["second_trade_size"].value_counts(normalize=True)   # index = size, value = share of second legs
    chance_of_matching_sizes = (first_leg_size_shares * second_leg_size_shares).sum()                 # sizes on only one side give NaN, which sum() skips

    # observed: share of pairs in each time range whose two legs are the same size
    pairs_grouped_by_time_range = profitable_pairs_df.groupby("time_delta_range")
    matched_share = (pairs_grouped_by_time_range["matching_size_flag"].sum() / pairs_grouped_by_time_range.size()).reindex(labels) 

    print()
    print(f"chance two legs match sizes by chance = {chance_of_matching_sizes.round(3)}")
    print("observed share of paired legs that match sizes:")
    print(matched_share.round(3))
    print()





    # each rule is (label, condition) and we use this when we get the different labels below
    # pair_type_rules = [
    #     ("same_size_legs", is_same_size),
    #     ("different_size_legs", ~is_same_size),
    # ]

    # ORIGINAL CODE: included a time cutoff too based on bot_speed_cutoff but that's kinda arbitrary
    pair_type_rules = [
        ("fast_same_size", is_inhuman_speed & is_same_size),
        ("fast_different_size", is_inhuman_speed & ~is_same_size),
        ("slow", ~is_inhuman_speed),
    ]

    pair_type_labels = [label for label, condition in pair_type_rules]
    pair_type_conditions = [condition for label, condition in pair_type_rules]
    profitable_pairs_df["pair_type"] = np.select(pair_type_conditions, pair_type_labels, default="other")

    # table: rows = pair type, columns = direction, cells = actual dollars, with totals
    print()
    print(profitable_pairs_df.pivot_table(index="pair_type", columns="direction", values="actual_dollars",
                                          aggfunc="sum", margins=True, margins_name="total").round(0))
    print()


# plot the profit distribution charts
    # fig = plot_profit_distribution(pairs_df)
    # fig.savefig("chart2_profit_distribution.png", dpi=200)

    # fig = plot_timing(pairs_df)
    # fig.savefig("chart3_timing.png", dpi=200)

    #plt.show()

    con.close()

    

# chart settings
for i_only_put_this_for_loop_here_so_it_would_be_easier_to_collapse_in_VSCode in range(1):

    DIRECTIONS = [("no", "Both No"), ("yes", "Both Yes")]

    # font settings for both charts: San Francisco (registered as "System Font" on macOS; falling back for any
    # glyph it lacks), and bigger default text; fontsize= arguments below override the default, so they're bumped up too
    plt.rcParams["font.family"] = ["System Font", "Helvetica Neue", "DejaVu Sans"]
    plt.rcParams["font.size"] = 12          # matplotlib's default is 10
    plt.rcParams["legend.fontsize"] = 13    # legends a notch bigger still
    # matplotlib can't mix styles within one text, but its maths notation can: $\mathit{word}$ italicises one word.
    # point that notation at Helvetica Neue bold italic so an italic word inside a bold title matches the rest of it
    plt.rcParams["mathtext.fontset"] = "custom"
    plt.rcParams["mathtext.it"] = "Helvetica Neue:italic:bold"

    # (dark, light) for each direction, using the yes-no-gap chart's colours: there, pink = Yes route dearer,
    # which points to buying both No, so Both No is pink and Both Yes is blue
    DIRECTION_COLOURS = {
        "no": ("#c9508a", "#ec93ba"),
        "yes": ("#3778bd", "#6aa0f1"),
    }

# chart plotting machinery, courtesy of claude opus
def plot_profit_distribution(pairs_df, max_cents=10):
    """Chart 2: profit per contract for each pair, gross vs net of fees, one panel per direction."""
    bins = np.arange(-max_cents - 0.5, max_cents + 1.5, 1)   # edges at half-cents: each whole cent gets its own bar

    fig, axes = plt.subplots(1, 2, figsize=(12, 5), sharex=True, sharey=True)
    for ax, (key, title) in zip(axes, DIRECTIONS):
        d = pairs_df[pairs_df["direction"] == key]
        weights = np.ones(len(d)) / len(d) * 100                # bars = % of ALL this direction's pairs
        colour = DIRECTION_COLOURS[key][0]

        ax.hist(d["gross_profit_cents"], bins=bins, weights=weights, histtype="step",
                linewidth=1.5, color="#9a9891", label="before fees")
        ax.hist(d["net_profit_cents"], bins=bins, weights=weights, histtype="stepfilled",
                alpha=0.35, color=colour, edgecolor=colour, linewidth=1.5, label="after fees")
        ax.axvline(0, color="black", linewidth=1)

        share_gross = (d["gross_profit_cents"] > 0).mean() * 100
        share_net = (d["net_profit_cents"] > 0).mean() * 100
        ax.text(0.98, 0.95, f"profitable before fees: {share_gross:.1f}%\nprofitable after fees: {share_net:.1f}%",
                transform=ax.transAxes, ha="right", va="top", fontsize=11)

        ax.set_title(f"{title}  (n = {len(d):,} pairs)", loc="left", fontsize=13)
        ax.set_xlabel("profit per contract (¢)")
        ax.set_ylabel("% of pairs")
        ax.tick_params(labelleft=True)   # sharey hides the right panel's y labels; show them on both
        ax.legend(frameon=False, loc="upper left")
        ax.spines[["top", "right"]].set_visible(False)
        ax.yaxis.grid(True, color="#e1e0d9", linewidth=0.6)
        ax.set_axisbelow(True)

    # bold via Helvetica Neue: matplotlib can't make San Francisco bold (see plot_timing).
    # \$ is a literal dollar sign; $\mathit{against}$ is the one italic word (see mathtext settings at the top)
    fig.suptitle(r"Locking in \$1: betting $\mathit{against}$ both teams works better", x=0.01, y=0.985,
                 ha="left", va="top", fontsize=15, fontweight="bold", fontfamily="Helvetica Neue")
    fig.text(0.01, 0.935, "Buying both sides of a game - profit per pair",   # subtitle
             ha="left", va="top", fontsize=12, fontstyle="italic", color="#52514e")
    fig.text(0.01, 0.01, f"Pairs of trades on opposite teams within {max_time_gap}s · pairs beyond ±{max_cents}¢ not shown",
             fontsize=10, color="#52514e")
    fig.tight_layout(rect=(0, 0.03, 1, 0.96))   # leave the top for the titles
    return fig

def plot_timing(pairs_df):
    """Chart 3, two panels sharing the time axis, per direction:
    top    = share of pairs profitable (each bar is 100% of that bar's pairs; grey = not profitable)
    bottom = number of profitable pairs
    In both: dark = profitable, legs of different sizes; light = profitable, legs of the same size."""
    edges = [0, 0.01, 0.03, 0.1, 0.3, np.inf]
    names = ["<10ms", "10–30ms", "30–100ms", "100–300ms", f"300ms–{max_time_gap}s"]
    # right=False makes each bin include its lower edge but not its upper one, so exactly 10ms lands in "10–30ms";
    # the last bin runs to infinity, so it also catches pairs exactly max_time_gap apart
    gap_bin = pd.cut(pairs_df["time_delta"], bins=edges, labels=names, right=False)

    profitable = pairs_df["net_profit_cents"] > 0
    same_size = pairs_df["matching_size_flag"]
    grouped = pd.DataFrame({
        "direction": pairs_df["direction"].astype(str),
        "gap_bin": gap_bin,
        "profit_diff": profitable & ~same_size,    # profitable, legs of different sizes
        "profit_same": profitable & same_size,     # profitable, legs of the same size
        "unprofitable": ~profitable,
    }).groupby(["gap_bin", "direction"], observed=True)
    shares = grouped.mean() * 100    # % of that bar's pairs
    counts = grouped.sum()           # number of pairs
    n_pairs = grouped.size()         # all pairs behind each bar

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(15, 8), sharex=True,
                                      gridspec_kw=dict(height_ratios=[1, 1]))
    x = np.arange(len(names))
    width = 0.38
    bar = dict(width=width, edgecolor="white", linewidth=1)
    for i, (key, title) in enumerate(DIRECTIONS):
        xpos = x + (i - 0.5) * width
        dark, light = DIRECTION_COLOURS[key]
        s = shares.xs(key, level="direction").reindex(names).fillna(0)
        c = counts.xs(key, level="direction").reindex(names).fillna(0)
        n = n_pairs.xs(key, level="direction").reindex(names).fillna(0)

        # top: shares, with grey for unprofitable
        top.bar(xpos, s["profit_diff"], color=dark, label=f"{title}: profitable, legs of different sizes", **bar)
        top.bar(xpos, s["profit_same"], bottom=s["profit_diff"], color=light,
                label=f"{title}: profitable, legs of the same size\n(possibly one trader taking both legs)", **bar)
        top.bar(xpos, s["unprofitable"], bottom=s["profit_diff"] + s["profit_same"], color="#e4e3dd",
                label="not profitable" if i == len(DIRECTIONS) - 1 else None, **bar)
        for xp, count in zip(xpos, n):
            top.text(xp, 101, f"{count / 1000:,.0f}k", ha="center", va="bottom", fontsize=9, color="#52514e")

        # bottom: counts of profitable pairs
        bottom.bar(xpos, c["profit_diff"], color=dark, **bar)
        bottom.bar(xpos, c["profit_same"], bottom=c["profit_diff"], color=light, **bar)

    top.set_ylim(0, 108)
    top.set_yticks(range(0, 101, 20))
    top.yaxis.set_major_formatter(FuncFormatter(lambda v, pos: f"{v:.0f}%"))
    top.set_ylabel("% of pairs")
    top.set_title("How likely is a pair to be profitable?  (number above each bar = pairs in that range)",
                  loc="left", fontsize=15)
    top.set_xlabel("time between the two trades")

    bottom.yaxis.set_major_formatter(FuncFormatter(lambda v, pos: f"{v:,.0f}"))
    bottom.set_ylabel("profitable pairs")
    bottom.set_title("How many profitable pairs are there?", loc="left", fontsize=15)
    bottom.set_xticks(x)
    bottom.set_xticklabels(names)
    bottom.set_xlabel("time between the two trades")
    bottom.yaxis.grid(True, color="#e1e0d9", linewidth=0.6)
    bottom.set_axisbelow(True)

    top.tick_params(labelbottom=True)    # repeat the time-bin labels under the top panel (sharex hides them)
    for ax in (top, bottom):
        ax.spines[["top", "right"]].set_visible(False)
    handles, labels = top.get_legend_handles_labels()
    fig.legend(handles, labels, frameon=False, loc="center left", bbox_to_anchor=(0.63, 0.5),
               labelspacing=1.2)   # font size comes from legend.fontsize at the top
    # matplotlib can't make macOS's San Francisco bold (it only reads the regular weight), so the bold title
    # uses Helvetica Neue, which looks nearly identical at this size.
    # 19, not 15: this figure is 15in wide vs chart 2's 12in, so it's scaled down more when viewed; 15 * 15/12 ≈ 19
    fig.suptitle("Most profitable pairs trade within 30ms", x=0.01, y=0.985, ha="left", va="top", fontsize=19,
                 fontweight="bold", fontfamily="Helvetica Neue")
    fig.text(0.01, 0.945, "Profitable pairs (after fees), by time between the two trades",   # subtitle
             ha="left", va="top", fontsize=15, fontstyle="italic", color="#52514e")
    fig.text(0.01, 0.01, f"Pairs of trades on opposite teams within {max_time_gap}s", fontsize=10, color="#52514e")
    fig.tight_layout(rect=(0, 0.03, 0.62, 0.95))   # leave the right side for the legend, and the top for the titles
    return fig




if __name__ == "__main__":
    main()