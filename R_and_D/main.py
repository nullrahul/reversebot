import asyncio
import argparse
import sqlite3
import yaml
import os
import sys
from R_and_D.data_ingestion.clob_fetcher import ClobFetcher
from R_and_D.storage.parquet_writer import ParquetStorage
from R_and_D.queries.duckdb_client import DuckDBClient
from R_and_D.features.processor import FeatureEngineer
from R_and_D.backtesting.engine import BacktestEngine
from R_and_D.bot_integration.visualization import Visualizer

async def run_rd_workflow(market_ids: list = None):
    # Load config
    config_path = "R_and_D/config/config.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    fetcher = ClobFetcher()
    storage = ParquetStorage(config['storage']['base_path'])
    
    if not market_ids:
        # Default fallback
        market_ids = ['2147041']

    print(f"🚀 Starting R&D Pipeline for markets: {market_ids}")

    # 1. Ingestion
    for mid in market_ids:
        print(f"📥 Syncing {mid}...")
        history = await fetcher.fetch_price_history(mid)
        if history:
            storage.write_batch("prices", mid, history)
            print(f"✅ Saved {len(history)} price points.")
            
        trades = await fetcher.fetch_trades(mid)
        if trades:
            storage.write_batch("trades", mid, trades)
            print(f"✅ Saved {len(trades)} trades.")

    # 2. Analysis & Viz
    db = DuckDBClient()
    if db.register_parquet("prices"):
        engineer = FeatureEngineer(db)
        viz = Visualizer()
        
        for mid in market_ids:
            print(f"\n📊 Analyzing {mid}...")
            features = engineer.compute_features(mid)
            if features.empty:
                print(f"⚠️ No data for {mid}")
                continue

            # Visualization
            plot_path = viz.plot_market_analysis(features, mid)
            print(f"📈 Chart: {plot_path}")
            
            # Backtest
            backtester = BacktestEngine(fee_pct=config['strategy'].get('fee_pct', 0.02))
            results = backtester.run_strategy(features, config['strategy'])
            print(f"💰 Backtest Results for {mid}:")
            print(f"   WinRate: {results.get('win_rate',0)*100:.1f}%")
            print(f"   Total ROI: {results.get('roi',0):.2f}%")
            print(f"   Trades: {results.get('trade_count',0)}")
    else:
        print("❌ No data available in Parquet for DuckDB registration.")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--market", type=str)
    parser.add_argument("--markets", type=str)
    parser.add_argument("--vps-recent", action="store_true")
    args = parser.parse_args()
    
    target_list = []
    if args.market: target_list.append(args.market)
    if args.markets: target_list.extend(args.markets.split(","))
    
    if args.vps_recent:
        # VPS usually has remote_reversebot_rd.db for paper trades
        db_paths = ["data/remote_reversebot_rd.db", "data/reversebot_rd.db"]
        for db_path in db_paths:
            if os.path.exists(db_path):
                try:
                    conn = sqlite3.connect(db_path)
                    cur = conn.cursor()
                    cur.execute("SELECT DISTINCT market_id FROM paper_positions ORDER BY opened_at DESC LIMIT 5")
                    target_list.extend([r[0] for r in cur.fetchall()])
                    conn.close()
                    break
                except Exception:
                    continue

    asyncio.run(run_rd_workflow(list(set(target_list))))

if __name__ == "__main__":
    main()
