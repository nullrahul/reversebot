import pandas as pd
import numpy as np

class BacktestEngine:
    def __init__(self, fee_pct: float = 0.02):
        self.fee_pct = fee_pct

    def run_strategy(self, df: pd.DataFrame, strategy_config: dict) -> dict:
        """
        Simulates the strategy against engineered features.
        """
        if df.empty:
            return {"win_rate": 0, "roi": 0, "trade_count": 0}

        entry_z = strategy_config.get('entry_z_threshold', 2.0)
        tp_mult = strategy_config.get('tp_multiplier', 2.0)
        sl_mult = strategy_config.get('sl_multiplier', 0.5)

        trades = []
        in_pos = False
        entry_p = 0
        
        for i, row in df.iterrows():
            if not in_pos:
                # ENTRY: Z-Score high AND not crashing
                if row['z_score'] >= entry_z and row['is_crash'] == 0:
                    in_pos = True
                    entry_p = row['price']
            else:
                # EXIT: SL/TP
                cur_p = row['price']
                ret = cur_p / entry_p
                
                if ret >= tp_mult or ret <= sl_mult:
                    # Apply fee
                    net_ret = (ret - 1) * (1 - self.fee_pct)
                    trades.append(net_ret)
                    in_pos = False
                    
        if not trades:
            return {"win_rate": 0, "roi": 0, "trade_count": 0}
            
        return {
            "win_rate": len([t for t in trades if t > 0]) / len(trades),
            "roi": sum(trades),
            "trade_count": len(trades),
            "avg_trade": np.mean(trades)
        }
