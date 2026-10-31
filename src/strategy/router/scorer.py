from typing import Dict, Any
from .weights import RouterWeightsLoader

class RouterScorer:
    def __init__(self, weights_loader: RouterWeightsLoader = None):
        if weights_loader is None:
            weights_loader = RouterWeightsLoader()
        self.weights = weights_loader

    def calculate_scores(self, features: Dict[str, Any]) -> Dict[str, float]:
        price = float(features.get("entry_price", 0.50))
        cvd = float(features.get("cvd_60s", 0.0))
        imbalance = float(features.get("book_imbalance", 0.0))
        side = str(features.get("side", "YES")).upper()
        seconds_to_close = float(features.get("seconds_to_close", 300.0))
        is_post_loss = bool(features.get("is_post_loss", False))

        # 1. Reversal Engine Score
        rev_w = self.weights.get_engine_weights("Reversal")
        phi_price = 1.0 if (0.45 <= price <= 0.70) else 0.0
        phi_cvd = 1.0 if (cvd < -10000) else 0.0
        phi_imb = 1.0 if (imbalance < -0.15) else 0.0
        
        s_rev = (
            rev_w.get("w_price", 0.385) * phi_price +
            rev_w.get("w_cvd", 0.330) * phi_cvd +
            rev_w.get("w_imbalance", 0.285) * phi_imb
        )

        # 2. Loss Recovery Engine Score
        rec_w = self.weights.get_engine_weights("Loss_Recovery")
        phi_rec_price = 1.0 if (price < 0.45) else (0.5 if price <= 0.60 else 0.0)
        phi_rec_cvd = 1.0 if (cvd < -10000 or cvd > 10000) else 0.0
        phi_post_loss = 1.0 if is_post_loss else 0.0

        s_rec = (
            rec_w.get("w_price", 0.400) * phi_rec_price +
            rec_w.get("w_cvd", 0.300) * phi_rec_cvd +
            rec_w.get("w_post_loss", 0.300) * phi_post_loss
        )

        # 3. Micro Flow Engine Score
        flow_w = self.weights.get_engine_weights("Micro_Flow")
        phi_side_no = 1.0 if side == "NO" else 0.0
        phi_disc = 1.0 if (0.35 <= price <= 0.45) else 0.0
        phi_time = 1.0 if (seconds_to_close < 180.0) else 0.0

        s_flow = (
            flow_w.get("w_side_no", 0.500) * phi_side_no +
            flow_w.get("w_discount_price", 0.300) * phi_disc +
            flow_w.get("w_time_close", 0.200) * phi_time
        )

        return {
            "Reversal": round(float(s_rev), 4),
            "Loss_Recovery": round(float(s_rec), 4),
            "Micro_Flow": round(float(s_flow), 4)
        }
