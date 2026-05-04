import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import os
from datetime import datetime
from typing import List, Dict, Any

class ParquetStorage:
    def __init__(self, base_path: str):
        self.base_path = base_path
        if not os.path.exists(base_path):
            os.makedirs(base_path)

    def write_batch(self, table_name: str, market_id: str, data: List[Dict[str, Any]]):
        """
        Writes a batch of data to partitioned parquet files.
        """
        if not data:
            return

        df = pd.DataFrame(data)
        
        # Force correct types for DuckDB
        if 'price' in df.columns:
            df['price'] = pd.to_numeric(df['price'], errors='coerce').astype(float)
        if 'size' in df.columns:
            df['size'] = pd.to_numeric(df['size'], errors='coerce').astype(float)

        # Robust Timestamp Handling
        if 'timestamp' in df.columns:
            try:
                # Force to numeric first to avoid string parsing issues
                df['timestamp'] = pd.to_numeric(df['timestamp'], errors='coerce')
                df['dt'] = pd.to_datetime(df['timestamp'], unit='s')
                df['date'] = df['dt'].dt.strftime('%Y-%m-%d')
            except Exception:
                df['date'] = datetime.now().strftime('%Y-%m-%d')
        else:
            df['date'] = datetime.now().strftime('%Y-%m-%d')

        df['market_id'] = str(market_id)
        
        table_path = os.path.join(self.base_path, table_name)
        table = pa.Table.from_pandas(df)
        
        pq.write_to_dataset(
            table,
            root_path=table_path,
            partition_cols=['market_id', 'date'],
            existing_data_behavior='overwrite_or_ignore'
        )

    def get_dataset_path(self, table_name: str) -> str:
        return os.path.join(self.base_path, table_name)
