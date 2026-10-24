"""Synthetic supplier worlds with planted parameters, for testing Track B models."""

from matsya_intelligence.synth.config import PRESETS, BehaviourChange, TypePrior, WorldConfig
from matsya_intelligence.synth.world import Futures, Truth, generate_world, resample_futures

__all__ = ["PRESETS", "BehaviourChange", "Futures", "Truth", "resample_futures", "TypePrior", "WorldConfig", "generate_world"]
