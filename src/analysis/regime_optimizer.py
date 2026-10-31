import sqlite3
import os
import math
import numpy as np
import pandas as pd
from datetime import datetime, timezone

DB_PATH = os.path.expanduser("~/reversebot/data/reversebot_rd.db")

def load_data():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"Database not found at {DB_PATH}")
    
    conn = sqlite3.connect(DB_PATH)
    
    query = """
        SELECT 'main_paper' AS profile_name, opened_at, realised_pnl
        FROM paper_positions
        WHERE order_status = 'FILLED' AND realised_pnl IS NOT NULL AND seconds_to_close_at_entry >= 0
        UNION ALL
        SELECT profile_name, opened_at, realised_pnl
        FROM profile_positions
        WHERE order_status = 'FILLED' AND realised_pnl IS NOT NULL AND seconds_to_close_at_entry >= 0
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    # Convert timestamps to datetime in UTC
    df['datetime'] = pd.to_datetime(df['opened_at'], unit='s', utc=True)
    df['date'] = df['datetime'].dt.date
    df['hour'] = df['datetime'].dt.hour
    df['day_of_week'] = df['datetime'].dt.day_name()
    df['day_index'] = df['datetime'].dt.dayofweek # 0=Monday, 6=Sunday
    return df

def analyze_time_buckets(df):
    print("\n" + "="*80)
    print("TASK 1: Time-Bucket Clustering (2-Hour Windows)")
    print("="*80)
    
    # 2-hour binning
    df['bin_2h'] = (df['hour'] // 2) * 2
    
    # Group by profile and 2-hour bin
    grouped = df.groupby(['profile_name', 'bin_2h']).agg(
        trades=('realised_pnl', 'count'),
        wins=('realised_pnl', lambda x: (x > 0).sum()),
        total_pnl=('realised_pnl', 'sum')
    ).reset_index()
    
    grouped['win_rate'] = (grouped['wins'] / grouped['trades']) * 100.0
    
    # Display results for major profiles
    major_profiles = ['main_paper', 'stop_loss', 'reversal_stop_loss']
    for profile in major_profiles:
        prof_df = grouped[grouped['profile_name'] == profile].sort_values('bin_2h')
        print(f"\nProfile: {profile}")
        print(f"{'UTC Window':12s} | Trades | Win Rate | Total PnL (USDC)")
        print("-" * 50)
        for _, row in prof_df.iterrows():
            win_str = f"{row['bin_2h']:02d}:00-{(row['bin_2h']+2):02d}:00"
            print(f"{win_str:12s} | {int(row['trades']):6d} | {row['win_rate']:7.1f}% | {row['total_pnl']:+16.2f}")

def generate_correlation_matrix(df):
    print("\n" + "="*80)
    print("TASK 2: Profile Daily P&L Pearson Correlation Matrix")
    print("="*80)
    
    # Calculate daily P&L per profile
    daily_pnl = df.groupby(['date', 'profile_name'])['realised_pnl'].sum().reset_index()
    
    # Pivot to date x profile
    pivoted = daily_pnl.pivot(index='date', columns='profile_name', values='realised_pnl').fillna(0.0)
    
    # Pearson correlation
    corr_matrix = pivoted.corr(method='pearson')
    
    # Rename index and columns to avoid pandas naming clash during reset_index()
    corr_matrix.index.name = 'Profile 1'
    corr_matrix.columns.name = 'Profile 2'
    
    # Print matrix nicely
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 1000)
    pd.set_option('display.float_format', lambda x: f'{x:+.4f}')
    print(corr_matrix)
    print("\nKey Insights:")
    
    # Find most negatively correlated pairs (hedges)
    unstacked = corr_matrix.unstack().reset_index()
    unstacked.columns = ['Profile 2', 'Profile 1', 'Corr']
    # Filter self-correlations
    pairs = unstacked[unstacked['Profile 1'] < unstacked['Profile 2']].sort_values('Corr')
    
    print("\nTop 3 Hedging Pairs (Negative Correlation):")
    for _, row in pairs.head(3).iterrows():
        print(f"  - {row['Profile 1']} vs {row['Profile 2']}: {row['Corr']:+.4f}")
        
    print("\nTop 3 Downside-Multiplying Pairs (Positive Correlation):")
    for _, row in pairs.tail(3).iloc[::-1].iterrows():
        print(f"  - {row['Profile 1']} vs {row['Profile 2']}: {row['Corr']:+.4f}")

def scan_streak_density(df):
    print("\n" + "="*80)
    print("TASK 3: Streak Density Scanner (Losing Streaks > 5 Trades)")
    print("="*80)
    
    major_profiles = ['main_paper', 'stop_loss', 'reversal_stop_loss']
    
    for profile in major_profiles:
        prof_df = df[df['profile_name'] == profile].sort_values('opened_at').reset_index(drop=True)
        if len(prof_df) == 0:
            continue
            
        streaks = []
        current_streak_len = 0
        start_idx = None
        
        for idx, row in prof_df.iterrows():
            pnl = row['realised_pnl']
            if pnl < 0:
                if current_streak_len == 0:
                    start_idx = idx
                current_streak_len += 1
            else:
                if current_streak_len > 5:
                    streaks.append((current_streak_len, start_idx, idx - 1))
                current_streak_len = 0
                
        if current_streak_len > 5:
            streaks.append((current_streak_len, start_idx, len(prof_df) - 1))
            
        print(f"\nProfile: {profile} | Found {len(streaks)} streaks > 5 losses")
        if len(streaks) == 0:
            continue
            
        streak_details = []
        for length, start, end in streaks:
            start_row = prof_df.loc[start]
            dt = start_row['datetime']
            streak_details.append({
                'length': length,
                'hour': dt.hour,
                'day_of_week': start_row['day_of_week'],
                'day_index': start_row['day_index'],
                'timestamp': start_row['opened_at']
            })
            
        streak_df = pd.DataFrame(streak_details)
        
        # Distribution by day of week
        day_dist = streak_df.groupby('day_of_week')['length'].count().reindex(
            ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        ).fillna(0)
        
        # Distribution by hour
        hour_dist = streak_df.groupby('hour')['length'].count().reindex(range(24)).fillna(0)
        
        print(f"  Distribution of Streak Starts by Day of Week:")
        for day, count in day_dist.items():
            if count > 0:
                print(f"    - {day:<10s}: {int(count):2d} occurrences")
                
        print(f"  Distribution of Streak Starts by Hour of Day (UTC):")
        peak_hours = []
        for hr, count in hour_dist.items():
            if count > 0:
                peak_hours.append(f"{hr:02d}:00 ({int(count)})")
        print(f"    - Active Hours: {', '.join(peak_hours)}")

def main():
    try:
        df = load_data()
        analyze_time_buckets(df)
        generate_correlation_matrix(df)
        scan_streak_density(df)
    except Exception as e:
        print(f"Error executing analysis: {e}")

if __name__ == "__main__":
    main()
