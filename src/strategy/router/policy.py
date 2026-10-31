from typing import Dict, Any, Tuple
from .weights import RouterWeightsLoader

class RouterPolicy:
    def __init__(self, weights_loader: RouterWeightsLoader = None):
        if weights_loader is None:
            weights_loader = RouterWeightsLoader()
        self.weights = weights_loader

    def evaluate_policy(self, scores: Dict[str, float]) -> Tuple[str, str, float, float, float]:
        """
        Evaluates scores against Gray Zone rules & Score Margin tie-breaking.
        Returns:
            (router_decision, reason_rejected, winning_score, second_best_score, score_margin)
        """
        if not scores:
            return ("REJECT", "EmptyScores", 0.0, 0.0, 0.0)

        # Sort engines by score descending
        sorted_engines = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        winning_engine, winning_score = sorted_engines[0]
        second_best_engine, second_best_score = sorted_engines[1] if len(sorted_engines) > 1 else ("None", 0.0)
        
        score_margin = round(winning_score - second_best_score, 4)
        min_margin = self.weights.score_margin_min
        noise_floor = self.weights.noise_floor
        watch_ceiling = self.weights.watch_zone_ceiling
        engine_threshold = self.weights.get_engine_threshold(winning_engine)

        # 1. Noise Floor Rule (Score < 0.65)
        if winning_score < noise_floor:
            return ("REJECT", f"NoiseFloor (Score {winning_score:.2f} < {noise_floor:.2f})", winning_score, second_best_score, score_margin)

        # 2. Watch / Shadow Zone Rule (0.65 <= Score < 0.75)
        if winning_score < watch_ceiling or winning_score < engine_threshold:
            return ("WATCH", f"WatchZone (Score {winning_score:.2f} < Threshold {engine_threshold:.2f})", winning_score, second_best_score, score_margin)

        # 3. Score Margin Tie-Breaking Rule (Delta S < 0.08)
        if len(sorted_engines) > 1 and second_best_score >= noise_floor and score_margin < min_margin:
            return ("REJECT", f"ConflictedRegime (Margin {score_margin:.2f} < {min_margin:.2f} between {winning_engine} and {second_best_engine})", winning_score, second_best_score, score_margin)

        # 4. High Conviction Accept
        return ("ACCEPT", f"HighConviction ({winning_engine} Score {winning_score:.2f} >= {engine_threshold:.2f}, Margin {score_margin:.2f})", winning_score, second_best_score, score_margin)
