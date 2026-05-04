import duckdb
import os
from typing import Optional
import pandas as pd

class DuckDBClient:
    def __init__(self, db_path: str = "R_and_D/data/analytics.db"):
        self.conn = duckdb.connect(db_path)
        self.parquet_base = "R_and_D/data"

    def register_parquet(self, table_name: str):
        """Registers a partitioned parquet directory as a view."""
        path = os.path.join(self.parquet_base, table_name, "**/*.parquet")
        # Check if any files exist before registering
        import glob
        if not glob.glob(path, recursive=True):
            return False
            
        self.conn.execute(f"CREATE OR REPLACE VIEW {table_name} AS SELECT * FROM read_parquet('{path}', hive_partitioning=1)")
        return True

    def query(self, sql: str) -> pd.DataFrame:
        """Runs a query and returns a Pandas DataFrame."""
        return self.conn.execute(sql).df()

    def get_price_series(self, market_id: str, minutes: int = 60) -> pd.DataFrame:
        """Utility to get last N minutes of price data."""
        sql = f"""
            SELECT timestamp, price 
            FROM prices 
            WHERE market_id = '{market_id}'
            AND timestamp > (epoch(now()) - {minutes * 60})
            ORDER BY timestamp ASC
        """
        return self.query(sql)

    def get_summary_stats(self, market_id: str):
        """Calculates volatility and mean for a market."""
        sql = f"""
            SELECT 
                avg(price) as mean_price,
                stddev(price) as volatility,
                max(price) - min(price) as range
            FROM prices
            WHERE market_id = '{market_id}'
        """
        return self.query(sql)
