# DF-W8-12 QualityGate Tests [CRUX-MK]
"""
Tests fuer QualityGate-Hauptpfad: check, K16-Mutex, LC1-Mode-Detection, STOP.flag.

Mocks: Provider mit mock_mode=True (LLMProvider unterstuetzt mock_mode + mock_response).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ============================================================
# Helpers
# ============================================================

def _import_modules():
    from src.llm_provider import (
        LLMProvider,
        LLMResponse,
        Verdict,
        CodexProvider,
        GeminiProvider,
        CopilotProvider,
        GrokProvider,
    )
    from src.consensus_engine import (
        ConsensusEngine,
        ConvergenceClass,
        TierRecommendation,
    )
    from src.quality_gate import (
        QualityGate,
        GateMode,
        CircuitBreaker,
        CircuitBreakerState,
        K16ConcurrentSpawnError,
        QualityGateError,
    )
    return {
        "LLMProvider": LLMProvider,
        "LLMResponse": LLMResponse,
        "Verdict": Verdict,
        "CodexProvider": CodexProvider,
        "GeminiProvider": GeminiProvider,
        "CopilotProvider": CopilotProvider,
        "GrokProvider": GrokProvider,
        "ConsensusEngine": ConsensusEngine,
        "ConvergenceClass": ConvergenceClass,
        "TierRecommendation": TierRecommendation,
        "QualityGate": QualityGate,
        "GateMode": GateMode,
        "CircuitBreaker": CircuitBreaker,
        "CircuitBreakerState": CircuitBreakerState,
        "K16ConcurrentSpawnError": K16ConcurrentSpawnError,
        "QualityGateError": QualityGateError,
    }


@pytest.fixture
def tmp_gate_paths(tmp_path):
    return {
        "jsonl": tmp_path / "audit.jsonl",
        "lock": tmp_path / "gate.lock",
        "stop": tmp_path / "gate.stop",
        "health": tmp_path / "health.json",
    }


def _build_mock_providers(verdicts: dict[str, str]):
    """Erzeugt 3-4 Provider in mock_mode mit pre-definierten Verdicts.

    Args:
        verdicts: Dict provider_name -> verdict-string ("ADOPT" / "REJECT" / ...).
    """
    M = _import_modules()
    Verdict = M["Verdict"]

    providers = []
    if "codex" in verdicts:
        providers.append(
            M["CodexProvider"](
                mock_mode=True,
                mock_response=f"{verdicts['codex']} - mocked codex",
                mock_verdict=Verdict(verdicts["codex"]),
            )
        )
    if "gemini" in verdicts:
        providers.append(
            M["GeminiProvider"](
                mock_mode=True,
                mock_response=f"{verdicts['gemini']} - mocked gemini",
                mock_verdict=Verdict(verdicts["gemini"]),
            )
        )
    if "copilot" in verdicts:
        providers.append(
            M["CopilotProvider"](
                mock_mode=True,
                mock_response=f"{verdicts['copilot']} - mocked copilot",
                mock_verdict=Verdict(verdicts["copilot"]),
            )
        )
    if "grok" in verdicts:
        providers.append(
            M["GrokProvider"](
                mock_mode=True,
                mock_response=f"{verdicts['grok']} - mocked grok",
                mock_verdict=Verdict(verdicts["grok"]),
            )
        )
    return providers


@pytest.fixture
def gate_full(tmp_gate_paths):
    """Full-Mode QualityGate mit 4 mocked Provider (alle ADOPT)."""
    M = _import_modules()
    providers = _build_mock_providers(
        {"codex": "ADOPT", "gemini": "ADOPT", "copilot": "ADOPT", "grok": "ADOPT"}
    )
    return M["QualityGate"](
        providers=providers,
        jsonl_audit_path=tmp_gate_paths["jsonl"],
        lock_dir=tmp_gate_paths["lock"],
        stop_flag_path=tmp_gate_paths["stop"],
        health_file_path=tmp_gate_paths["health"],
        skip_mutex_for_tests=True,
        engine_pgrep_check=False,
    )


# ============================================================
# Test 1 - 3-LLM-Konsens Full-Mode -> HARDENED-Tier
# ============================================================

def test_gate_check_3llm_consensus_full_mode(gate_full, tmp_gate_paths):
    M = _import_modules()
    score = gate_full.check("Soll Hotel-Preis-Update angewendet werden?")
    # 4/4 ADOPT mit verschiedenen Familien -> HARDENED
    assert score.tier_recommendation == M["TierRecommendation"].HARDENED
    assert score.n_providers_available == 4
    assert score.n_providers_total == 4
    assert "family-diversity" in score.g3_2_divergence_proxies
    assert "lineage-distance" in score.g3_2_divergence_proxies

    # Audit-Log enthaelt Eintrag
    assert tmp_gate_paths["jsonl"].exists()
    lines = tmp_gate_paths["jsonl"].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1


# ============================================================
# Test 2 - K16 Mutex blockt 2. Spawn
# ============================================================

def test_k16_mutex_blocks_concurrent_spawn(tmp_gate_paths):
    M = _import_modules()
    providers = _build_mock_providers({"codex": "ADOPT"})

    gate_a = M["QualityGate"](
        providers=providers,
        jsonl_audit_path=tmp_gate_paths["jsonl"],
        lock_dir=tmp_gate_paths["lock"],
        stop_flag_path=tmp_gate_paths["stop"],
        skip_mutex_for_tests=False,
        engine_pgrep_check=False,
    )
    ok_a = gate_a.acquire_mutex()
    assert ok_a is True
    assert tmp_gate_paths["lock"].exists()

    try:
        gate_b = M["QualityGate"](
            providers=providers,
            jsonl_audit_path=tmp_gate_paths["jsonl"],
            lock_dir=tmp_gate_paths["lock"],
            stop_flag_path=tmp_gate_paths["stop"],
            skip_mutex_for_tests=False,
            engine_pgrep_check=False,
        )
        ok_b = gate_b.acquire_mutex()
        assert ok_b is False
    finally:
        gate_a.release_mutex()
        assert not tmp_gate_paths["lock"].exists()


# ============================================================
# Test 3 - LC1 Degraded-Mode: Grok unavailable (cooldown)
# ============================================================

def test_lc1_degraded_mode_grok_unavailable(tmp_gate_paths):
    M = _import_modules()

    # 3 Provider in mock_mode, Grok-Auth-Check return False simuliert via patch
    providers = _build_mock_providers(
        {"codex": "ADOPT", "gemini": "ADOPT", "copilot": "ADOPT"}
    )
    # Real-Mode Grok hinzufuegen aber auth_check returnt False
    grok = M["GrokProvider"](mock_mode=False)  # echter mode

    # patch grok.auth_check -> False (simuliert Cooldown)
    with patch.object(grok, "auth_check", return_value=False):
        providers_with_grok = providers + [grok]

        gate = M["QualityGate"](
            providers=providers_with_grok,
            jsonl_audit_path=tmp_gate_paths["jsonl"],
            lock_dir=tmp_gate_paths["lock"],
            stop_flag_path=tmp_gate_paths["stop"],
            skip_mutex_for_tests=True,
            engine_pgrep_check=False,
        )

        mode = gate.current_mode()
        # 3 Provider verfuegbar (Grok auth fail) -> degraded_grok
        assert mode == M["GateMode"].DEGRADED_GROK


def test_lc1_degraded_2llm_mode(tmp_gate_paths):
    M = _import_modules()
    # 2 mocked + 2 echte mit Auth-Fail
    providers_mock = _build_mock_providers({"codex": "ADOPT", "gemini": "ADOPT"})
    copilot_real = M["CopilotProvider"](mock_mode=False)
    grok_real = M["GrokProvider"](mock_mode=False)
    with patch.object(copilot_real, "auth_check", return_value=False), \
         patch.object(grok_real, "auth_check", return_value=False):
        providers = providers_mock + [copilot_real, grok_real]

        gate = M["QualityGate"](
            providers=providers,
            jsonl_audit_path=tmp_gate_paths["jsonl"],
            lock_dir=tmp_gate_paths["lock"],
            stop_flag_path=tmp_gate_paths["stop"],
            skip_mutex_for_tests=True,
            engine_pgrep_check=False,
        )
        assert gate.current_mode() == M["GateMode"].DEGRADED_2LLM


# ============================================================
# Test 4 - STOP.flag blockt check
# ============================================================

def test_stop_flag_blocks_check(gate_full, tmp_gate_paths):
    M = _import_modules()

    # STOP.flag setzen
    tmp_gate_paths["stop"].write_text("stop", encoding="utf-8")

    score = gate_full.check("any prompt")
    # NO_PROVIDERS / REJECT bei STOP.flag
    assert score.tier_recommendation == M["TierRecommendation"].REJECT
    assert score.n_providers_available == 0
    assert score.convergence_class == M["ConvergenceClass"].NO_PROVIDERS

    # Mode-Detection -> STOPPED
    mode = gate_full.current_mode()
    assert mode == M["GateMode"].STOPPED


# ============================================================
# Test 5 - Health-Check liefert dict mit score
# ============================================================

def test_health_check_full_score(gate_full):
    health = gate_full.health_check()
    assert "score" in health
    assert "n_providers_available" in health
    assert "n_providers_total" in health
    assert "providers" in health
    assert "mode" in health
    assert health["n_providers_total"] == 4
    # 4 Provider in mock_mode -> alle "verfuegbar"
    assert health["n_providers_available"] == 4
    assert health["score"] >= 0.9


# ============================================================
# Test 6 - Circuit-Breaker open nach 3 fails
# ============================================================

def test_circuit_breaker_open_after_threshold():
    M = _import_modules()
    cb = M["CircuitBreaker"](open_threshold=3, half_open_test_interval_s=300.0)

    assert cb.state == M["CircuitBreakerState"].CLOSED
    assert cb.should_attempt() is True

    cb.record_failure()
    cb.record_failure()
    assert cb.state == M["CircuitBreakerState"].CLOSED

    cb.record_failure()
    assert cb.state == M["CircuitBreakerState"].OPEN
    assert cb.should_attempt() is False

    # Reset
    cb.state = M["CircuitBreakerState"].HALF_OPEN
    cb.record_success()
    assert cb.state == M["CircuitBreakerState"].CLOSED
    assert cb.fail_count == 0


# ============================================================
# Test 7 - REJECT-Mehrheit -> REJECT-Tier
# ============================================================

def test_check_with_majority_reject(tmp_gate_paths):
    M = _import_modules()
    providers = _build_mock_providers(
        {"codex": "REJECT", "gemini": "REJECT", "grok": "ADOPT"}
    )
    gate = M["QualityGate"](
        providers=providers,
        jsonl_audit_path=tmp_gate_paths["jsonl"],
        lock_dir=tmp_gate_paths["lock"],
        stop_flag_path=tmp_gate_paths["stop"],
        skip_mutex_for_tests=True,
        engine_pgrep_check=False,
    )
    score = gate.check("test prompt")
    assert score.tier_recommendation == M["TierRecommendation"].REJECT


# ============================================================
# Test 8 - Audit-Log wird append-only geschrieben (mehrere Runs)
# ============================================================

def test_audit_log_append_only(tmp_gate_paths):
    M = _import_modules()
    providers = _build_mock_providers(
        {"codex": "ADOPT", "gemini": "ADOPT", "grok": "ADOPT"}
    )
    gate = M["QualityGate"](
        providers=providers,
        jsonl_audit_path=tmp_gate_paths["jsonl"],
        lock_dir=tmp_gate_paths["lock"],
        stop_flag_path=tmp_gate_paths["stop"],
        skip_mutex_for_tests=True,
        engine_pgrep_check=False,
    )
    gate.check("prompt 1")
    gate.check("prompt 2")
    gate.check("prompt 3")

    lines = tmp_gate_paths["jsonl"].read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3

    # Jeder Eintrag ist gueltiges JSON mit prompt_hash
    import json
    for line in lines:
        entry = json.loads(line)
        assert "timestamp" in entry
        assert "prompt_hash" in entry
        assert "consensus" in entry
        assert "tier_recommendation" in entry["consensus"]


# ============================================================
# Test 9 - Stale-Lock-Cleanup nach 6h
# ============================================================

def test_stale_lock_cleanup(tmp_gate_paths):
    M = _import_modules()

    # Lock-Dir manuell anlegen + mtime alt setzen
    lock_dir = tmp_gate_paths["lock"]
    lock_dir.mkdir()
    old_time = time.time() - (8 * 3600)  # 8h alt
    os.utime(lock_dir, (old_time, old_time))

    providers = _build_mock_providers({"codex": "ADOPT"})
    gate = M["QualityGate"](
        providers=providers,
        jsonl_audit_path=tmp_gate_paths["jsonl"],
        lock_dir=lock_dir,
        stop_flag_path=tmp_gate_paths["stop"],
        skip_mutex_for_tests=False,
        engine_pgrep_check=False,
        lock_stale_age_h=6.0,
    )
    ok = gate.acquire_mutex()
    assert ok is True
    gate.release_mutex()


# ============================================================
# Test 10 - write_health_file persistiert
# ============================================================

def test_write_health_file(gate_full, tmp_gate_paths):
    ok = gate_full.write_health_file()
    assert ok is True
    assert tmp_gate_paths["health"].exists()

    import json
    data = json.loads(tmp_gate_paths["health"].read_text(encoding="utf-8"))
    assert "score" in data
    assert "timestamp" in data
    assert "mode" in data
    assert "n_providers_total" in data
