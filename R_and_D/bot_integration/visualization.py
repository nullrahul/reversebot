import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import os

class Visualizer:
    def __init__(self, output_dir: str = "R_and_D/plots"):
        self.output_dir = output_dir
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

    def plot_market_analysis(self, df: pd.DataFrame, market_id: str):
        """
        Generates an interactive HTML plot with Price, SMA, and Z-Score.
        """
        if df.empty:
            return None

        # Create subplots: Top for Price, Bottom for Z-Score
        fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                           vertical_spacing=0.03, subplot_titles=(f'Price Analysis: {market_id}', 'Z-Score Momentum'),
                           row_heights=[0.7, 0.3])

        # 1. Price Line
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['price'], name='YES Price', line=dict(color='#17BECF')), row=1, col=1)
        
        # 2. SMA 5m
        if 'sma_5m' in df.columns:
            fig.add_trace(go.Scatter(x=df['timestamp'], y=df['sma_5m'], name='SMA 5m', line=dict(color='#FF7F0E', dash='dash')), row=1, col=1)

        # 3. Z-Score (Bottom Plot)
        if 'z_score' in df.columns:
            fig.add_trace(go.Scatter(x=df['timestamp'], y=df['z_score'], name='Z-Score', fill='tozeroy', line=dict(color='#7F7F7F')), row=2, col=1)
            # Add threshold lines
            fig.add_hline(y=2.0, line_dash="dot", line_color="red", row=2, col=1)
            fig.add_hline(y=-2.0, line_dash="dot", line_color="green", row=2, col=1)

        fig.update_layout(height=800, title_text=f"Market Alpha Scan: {market_id}", template="plotly_dark", showlegend=True)
        
        output_path = os.path.join(self.output_dir, f"{market_id}_analysis.html")
        fig.write_html(output_path)
        return output_path
