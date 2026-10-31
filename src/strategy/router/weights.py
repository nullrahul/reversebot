import json
import os
import hashlib
from typing import Dict, Any, List, Set
from src.logs.log_config import get_logger

logger = get_logger("router_weights")

class RouterWeightsLoader:
    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "config", "router_weights.json")
        self.config_path = os.path.abspath(config_path)
        self._raw_json_str: str = ""
        self._config: Dict[str, Any] = self._load_and_validate_config()
        self._allowlist: Set[str] = set(self._config.get("profile_allowlist", []))
        self._purged: Set[str] = set(self._config.get("purged_profiles", []))
        self._print_startup_fingerprint()

    def _load_and_validate_config(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path):
            logger.warning("router_weights_config_missing_using_fallback", path=self.config_path)
            fallback = {
                "weights_version": "v4.5.2-allowlist-fallback",
                "router_version": "v4.5.0-shadow",
                "feature_schema_version": "v1.0",
                "profile_allowlist": [
                    "cheap_reversal", "reversal_yes_cvd_imb", "reversal_yes_cvd_imb_wide",
                    "reversal_stop_loss", "reversal_eth_5m_momentum", "micro_flow_no",
                    "cross_market_hedge", "single_market_hedge_expensive"
                ],
                "purged_profiles": [
                    "loss_skip", "loss_skip_yes_cvd", "loss_skip_yes_cvd_v2", "loss_skip_btc_5m_recovery"
                ],
                "engines": {
                    "Reversal": {"weights": {"w_price": 0.385, "w_cvd": 0.330, "w_imbalance": 0.285}, "trigger_threshold": 0.75},
                    "Micro_Flow": {"weights": {"w_side_no": 0.500, "w_discount_price": 0.300, "w_time_close": 0.200}, "trigger_threshold": 0.80}
                },
                "policy": {"score_margin_min": 0.08, "gray_zone": {"noise_floor": 0.65, "watch_zone_ceiling": 0.75}}
            }
            self._raw_json_str = json.dumps(fallback)
            return fallback

        with open(self.config_path, "r", encoding="utf-8") as f:
            self._raw_json_str = f.read()

        try:
            config = json.loads(self._raw_json_str)
        except Exception as e:
            raise ValueError(f"Malformed JSON in router_weights.json at {self.config_path}: {e}")

        # Strict Schema Validation
        required_keys = ["weights_version", "router_version", "engines", "policy", "profile_allowlist"]
        for k in required_keys:
            if k not in config:
                raise ValueError(f"Missing required key '{k}' in router_weights.json configuration")

        return config

    def _print_startup_fingerprint(self):
        config_hash = hashlib.md5(self._raw_json_str.encode('utf-8')).hexdigest()[:8]
        rev_t = self.get_engine_threshold("Reversal")
        flow_t = self.get_engine_threshold("Micro_Flow")

        fingerprint_msg = (
            "\n============================================================\n"
            "   ReverseBot V4.5 Challenger Market State Router           \n"
            f"   Router Version       : {self.router_version}\n"
            f"   Weights Version      : {self.weights_version}\n"
            f"   Config MD5 Hash      : {config_hash}\n"
            f"   Approved Profiles    : {len(self._allowlist)} Active Alpha Generators\n"
            f"   Purged Profiles      : {len(self._purged)} Excluded (Loss-Skip, Telemetry, Baselines)\n"
            f"   Engine Thresholds    : Reversal={rev_t:.2f} | Micro_Flow={flow_t:.2f}\n"
            f"   Gray Zone Policy     : Noise < {self.noise_floor:.2f} | Watch < {self.watch_zone_ceiling:.2f} | Margin >= {self.score_margin_min:.2f}\n"
            "   Shadow Mode Status   : ENABLED (Parallel Non-Blocking Telemetry)\n"
            "   Execution Authority  : LEGACY STRATEGY ENGINE\n"
            "============================================================"
        )
        print(fingerprint_msg)
        logger.info(
            "router_startup_fingerprint_loaded",
            router_version=self.router_version,
            weights_version=self.weights_version,
            config_hash=config_hash,
            allowlist_count=len(self._allowlist),
            purged_count=len(self._purged),
            reversal_threshold=rev_t,
            micro_flow_threshold=flow_t,
            score_margin_min=self.score_margin_min,
            shadow_mode=True
        )

    def is_profile_allowed(self, profile_name: str) -> bool:
        if not self._allowlist:
            return True
        return profile_name in self._allowlist

    @property
    def profile_allowlist(self) -> List[str]:
        return list(self._config.get("profile_allowlist", []))

    @property
    def purged_profiles(self) -> List[str]:
        return list(self._config.get("purged_profiles", []))

    @property
    def weights_version(self) -> str:
        return self._config.get("weights_version", "v4.5.2-allowlist")

    @property
    def router_version(self) -> str:
        return self._config.get("router_version", "v4.5.0-shadow")

    @property
    def feature_schema_version(self) -> str:
        return self._config.get("feature_schema_version", "v1.0")

    def get_engine_weights(self, engine_name: str) -> Dict[str, float]:
        return self._config.get("engines", {}).get(engine_name, {}).get("weights", {})

    def get_engine_threshold(self, engine_name: str) -> float:
        return self._config.get("engines", {}).get(engine_name, {}).get("trigger_threshold", 0.75)

    @property
    def score_margin_min(self) -> float:
        return self._config.get("policy", {}).get("score_margin_min", 0.08)

    @property
    def noise_floor(self) -> float:
        return self._config.get("policy", {}).get("gray_zone", {}).get("noise_floor", 0.65)

    @property
    def watch_zone_ceiling(self) -> float:
        return self._config.get("policy", {}).get("gray_zone", {}).get("watch_zone_ceiling", 0.75)
