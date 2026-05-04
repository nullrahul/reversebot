import pandas as pd
import numpy as np
from R_and_D.queries.duckdb_client import DuckDBClient

class FeatureEngineer:
    def __init__(self, db: DuckDBClient):
        self.db = db

    def compute_features(self, market_id: str) -> pd.DataFrame:
        """
        Extracts raw prices from DuckDB and engineers alpha features.
        """
        # Fetch data from DuckDB (Parquet)
        query = f"SELECT * FROM prices WHERE market_id = '{market_id}' ORDER BY timestamp"
        df = self.db.query(query) # Fixed: query instead of execute_query
        
        if df.empty:
            return df

        # --- Alpha Engineering ---
        # 1. Z-Score (The core signal)
        window = 10
        df['sma_5m'] = df['price'].rolling(window=window).mean()
        df['std_5m'] = df['price'].rolling(window=window).std()
        df['z_score'] = (df['price'] - df['sma_5m']) / df['std_5m'].replace(0, np.nan)
        
        # 2. Acceleration Guard (MISTAKE 117 Fix)
        # Measures how fast the NO price is collapsing
        df['no_velocity'] = df['price'].diff()
        df['is_crash'] = (df['no_velocity'] / df['price'].shift(1) < -0.10).astype(int)
        
        # 3. Simple SMA crossing for exit signals
        df['sma_20'] = df['price'].rolling(window=20).mean()
        
        return df.fillna(0)
