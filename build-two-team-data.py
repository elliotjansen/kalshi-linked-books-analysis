
# builds the two cleaned files the analysis scripts read, from the raw Kalshi data in Jon Becker's dataset:
#   two_team_markets_clean.parquet  -- one row per team market (two per game), only games with a clean result
#   two_team_trades_clean.parquet   -- every trade on those markets
#   unclean_events.csv              -- the games that were left out, and why
# usage: python build-two-team-data.py [path to the dataset's data/kalshi folder] [output folder]

import os
import sys
import time
import duckdb

RAW_DIR = sys.argv[1] if len(sys.argv) > 1 else "becker-prediction-market-data/data/kalshi"
OUT_DIR = sys.argv[2] if len(sys.argv) > 2 else "data"

# game-winner series for sports where every game has exactly two markets, one per team (no draw market).
# listed explicitly: soccer series have a third "Tie" market, and a few other two-market series
# (Counter-Strike, EuroLeague, Ryder Cup) were left out of this analysis
SERIES = ["KXMLBGAME", "KXATPMATCH", "KXWTAMATCH", "KXNCAAFGAME", "KXNCAAMBGAME",
          "KXNHLGAME", "KXNBAGAME", "KXWNBAGAME", "KXNFLGAME", "KXNCAAWBGAME"]


def main():
    start = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)     # COPY ... TO fails if the output folder doesn't exist yet
    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")     # so the times written to the CSV are in UTC, not this computer's time zone
    series_list = ", ".join(f"'{s}'" for s in SERIES)

    # the team markets for every game in those series. side_idx numbers the two teams 1 and 2, in alphabetical
    # order of their market tickers; team_code is the end of the ticker (e.g. "MEM" in ...-25NOV11MEMMISS-MEM)
    con.execute(f"""--sql
    CREATE TABLE markets AS
    SELECT
        ticker, event_ticker,
        split_part(event_ticker, '-', 1) AS series,
        row_number() OVER (PARTITION BY event_ticker ORDER BY ticker) AS side_idx,
        split_part(ticker, '-', -1) AS team_code,
        title, yes_sub_title, no_sub_title, result, open_time, close_time, volume
    FROM read_parquet('{RAW_DIR}/markets/*.parquet')
    WHERE split_part(event_ticker, '-', 1) IN ({series_list})
    """)

    # every game should have exactly two markets; stop rather than quietly build bad data if not
    bad_games = con.sql("""--sql
        SELECT count(*) FROM (SELECT event_ticker FROM markets GROUP BY 1 HAVING count(*) != 2)
    """).fetchone()[0]
    if bad_games:
        sys.exit(f"{bad_games} games don't have exactly two markets; check the SERIES list")

    # a game resolved cleanly if one team's market resolved yes and the other's no. the results are joined
    # in alphabetical order, so a clean game reads "no+yes"; an unresolved game (no result yet) reads "+"
    con.execute("""--sql
    CREATE TABLE games AS
    SELECT event_ticker, string_agg(result, '+' ORDER BY result) AS pattern
    FROM markets
    GROUP BY event_ticker
    """)

    # the clean markets
    con.execute(f"""--sql
    COPY (
        SELECT m.* FROM markets m JOIN games g USING (event_ticker)
        WHERE g.pattern = 'no+yes'
        ORDER BY m.event_ticker, m.side_idx
    ) TO '{OUT_DIR}/two_team_markets_clean.parquet' (FORMAT parquet)
    """)

    # the games left out, one row per market, with the reason
    con.execute(f"""--sql
    COPY (
        SELECT m.event_ticker, m.series, g.pattern,
            CASE g.pattern
                WHEN '+' THEN 'no result recorded (mostly games scheduled after the data ends)'
                WHEN 'no+no' THEN 'both sides resolved No (likely cancelled/walkover)'
                ELSE 'other unexpected result'
            END AS reason,
            m.ticker, m.result, m.open_time, m.close_time
        FROM markets m JOIN games g USING (event_ticker)
        WHERE g.pattern != 'no+yes'
        ORDER BY m.series, m.event_ticker, m.ticker
    ) TO '{OUT_DIR}/unclean_events.csv' (HEADER)
    """)

    # every trade on the clean markets, sorted by market and time (it compresses far better that way).
    # kalshi's trade_id is left out: the ids are long random strings that would make the file ~5x bigger
    con.execute(f"""--sql
    COPY (
        SELECT ticker, count, yes_price, no_price, taker_side, created_time
        FROM read_parquet('{RAW_DIR}/trades/*.parquet')
        WHERE ticker IN (
            SELECT m.ticker FROM markets m JOIN games g USING (event_ticker) WHERE g.pattern = 'no+yes')
        ORDER BY ticker, created_time
    ) TO '{OUT_DIR}/two_team_trades_clean.parquet' (FORMAT parquet)
    """)

    # summary
    print(con.sql("""--sql
        SELECT pattern, count(*) AS games FROM games GROUP BY 1 ORDER BY 2 DESC
    """))
    n_markets, n_games = con.sql(f"""--sql
        SELECT count(*), count(DISTINCT event_ticker) FROM '{OUT_DIR}/two_team_markets_clean.parquet'
    """).fetchone()
    n_trades = con.sql(f"SELECT count(*) FROM '{OUT_DIR}/two_team_trades_clean.parquet'").fetchone()[0]
    print(f"kept {n_games:,} games, {n_markets:,} markets and {n_trades:,} trades "
          f"in {OUT_DIR}/ ({time.time() - start:.0f}s)")


if __name__ == "__main__":
    main()
