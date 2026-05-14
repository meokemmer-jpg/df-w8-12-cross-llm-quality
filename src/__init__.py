# DF-W8-12 Cross-LLM-Quality-Gate [CRUX-MK]
"""
Cross-LLM-Quality-Gate

Multi-Provider-LLM-Output-Konsens-Pruefung als Pre-Production-Gate.
Pattern-Reuse aus DF-86 (NLM-Daily-Sync) + DF-95 (Cross-LLM-Wargame-Engine).

Phase-1 Scaffold:
- LLMProvider Bash-Wrapper-Pattern (Codex/Gemini/Copilot/Grok)
- ConsensusEngine + ConsensusScore Dataclass
- QualityGate Hauptklasse mit K16/LC1-LC5
"""

from .llm_provider import (
    LLMProvider,
    LLMResponse,
    Verdict,
    CodexProvider,
    GeminiProvider,
    CopilotProvider,
    GrokProvider,
    detect_cooldown,
    classify_verdict,
)
from .consensus_engine import (
    ConsensusEngine,
    ConsensusScore,
    ConvergenceClass,
    TierRecommendation,
    detect_g3_2_divergence_proxies,
)
from .quality_gate import (
    QualityGate,
    GateMode,
    CircuitBreaker,
    CircuitBreakerState,
    K16ConcurrentSpawnError,
    QualityGateError,
)

__version__ = "0.1.0-PHASE-1"

__all__ = [
    # llm_provider
    "LLMProvider",
    "LLMResponse",
    "Verdict",
    "CodexProvider",
    "GeminiProvider",
    "CopilotProvider",
    "GrokProvider",
    "detect_cooldown",
    "classify_verdict",
    # consensus_engine
    "ConsensusEngine",
    "ConsensusScore",
    "ConvergenceClass",
    "TierRecommendation",
    "detect_g3_2_divergence_proxies",
    # quality_gate
    "QualityGate",
    "GateMode",
    "CircuitBreaker",
    "CircuitBreakerState",
    "K16ConcurrentSpawnError",
    "QualityGateError",
]
