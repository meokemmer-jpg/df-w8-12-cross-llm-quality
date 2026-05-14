# DF-W8-12 ConsensusEngine [CRUX-MK]
"""
Konsens-Algorithmus fuer Multi-LLM-Output-Aggregation.

Pattern-Reuse aus DF-95 (Cross-LLM-Wargame-Engine):
- 3OF4 / 2OF3 Tier-Hierarchie
- G3.2-Divergenz-Proxy-Detection
- Verdict-Distribution-Analyse

Phase-1:
- Provider-Family-Diversity (G3.2 Proxy 1)
- Lineage-Distance (G3.2 Proxy 2, base-model-Heuristic)

Phase-2:
- Token-Prob-Variance (G3.2 Proxy 3)
- Gewichtete Verdict-Scores
- Postgres-State-Persistierung
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

from .llm_provider import LLMResponse, Verdict


# ============================================================
# Enums
# ============================================================

class ConvergenceClass(str, Enum):
    """Konvergenz-Klasse pro Konsens-Run.

    THREE_OF_THREE_CONVERGENT  - 3/3 zustimmend (oder mehr)
    TWO_OF_THREE_CONVERGENT    - 2/3 zustimmend
    ONE_OF_TWO_PARTIAL         - 1/2 zustimmend (nur 2 verfuegbar)
    NO_CONSENSUS               - Verdicts gemischt, kein klares Bild
    SINGLE_LLM                 - Nur 1 LLM verfuegbar (kein Konsens moeglich)
    NO_PROVIDERS               - 0 Provider verfuegbar (Error-Mode)
    """
    THREE_OF_THREE_CONVERGENT = "3OF3-CONVERGENT"
    TWO_OF_THREE_CONVERGENT = "2OF3-CONVERGENT"
    ONE_OF_TWO_PARTIAL = "1OF2-PARTIAL"
    NO_CONSENSUS = "NO-CONSENSUS"
    SINGLE_LLM = "SINGLE-LLM"
    NO_PROVIDERS = "NO-PROVIDERS"


class TierRecommendation(str, Enum):
    """Verdict-Tier (gemaess FIXPUNKT-1 Update + meta-stack-fixpunkte)."""

    REJECT = "REJECT"
    CONDITIONAL = "CONDITIONAL"
    SIM_HARDENED = "CROSS-LLM-SIMULATION-HARDENED"
    TWOOFTHREE_HARDENED = "CROSS-LLM-2OF3-HARDENED"
    HARDENED = "HARDENED"


# ============================================================
# G3.2 Divergenz-Proxies
# ============================================================

# Mapping family -> "Lineage-Distance"-Marker.
# Phase-1: heuristic. Phase-2: empirische Distance-Matrix.
FAMILY_LINEAGE_GROUPS: dict[str, str] = {
    "openai": "openai-line",
    "github_openai": "openai-line",  # nutzt OpenAI-Modelle (gpt-4o, gpt-5)
    "google": "google-line",
    "xai": "xai-line",
    "anthropic": "anthropic-line",
    "meta": "meta-line",
    "mistral": "mistral-line",
}


def detect_g3_2_divergence_proxies(responses: list[LLMResponse]) -> list[str]:
    """G3.2 Divergenz-Proxy-Detection (Phase-1 Subset).

    Phase-1 prueft 2 von 3 Proxies:
    1. Provider-Family-Diversity (mindestens 2 verschiedene families)
    2. Lineage-Distance (mindestens 2 verschiedene Lineage-Groups)

    Phase-2 ergaenzt:
    3. Token-Prob-Distribution-Variance > Schwelle T

    Args:
        responses: Liste von LLMResponse-Objekten (nur available=True).

    Returns:
        Liste von erfuellten Proxy-Namen (bspw. ["family-diversity", "lineage-distance"]).
    """
    available = [r for r in responses if r.available]
    proxies: list[str] = []

    if len(available) < 2:
        return proxies

    # Proxy 1: Provider-Family-Diversity
    families = {r.family for r in available if r.family}
    if len(families) >= 2:
        proxies.append("family-diversity")

    # Proxy 2: Lineage-Distance
    lineage_groups = {
        FAMILY_LINEAGE_GROUPS.get(r.family, r.family) for r in available if r.family
    }
    # Filtere None / leere Werte
    lineage_groups = {lg for lg in lineage_groups if lg}
    if len(lineage_groups) >= 2:
        proxies.append("lineage-distance")

    # Proxy 3 (Phase-2 placeholder): token-prob-variance
    # Phase-1: nicht implementiert - bleibt nicht in der Liste

    return proxies


# ============================================================
# ConsensusScore Dataclass
# ============================================================

@dataclass
class ConsensusScore:
    """Aggregiertes Ergebnis einer Konsens-Pruefung.

    Pflicht-Felder fuer Audit-Log + Tier-Mapping:
    - overall_score [0.0-1.0]
    - convergence_class
    - tier_recommendation
    - llm_responses (list[LLMResponse])
    - g3_2_divergence_proxies (list[str])
    """
    overall_score: float
    convergence_class: ConvergenceClass
    llm_responses: list[LLMResponse]
    g3_2_divergence_proxies: list[str]
    tier_recommendation: TierRecommendation
    n_providers_available: int
    n_providers_total: int
    verdict_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "overall_score": self.overall_score,
            "convergence_class": self.convergence_class.value,
            "tier_recommendation": self.tier_recommendation.value,
            "g3_2_divergence_proxies": list(self.g3_2_divergence_proxies),
            "n_providers_available": self.n_providers_available,
            "n_providers_total": self.n_providers_total,
            "verdict_counts": dict(self.verdict_counts),
            "llm_responses": [r.to_dict() for r in self.llm_responses],
        }
        return d


# ============================================================
# Verdict-Aggregation Helpers
# ============================================================

ZUSTIMMENDE_VERDICTS = {Verdict.ADOPT, Verdict.MODIFY_LIGHT}
ABLEHNENDE_VERDICTS = {Verdict.REJECT, Verdict.MODIFY_STRONG}


def count_verdicts(responses: list[LLMResponse]) -> dict[str, int]:
    """Zaehlt Verdicts pro Klasse.

    Args:
        responses: Available responses.

    Returns:
        Dict mit verdict-name -> count.
    """
    counts: dict[str, int] = {v.value: 0 for v in Verdict}
    for r in responses:
        if not r.available:
            continue
        v = r.verdict if isinstance(r.verdict, Verdict) else Verdict(r.verdict)
        counts[v.value] += 1
    return counts


# ============================================================
# ConsensusEngine
# ============================================================

class ConsensusEngine:
    """Berechnet Konsens-Score + Tier-Empfehlung.

    Phase-1 Algorithmus (regelbasiert):
    1. Verdict-Distribution analysieren
    2. Convergence-Class bestimmen (3OF3 / 2OF3 / 1OF2 / NO-CONSENSUS / SINGLE-LLM / NO-PROVIDERS)
    3. G3.2-Proxies detektieren
    4. Tier-Mapping:
       - 3/3 ADOPT + 2+ Proxies -> HARDENED
       - 3/3 ADOPT + 1 Proxy   -> 2OF3-HARDENED
       - 3/3 MODIFY_LIGHT       -> SIM-HARDENED
       - 2/3 zustimmend         -> 2OF3-HARDENED (mit Proxy) oder SIM-HARDENED
       - Mehrheit REJECT        -> REJECT
       - 2 verfuegbar           -> max SIM-HARDENED
       - 1 verfuegbar           -> max CONDITIONAL
       - 0 verfuegbar           -> NO-CONSENSUS / Error
    """

    def __init__(self, hardened_proxy_threshold: int = 2):
        """
        Args:
            hardened_proxy_threshold: Anzahl G3.2-Proxies erforderlich fuer HARDENED.
                Default 2 (per config.yaml).
        """
        self.hardened_proxy_threshold = hardened_proxy_threshold

    # ============================================================
    # compute_consensus
    # ============================================================

    def compute_consensus(
        self,
        responses: list[LLMResponse],
        n_providers_total: int | None = None,
    ) -> ConsensusScore:
        """Berechnet ConsensusScore aus Provider-Responses.

        Args:
            responses: Liste von LLMResponse (von allen Providern).
            n_providers_total: Total-Anzahl konfigurierter Provider.
                None -> wird aus len(responses) abgeleitet.

        Returns:
            ConsensusScore mit Tier-Empfehlung + Convergence-Class + Proxies.
        """
        if n_providers_total is None:
            n_providers_total = len(responses)

        available_responses = [r for r in responses if r.available]
        n_avail = len(available_responses)

        # Verdict-Counts auf available
        counts = count_verdicts(available_responses)
        n_adopt = counts[Verdict.ADOPT.value]
        n_modify_light = counts[Verdict.MODIFY_LIGHT.value]
        n_modify_strong = counts[Verdict.MODIFY_STRONG.value]
        n_reject = counts[Verdict.REJECT.value]
        n_unknown = counts[Verdict.UNKNOWN.value]

        n_zustimmend = n_adopt + n_modify_light
        n_ablehnend = n_reject + n_modify_strong

        proxies = detect_g3_2_divergence_proxies(available_responses)

        # ============================================================
        # Convergence-Class + Tier-Empfehlung
        # ============================================================

        if n_avail == 0:
            convergence = ConvergenceClass.NO_PROVIDERS
            tier = TierRecommendation.REJECT
            score = 0.0
        elif n_avail == 1:
            convergence = ConvergenceClass.SINGLE_LLM
            tier = TierRecommendation.CONDITIONAL
            # Score basiert auf alleiniger LLM-Verdict
            single_v = available_responses[0].verdict
            if single_v in ZUSTIMMENDE_VERDICTS:
                score = 0.5
            else:
                score = 0.25
        elif n_avail == 2:
            # Max SIM-HARDENED
            if n_adopt == 2:
                convergence = ConvergenceClass.ONE_OF_TWO_PARTIAL  # 2/2 zustimmend = full convergent in 2-LLM-Mode
                tier = TierRecommendation.SIM_HARDENED
                score = 0.7
            elif n_zustimmend == 2:
                convergence = ConvergenceClass.ONE_OF_TWO_PARTIAL
                tier = TierRecommendation.SIM_HARDENED
                score = 0.65
            elif n_zustimmend == 1 and n_ablehnend == 1:
                convergence = ConvergenceClass.NO_CONSENSUS
                tier = TierRecommendation.CONDITIONAL
                score = 0.4
            elif n_reject >= 1 and n_zustimmend == 0:
                convergence = ConvergenceClass.NO_CONSENSUS
                tier = TierRecommendation.REJECT
                score = 0.1
            else:
                convergence = ConvergenceClass.NO_CONSENSUS
                tier = TierRecommendation.CONDITIONAL
                score = 0.3
        else:
            # 3+ Provider verfuegbar
            convergence, tier, score = self._compute_3plus_consensus(
                n_avail=n_avail,
                n_adopt=n_adopt,
                n_modify_light=n_modify_light,
                n_modify_strong=n_modify_strong,
                n_reject=n_reject,
                n_unknown=n_unknown,
                proxies=proxies,
            )

        return ConsensusScore(
            overall_score=score,
            convergence_class=convergence,
            llm_responses=responses,
            g3_2_divergence_proxies=proxies,
            tier_recommendation=tier,
            n_providers_available=n_avail,
            n_providers_total=n_providers_total,
            verdict_counts=counts,
        )

    # ============================================================
    # 3+ Provider Konsens-Logic
    # ============================================================

    def _compute_3plus_consensus(
        self,
        n_avail: int,
        n_adopt: int,
        n_modify_light: int,
        n_modify_strong: int,
        n_reject: int,
        n_unknown: int,
        proxies: list[str],
    ) -> tuple[ConvergenceClass, TierRecommendation, float]:
        """Konsens-Logic fuer 3+ Provider.

        Mappings (from spec):
        - 3/3 ADOPT + 2+ Proxies -> HARDENED                    (score 0.95)
        - 3/3 ADOPT + 1 Proxy   -> 2OF3-HARDENED               (score 0.85)
        - 3/3 ADOPT + 0 Proxies -> SIM-HARDENED                 (score 0.80)
        - 3/3 MODIFY_LIGHT       -> SIM-HARDENED                 (score 0.75)
        - 2/3 zustimmend + Proxy -> 2OF3-HARDENED               (score 0.70)
        - 2/3 zustimmend         -> SIM-HARDENED                 (score 0.65)
        - 1/3 zustimmend + 2/3 ablehnend -> CONDITIONAL          (score 0.30)
        - 3/3 REJECT             -> REJECT                       (score 0.05)
        - Mehrheit REJECT (>50%) -> REJECT                        (score 0.10)
        - sonst                  -> CONDITIONAL                   (score 0.40)
        """
        n_zustimmend = n_adopt + n_modify_light
        n_ablehnend = n_reject + n_modify_strong
        proxy_count = len(proxies)

        # 3/3 ADOPT (alle verfuegbaren stimmen ADOPT)
        if n_adopt == n_avail and n_avail >= 3:
            if proxy_count >= self.hardened_proxy_threshold:
                return (
                    ConvergenceClass.THREE_OF_THREE_CONVERGENT,
                    TierRecommendation.HARDENED,
                    0.95,
                )
            elif proxy_count >= 1:
                return (
                    ConvergenceClass.THREE_OF_THREE_CONVERGENT,
                    TierRecommendation.TWOOFTHREE_HARDENED,
                    0.85,
                )
            else:
                return (
                    ConvergenceClass.THREE_OF_THREE_CONVERGENT,
                    TierRecommendation.SIM_HARDENED,
                    0.80,
                )

        # 3/3 MODIFY_LIGHT (alle stimmen mit kleinen Aenderungen zu)
        if n_modify_light == n_avail and n_avail >= 3:
            return (
                ConvergenceClass.THREE_OF_THREE_CONVERGENT,
                TierRecommendation.SIM_HARDENED,
                0.75,
            )

        # 3/3 zustimmend (gemischt ADOPT + MODIFY_LIGHT, alle verfuegbar)
        if n_zustimmend == n_avail and n_avail >= 3:
            if proxy_count >= 1:
                return (
                    ConvergenceClass.THREE_OF_THREE_CONVERGENT,
                    TierRecommendation.TWOOFTHREE_HARDENED,
                    0.78,
                )
            return (
                ConvergenceClass.THREE_OF_THREE_CONVERGENT,
                TierRecommendation.SIM_HARDENED,
                0.72,
            )

        # 3/3 REJECT (alle ablehnend)
        if n_reject == n_avail and n_avail >= 3:
            return (
                ConvergenceClass.THREE_OF_THREE_CONVERGENT,
                TierRecommendation.REJECT,
                0.05,
            )

        # Mehrheit REJECT (>50%)
        if n_reject > n_avail / 2.0:
            return (
                ConvergenceClass.NO_CONSENSUS,
                TierRecommendation.REJECT,
                0.10,
            )

        # 2/3 zustimmend (zustimmend ist Mehrheit)
        if n_zustimmend >= 2 and n_zustimmend > n_ablehnend:
            if proxy_count >= 1:
                return (
                    ConvergenceClass.TWO_OF_THREE_CONVERGENT,
                    TierRecommendation.TWOOFTHREE_HARDENED,
                    0.70,
                )
            return (
                ConvergenceClass.TWO_OF_THREE_CONVERGENT,
                TierRecommendation.SIM_HARDENED,
                0.62,
            )

        # 1/3 zustimmend + 2/3 ablehnend -> CONDITIONAL
        if n_zustimmend == 1 and n_ablehnend >= 2:
            return (
                ConvergenceClass.NO_CONSENSUS,
                TierRecommendation.CONDITIONAL,
                0.30,
            )

        # Default: kein klares Bild -> CONDITIONAL
        return (
            ConvergenceClass.NO_CONSENSUS,
            TierRecommendation.CONDITIONAL,
            0.40,
        )
