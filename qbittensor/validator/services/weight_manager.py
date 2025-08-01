"""
Weight and scoring management for the validator (no longer async)
"""

import time
from typing import TYPE_CHECKING

import torch
import bittensor as bt

if TYPE_CHECKING:  # avoid a hard import cycle
    from qbittensor.validator import Validator

# CONSTANTS
SCORING_INTERVAL = 15 * 60  # every 15 min
WEIGHT_SETTING_INTERVAL = 20 * 60  # every 30 min
MIN_WEIGHT = 0.0001  # floor weight for active miners


class WeightManager:
    """
    Updates the scoring history and submits weight extrinsics
    """

    def __init__(self, validator: "Validator"):
        self.validator = validator
        self.last_scoring_time = 0.0
        self.last_weight_time = 0.0

    # Public
    def update(self) -> None:
        now = time.time()

        if now - self.last_scoring_time > SCORING_INTERVAL:
            self._update_scoring(now)

        if now - self.last_weight_time > WEIGHT_SETTING_INTERVAL:
            self._set_weights(now)

    # Internals
    def _update_scoring(self, now: float) -> None:
        """Persist the latest rolling-window scores to the DB."""
        try:
            self.validator._scoring_mgr.update_daily_score_history()
            self.last_scoring_time = now
            bt.logging.info("Updated daily scoring history")
        except Exception as exc:  # pragma: no cover
            bt.logging.error(f"Scoring update failed: {exc}", exc_info=True)

    def _set_weights(self, now: float) -> None:
        """Compute & submit weights based on decayed scores."""
        try:
            # Locate our own UID in the metagraph
            try:
                uid = self.validator.metagraph.hotkeys.index(
                    self.validator.wallet.hotkey.ss58_address
                )
            except ValueError:
                bt.logging.warning("Hotkey not found in metagraph")
                return  # still update timestamp below

            scores = self.validator._scoring_mgr.calculate_decayed_scores(
                lookback_days=2
            )
            if not scores:
                bt.logging.warning("No scores available for weight calculation")
                return  # still update timestamp below

            weights = torch.zeros(len(self.validator.metagraph.uids))
            for uid_, score in scores.items():
                if 0 <= uid_ < len(weights):
                    weights[uid_] = max(MIN_WEIGHT, score)

            total = weights.sum()
            if total > 0:
                weights /= total

            bt.logging.info(
                f"Setting weights for { (weights > 0).sum().item() } miners"
            )

            result = self.validator.subtensor.set_weights(
                wallet=self.validator.wallet,
                netuid=self.validator.config.netuid,
                uids=self.validator.metagraph.uids,
                weights=weights,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )

            if result and result[0]:
                bt.logging.info("✅ Weight extrinsic submitted")
                for uid_, score in sorted(
                    scores.items(), key=lambda x: x[1], reverse=True
                )[:5]:
                    bt.logging.info(f"UID {uid_}: {score:.4f}")
            else:
                bt.logging.error(f"Weight extrinsic failed: {result}")

        except Exception as exc:
            if "NeuronNoValidatorPermit" in str(exc):
                bt.logging.warning("No validator permit")
                self.last_weight_time = now
                return
            bt.logging.error(f"Unexpected weight-setting error: {exc}", exc_info=True)
        finally:
            # Always bump the timestamp so we don’t spam retries
            if self.last_weight_time < now:
                self.last_weight_time = now

    # Housekeeping
    def cleanup_scoring_data(self, retention_days: int = 7) -> None:
        """Purge old score snapshots from the DB."""
        try:
            self.validator._scoring_mgr.cleanup(retention_days=retention_days)
            bt.logging.info("Cleaned up old scoring data")
        except Exception as exc:  # pragma: no cover
            bt.logging.error(f"Scoring cleanup failed: {exc}", exc_info=True)
