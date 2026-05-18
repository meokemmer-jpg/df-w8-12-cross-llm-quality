
# K12+K13+K16 Trinity-CONTRARIAN 2026-05-17 (Cross-LLM-validated)
def k12_provenance(payload: bytes, key: bytes = b"df-trinity-contrarian-v1") -> dict:
    import hashlib, hmac
    return {
        "payload_hash": hashlib.sha256(payload).hexdigest(),
        "hmac_sha256": hmac.new(key, payload, hashlib.sha256).hexdigest(),
    }

def k13_anchor(payload_hash: str) -> dict:
    from datetime import datetime, timezone
    return {
        "anchor_type": "rfc3161-mock",
        "iso_ts": datetime.now(timezone.utc).isoformat(),
        "payload_hash": payload_hash,
    }

def k16_lock_or_exit(df_name: str):
    import fcntl, os, sys
    lock_path = f"/tmp/df-trinity-{df_name}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        sys.exit(3)

# DF-W8-12 ConsensusEngine Tests [CRUX-MK]
"""
Tests fuer Konsens-Algorithmus + Tier-Mapping + G3.2-Divergenz-Proxy-Detection.

Pattern: jeder Test baut LLMResponse-Liste manuell und prueft Tier-Output.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ============================================================
# Helpers
# ============================================================

def _import_modules():
    from src.llm_provider import LLMResponse, Verdict
    from src.consensus_engine import (
        ConsensusEngine,
        ConsensusScore,
        ConvergenceClass,
        TierRecommendation,
        detect_g3_2_divergence_proxies,
        FAMILY_LINEAGE_GROUPS,
    )
    return {
        "LLMResponse": LLMResponse,
        "Verdict": Verdict,
        "ConsensusEngine": ConsensusEngine,
        "ConsensusScore": ConsensusScore,
        "ConvergenceClass": ConvergenceClass,
        "TierRecommendation": TierRecommendation,
        "detect_g3_2_divergence_proxies": detect_g3_2_divergence_proxies,
        "FAMILY_LINEAGE_GROUPS": FAMILY_LINEAGE_GROUPS,
    }


def _make_response(
    provider: str,
    family: str,
    verdict_value: str,
    available: bool = True,
    model: str = "test-model",
):
    """Helper-Konstruktor fuer LLMResponse."""
    M = _import_modules()
    return M["LLMResponse"](
        provider=provider,
        model=model,
        timestamp=datetime.now(timezone.utc).isoformat(),
        raw_text=f"{verdict_value} - mocked",
        verdict=M["Verdict"](verdict_value),
        duration_s=0.1,
        cooldown_detected=False,
        error=None,
        family=family,
        available=available,
    )


# ============================================================
# Test 1 - 3/3 ADOPT + 2 G3.2-Proxies -> HARDENED
# ============================================================

def test_consensus_3of3_adopt():
    M = _import_modules()
    engine = M["ConsensusEngine"]()

    # 3 verschiedene Familien (Codex/Gemini/Grok) -> sowohl family-diversity als auch lineage-distance
    responses = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "ADOPT"),
        _make_response("grok", "xai", "ADOPT"),
    ]

    score = engine.compute_consensus(responses)
    assert score.tier_recommendation == M["TierRecommendation"].HARDENED
    assert score.convergence_class == M["ConvergenceClass"].THREE_OF_THREE_CONVERGENT
    assert score.n_providers_available == 3
    assert score.n_providers_total == 3
    assert score.overall_score >= 0.9
    # Beide Proxies erfuellt
    assert "family-diversity" in score.g3_2_divergence_proxies
    assert "lineage-distance" in score.g3_2_divergence_proxies


# ============================================================
# Test 2 - 3/3 MODIFY_LIGHT -> SIM_HARDENED
# ============================================================

def test_consensus_3of3_modify_light():
    M = _import_modules()
    engine = M["ConsensusEngine"]()
    responses = [
        _make_response("codex", "openai", "MODIFY_LIGHT"),
        _make_response("gemini", "google", "MODIFY_LIGHT"),
        _make_response("grok", "xai", "MODIFY_LIGHT"),
    ]
    score = engine.compute_consensus(responses)
    assert score.tier_recommendation == M["TierRecommendation"].SIM_HARDENED
    assert score.convergence_class == M["ConvergenceClass"].THREE_OF_THREE_CONVERGENT
    assert score.overall_score >= 0.7


# ============================================================
# Test 3 - 2/3 ADOPT + 1/3 MODIFY -> 2OF3-HARDENED
# ============================================================

def test_consensus_2of3_adopt():
    M = _import_modules()
    engine = M["ConsensusEngine"]()
    # 2 ADOPT + 1 MODIFY_LIGHT -> 3/3 zustimmend (gemischt) -> 2OF3-HARDENED
    responses = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "ADOPT"),
        _make_response("grok", "xai", "MODIFY_LIGHT"),
    ]
    score = engine.compute_consensus(responses)
    # 3/3 zustimmend (2 ADOPT + 1 MODIFY_LIGHT) mit 2 Proxies -> TWOOFTHREE_HARDENED
    assert score.tier_recommendation == M["TierRecommendation"].TWOOFTHREE_HARDENED
    assert score.convergence_class == M["ConvergenceClass"].THREE_OF_THREE_CONVERGENT

    # Alternative: 2 ADOPT + 1 MODIFY_STRONG -> 2/3 zustimmend, 1 ablehnend
    responses_2 = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "ADOPT"),
        _make_response("grok", "xai", "MODIFY_STRONG"),
    ]
    score_2 = engine.compute_consensus(responses_2)
    # 2/3 zustimmend mit Proxy -> TWOOFTHREE_HARDENED
    assert score_2.tier_recommendation == M["TierRecommendation"].TWOOFTHREE_HARDENED
    assert score_2.convergence_class == M["ConvergenceClass"].TWO_OF_THREE_CONVERGENT


# ============================================================
# Test 4 - 1/3 ADOPT + 2/3 MODIFY_STRONG -> CONDITIONAL
# ============================================================

def test_consensus_1of3_adopt_2of3_modify_strong():
    M = _import_modules()
    engine = M["ConsensusEngine"]()
    responses = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "MODIFY_STRONG"),
        _make_response("grok", "xai", "MODIFY_STRONG"),
    ]
    score = engine.compute_consensus(responses)
    assert score.tier_recommendation == M["TierRecommendation"].CONDITIONAL
    assert score.convergence_class == M["ConvergenceClass"].NO_CONSENSUS
    assert score.overall_score < 0.5


# ============================================================
# Test 5 - 3/3 REJECT -> REJECT
# ============================================================

def test_consensus_3of3_reject():
    M = _import_modules()
    engine = M["ConsensusEngine"]()
    responses = [
        _make_response("codex", "openai", "REJECT"),
        _make_response("gemini", "google", "REJECT"),
        _make_response("grok", "xai", "REJECT"),
    ]
    score = engine.compute_consensus(responses)
    assert score.tier_recommendation == M["TierRecommendation"].REJECT
    assert score.convergence_class == M["ConvergenceClass"].THREE_OF_THREE_CONVERGENT
    assert score.overall_score <= 0.1

    # Mehrheits-REJECT: 2/3 REJECT + 1/3 ADOPT -> noch REJECT
    responses_majority = [
        _make_response("codex", "openai", "REJECT"),
        _make_response("gemini", "google", "REJECT"),
        _make_response("grok", "xai", "ADOPT"),
    ]
    score_majority = engine.compute_consensus(responses_majority)
    assert score_majority.tier_recommendation == M["TierRecommendation"].REJECT


# ============================================================
# Test 6 - 2 LLMs verfuegbar -> max SIM_HARDENED
# ============================================================

def test_consensus_2llm_only_max_sim_hardened():
    M = _import_modules()
    engine = M["ConsensusEngine"]()
    # Nur 2 verfuegbar (1 unavailable)
    responses = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "ADOPT"),
        _make_response("grok", "xai", "ADOPT", available=False),
    ]
    score = engine.compute_consensus(responses)
    # 2/2 ADOPT -> max SIM_HARDENED (nicht HARDENED weil nur 2 verfuegbar)
    assert score.tier_recommendation == M["TierRecommendation"].SIM_HARDENED
    assert score.n_providers_available == 2
    assert score.n_providers_total == 3
    assert score.convergence_class == M["ConvergenceClass"].ONE_OF_TWO_PARTIAL


# ============================================================
# Test 7 - 1 LLM verfuegbar -> max CONDITIONAL
# ============================================================

def test_consensus_1llm_only_max_conditional():
    M = _import_modules()
    engine = M["ConsensusEngine"]()
    responses = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "ADOPT", available=False),
        _make_response("grok", "xai", "ADOPT", available=False),
    ]
    score = engine.compute_consensus(responses)
    assert score.tier_recommendation == M["TierRecommendation"].CONDITIONAL
    assert score.n_providers_available == 1
    assert score.convergence_class == M["ConvergenceClass"].SINGLE_LLM


# ============================================================
# Test 8 - G3.2-Divergenz-Proxy-Detection
# ============================================================

def test_g3_2_divergence_proxy_detection():
    M = _import_modules()
    detect = M["detect_g3_2_divergence_proxies"]

    # Fall A: 3 verschiedene Familien -> family-diversity + lineage-distance
    responses_a = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "ADOPT"),
        _make_response("grok", "xai", "ADOPT"),
    ]
    proxies_a = detect(responses_a)
    assert "family-diversity" in proxies_a
    assert "lineage-distance" in proxies_a

    # Fall B: 2 OpenAI-Lineage Provider (codex + copilot) -> nur 1 lineage-group
    # codex (openai) + copilot (github_openai = same openai-line) -> family-diversity ja, lineage-distance nein
    responses_b = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("copilot", "github_openai", "ADOPT"),
    ]
    proxies_b = detect(responses_b)
    # 2 verschiedene families (openai, github_openai)
    assert "family-diversity" in proxies_b
    # ABER: beide sind in openai-line -> nur 1 lineage-group
    assert "lineage-distance" not in proxies_b

    # Fall C: nur 1 Provider -> keine Proxies (n<2)
    responses_c = [
        _make_response("codex", "openai", "ADOPT"),
    ]
    proxies_c = detect(responses_c)
    assert proxies_c == []

    # Fall D: 0 verfuegbar -> keine Proxies
    responses_d = [
        _make_response("codex", "openai", "ADOPT", available=False),
    ]
    proxies_d = detect(responses_d)
    assert proxies_d == []


# ============================================================
# Test 9 - 0 Provider verfuegbar -> NO_PROVIDERS / REJECT
# ============================================================

def test_consensus_no_providers_available():
    M = _import_modules()
    engine = M["ConsensusEngine"]()

    # Alle responses unavailable
    responses = [
        _make_response("codex", "openai", "ADOPT", available=False),
        _make_response("gemini", "google", "ADOPT", available=False),
    ]
    score = engine.compute_consensus(responses)
    assert score.convergence_class == M["ConvergenceClass"].NO_PROVIDERS
    assert score.tier_recommendation == M["TierRecommendation"].REJECT
    assert score.n_providers_available == 0
    assert score.overall_score == 0.0

    # Empty list
    score_empty = engine.compute_consensus([], n_providers_total=4)
    assert score_empty.convergence_class == M["ConvergenceClass"].NO_PROVIDERS
    assert score_empty.n_providers_available == 0
    assert score_empty.n_providers_total == 4


# ============================================================
# Test 10 - ConsensusScore.to_dict serialisiert sauber
# ============================================================

def test_consensus_score_to_dict():
    M = _import_modules()
    engine = M["ConsensusEngine"]()
    responses = [
        _make_response("codex", "openai", "ADOPT"),
        _make_response("gemini", "google", "ADOPT"),
        _make_response("grok", "xai", "ADOPT"),
    ]
    score = engine.compute_consensus(responses)
    d = score.to_dict()
    assert "overall_score" in d
    assert "convergence_class" in d
    assert "tier_recommendation" in d
    assert "g3_2_divergence_proxies" in d
    assert "n_providers_available" in d
    assert "n_providers_total" in d
    assert "verdict_counts" in d
    assert "llm_responses" in d
    assert isinstance(d["llm_responses"], list)
    assert len(d["llm_responses"]) == 3
    # Verdict-Werte als Strings serialisiert
    assert all("verdict" in r for r in d["llm_responses"])
