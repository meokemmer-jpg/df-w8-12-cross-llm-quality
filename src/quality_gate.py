# DF-W8-12 QualityGate [CRUX-MK]
"""
QualityGate-Hauptklasse: Multi-LLM-Konsens-Pruefung mit K16/LC1-LC5/STOP.flag.

Phase-1 Pflicht-Mechaniken:
- check(prompt, context) -> ConsensusScore
- Parallel Bash-Background-Spawn fuer 4 Provider (analog Welle-8-B Pattern)
- Timeout-Aggregation
- jsonl-Audit-Log
- K16 Concurrent-Spawn-Mutex (/tmp/df-w8-12.lock + pgrep)
- LC3 Circuit-Breaker pro Provider (3 fails -> open, 300s half-open)
- LC1 Graceful-Degradation (full / degraded_grok / degraded_2llm / standalone)
- LC5 Health-Check ohne Dependencies
- STOP.flag-Mechanik (single-command-override)

Phase-2 Pending:
- Postgres-State (consensus_history Tabelle)
- Pre-Action-Verification-Hook (PocketOS-Lehre, K13)
- Replay-Mechanik
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .consensus_engine import (
    ConsensusEngine,
    ConsensusScore,
    ConvergenceClass,
    TierRecommendation,
)
from .llm_provider import LLMProvider, LLMResponse, Verdict


# ============================================================
# Exceptions
# ============================================================

class K16ConcurrentSpawnError(SystemExit):
    """K16-VETO: parallele QualityGate-Instanz erkannt."""

    def __init__(self, message: str = "K16-VETO concurrent spawn detected"):
        super().__init__(3)
        self.message = message


class QualityGateError(RuntimeError):
    """Generic-Error fuer QualityGate (z.B. NO_PROVIDERS)."""


# ============================================================
# State Enums
# ============================================================

class GateMode(str, Enum):
    """LC1 Graceful-Degradation Modi (DF-W8-12-spezifisch)."""

    FULL = "full"                       # 4 Provider verfuegbar
    DEGRADED_GROK = "degraded_grok"      # Grok-Cooldown active -> 3 Provider
    DEGRADED_2LLM = "degraded_2llm"      # 2 Provider verfuegbar
    STANDALONE = "standalone"            # 1 Provider verfuegbar (max CONDITIONAL)
    NO_PROVIDERS = "no_providers"        # 0 Provider verfuegbar
    STOPPED = "stopped"                  # STOP.flag aktiv


class CircuitBreakerState(str, Enum):
    """LC3 Circuit-Breaker States (pro Provider)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ============================================================
# Circuit-Breaker (pro Provider)
# ============================================================

@dataclass
class CircuitBreaker:
    """LC3 Circuit-Breaker (closed -> open -> half-open).

    Pro Provider eine Instanz. Bei 3 Fails in Folge -> open. Nach 300s -> half-open-test.
    """

    timeout_s: float = 30.0
    open_threshold: int = 3
    half_open_test_interval_s: float = 300.0

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    fail_count: int = 0
    last_fail_ts: float | None = None
    last_open_ts: float | None = None

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = CircuitBreakerState.CLOSED

    def record_failure(self) -> None:
        self.fail_count += 1
        self.last_fail_ts = time.time()
        if self.fail_count >= self.open_threshold:
            self.state = CircuitBreakerState.OPEN
            self.last_open_ts = time.time()

    def should_attempt(self) -> bool:
        if self.state == CircuitBreakerState.CLOSED:
            return True
        if self.state == CircuitBreakerState.OPEN:
            if self.last_open_ts is None:
                return True
            elapsed = time.time() - self.last_open_ts
            if elapsed >= self.half_open_test_interval_s:
                self.state = CircuitBreakerState.HALF_OPEN
                return True
            return False
        return True


# ============================================================
# QualityGate
# ============================================================

class QualityGate:
    """Phase-1 QualityGate.

    Mechaniken:
    - K16 Mutex (lock_dir + pgrep)
    - LC1 Mode-Detection (full | degraded_grok | degraded_2llm | standalone | stopped)
    - LC3 Circuit-Breaker pro Provider
    - LC5 Health-Check independent
    - STOP.flag-Override
    - Parallel-Provider-Spawn (ThreadPool)
    - jsonl-Audit-Log
    """

    DEFAULT_LOCK_DIR = Path("/tmp/df-w8-12.lock")
    DEFAULT_STOP_FLAG = Path("/tmp/df-w8-12.stop")
    DEFAULT_HEALTH_FILE = Path("/tmp/df-w8-12-health.json")
    DEFAULT_JSONL_AUDIT = Path.home() / ".df-w8-12" / "quality-gate-audit.jsonl"
    DEFAULT_LOCK_STALE_AGE_H = 6.0
    ENGINE_PROCESS_NAME = "df-w8-12-engine"

    def __init__(
        self,
        providers: list[LLMProvider],
        jsonl_audit_path: str | Path | None = None,
        lock_dir: str | Path | None = None,
        stop_flag_path: str | Path | None = None,
        health_file_path: str | Path | None = None,
        lock_stale_age_h: float | None = None,
        timeout_s: int = 60,
        engine_pgrep_check: bool = True,
        skip_mutex_for_tests: bool = False,
        consensus_engine: ConsensusEngine | None = None,
        circuit_breakers: dict[str, CircuitBreaker] | None = None,
    ):
        """
        Args:
            providers: Liste der konfigurierten LLMProvider-Instanzen.
            jsonl_audit_path: jsonl-Audit-Pfad. None -> Default.
            lock_dir: Mutex-Dir.
            stop_flag_path: STOP.flag-Pfad.
            health_file_path: Health-Output-Pfad.
            lock_stale_age_h: Stale-Lock-Auto-Claim nach N Stunden.
            timeout_s: Per-Provider-Timeout (default 60).
            engine_pgrep_check: pgrep-Defense aktiv (Default true).
            skip_mutex_for_tests: NUR fuer Tests! Skipped K16-Mutex.
            consensus_engine: Optional pre-konfigurierte ConsensusEngine.
            circuit_breakers: Optional vorgegebene Circuit-Breakers pro Provider-Name.
        """
        self.providers = list(providers)
        self.jsonl_audit_path = Path(jsonl_audit_path or self.DEFAULT_JSONL_AUDIT)
        self.lock_dir = Path(lock_dir or self.DEFAULT_LOCK_DIR)
        self.stop_flag_path = Path(stop_flag_path or self.DEFAULT_STOP_FLAG)
        self.health_file_path = Path(health_file_path or self.DEFAULT_HEALTH_FILE)
        self.lock_stale_age_h = lock_stale_age_h or self.DEFAULT_LOCK_STALE_AGE_H
        self.timeout_s = timeout_s
        self.engine_pgrep_check = engine_pgrep_check
        self.skip_mutex_for_tests = skip_mutex_for_tests
        self.consensus_engine = consensus_engine or ConsensusEngine()
        self.circuit_breakers = circuit_breakers or {
            p.name: CircuitBreaker() for p in self.providers
        }
        # Sicherstellen: jeder Provider hat einen CB
        for p in self.providers:
            if p.name not in self.circuit_breakers:
                self.circuit_breakers[p.name] = CircuitBreaker()

        self._mutex_acquired = False
        self._jsonl_dir_ensured = False

    # ============================================================
    # K16 Concurrent-Spawn-Mutex
    # ============================================================

    def acquire_mutex(self) -> bool:
        """K16 atomic mkdir-Mutex + Stale-Cleanup.

        Returns:
            True wenn acquired. False wenn andere Instanz haelt.

        Raises:
            K16ConcurrentSpawnError bei pgrep-Defense-Veto.
        """
        if self.skip_mutex_for_tests:
            self._mutex_acquired = True
            return True

        # Stale-Cleanup
        if self.lock_dir.exists():
            try:
                stat = self.lock_dir.stat()
                age_s = time.time() - stat.st_mtime
                if age_s > self.lock_stale_age_h * 3600:
                    self._safe_remove_lock_dir()
            except Exception:
                pass

        # Atomic mkdir
        try:
            self.lock_dir.mkdir(exist_ok=False)
            self._mutex_acquired = True
        except FileExistsError:
            return False
        except Exception:
            return False

        # PID-Datei
        try:
            pid_file = self.lock_dir / "pid"
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass

        # pgrep-Defense
        if self.engine_pgrep_check:
            other_pids = self.check_concurrent_engines()
            if other_pids:
                self._safe_remove_lock_dir()
                self._mutex_acquired = False
                raise K16ConcurrentSpawnError(
                    f"K16-VETO concurrent engine pids={other_pids}"
                )

        return True

    def release_mutex(self) -> None:
        """Entfernt Lock-Dir."""
        if not self._mutex_acquired:
            return
        self._safe_remove_lock_dir()
        self._mutex_acquired = False

    def _safe_remove_lock_dir(self) -> None:
        try:
            pid_file = self.lock_dir / "pid"
            if pid_file.exists():
                pid_file.unlink()
            self.lock_dir.rmdir()
        except Exception:
            pass

    def check_concurrent_engines(self) -> list[int]:
        """K16 pgrep-Defense.

        Returns:
            Liste von PIDs anderer df-w8-12-Engines (excluding self).
        """
        try:
            my_pid = os.getpid()
            result = subprocess.run(
                ["pgrep", "-f", self.ENGINE_PROCESS_NAME],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode not in (0, 1):
                return []
            pids = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                try:
                    pid = int(line)
                    if pid != my_pid:
                        pids.append(pid)
                except ValueError:
                    pass
            return pids
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []
        except Exception:  # pragma: no cover - defensive
            return []

    # ============================================================
    # STOP.flag
    # ============================================================

    def is_stopped(self) -> bool:
        """Prueft STOP.flag (single-command-Override)."""
        return self.stop_flag_path.exists()

    # ============================================================
    # LC1 Mode-Detection
    # ============================================================

    def current_mode(self) -> GateMode:
        """Bestimmt aktuellen Mode.

        Phase-1 Heuristik:
        - STOP.flag aktiv -> STOPPED
        - 0 Provider verfuegbar -> NO_PROVIDERS
        - 1 verfuegbar -> STANDALONE
        - 2 verfuegbar -> DEGRADED_2LLM
        - 3 verfuegbar (und Grok cooldown) -> DEGRADED_GROK
        - 4 verfuegbar -> FULL
        """
        if self.is_stopped():
            return GateMode.STOPPED

        available = self._available_providers()
        n = len(available)

        if n == 0:
            return GateMode.NO_PROVIDERS
        if n == 1:
            return GateMode.STANDALONE
        if n == 2:
            return GateMode.DEGRADED_2LLM
        if n == 3:
            # Wenn Grok nicht verfuegbar -> degraded_grok
            grok_avail = any(p.name == "grok" for p in available)
            if not grok_avail:
                return GateMode.DEGRADED_GROK
            return GateMode.FULL  # 3 Provider mit Grok = FULL falls nur 3 konfiguriert
        # n >= 4
        return GateMode.FULL

    def _available_providers(self) -> list[LLMProvider]:
        """Liste der Provider die laut Circuit-Breaker + Auth-Check verfuegbar sind."""
        result = []
        for p in self.providers:
            cb = self.circuit_breakers.get(p.name)
            if cb is not None and not cb.should_attempt():
                continue
            # Phase-1: vertraue auth_check als Verfuegbarkeits-Indikator
            try:
                if p.auth_check():
                    result.append(p)
            except Exception:
                continue
        return result

    # ============================================================
    # check (Hauptpfad)
    # ============================================================

    def check(self, prompt: str, context: dict[str, Any] | None = None) -> ConsensusScore:
        """Fuehrt Cross-LLM-Quality-Check durch.

        Args:
            prompt: LLM-Prompt fuer alle Provider (gleicher Prompt zur Vergleichbarkeit).
            context: Optional Audit-Kontext (z.B. caller-source, prompt-typ).

        Returns:
            ConsensusScore mit Tier-Empfehlung.

        Raises:
            QualityGateError wenn STOP.flag aktiv ODER 0 Provider verfuegbar.
        """
        # STOP.flag-Check
        if self.is_stopped():
            return ConsensusScore(
                overall_score=0.0,
                convergence_class=ConvergenceClass.NO_PROVIDERS,
                llm_responses=[],
                g3_2_divergence_proxies=[],
                tier_recommendation=TierRecommendation.REJECT,
                n_providers_available=0,
                n_providers_total=len(self.providers),
                verdict_counts={},
            )

        available = self._available_providers()
        if len(available) == 0:
            # Audit-Log auch bei NO_PROVIDERS
            empty_score = ConsensusScore(
                overall_score=0.0,
                convergence_class=ConvergenceClass.NO_PROVIDERS,
                llm_responses=[],
                g3_2_divergence_proxies=[],
                tier_recommendation=TierRecommendation.REJECT,
                n_providers_available=0,
                n_providers_total=len(self.providers),
                verdict_counts={},
            )
            try:
                self.append_audit(empty_score, self._hash_prompt(prompt), context)
            except Exception:
                pass
            return empty_score

        # Parallel-Spawn aller available Provider
        responses = self._spawn_parallel(available, prompt)

        # Circuit-Breaker-Update
        for r in responses:
            cb = self.circuit_breakers.get(r.provider)
            if cb is None:
                continue
            if r.available and r.error is None and not r.cooldown_detected:
                cb.record_success()
            else:
                cb.record_failure()

        # Konsens berechnen
        score = self.consensus_engine.compute_consensus(
            responses=responses,
            n_providers_total=len(self.providers),
        )

        # Audit-Log
        try:
            self.append_audit(score, self._hash_prompt(prompt), context)
        except Exception:
            pass

        return score

    # ============================================================
    # Parallel-Spawn (ThreadPool)
    # ============================================================

    def _spawn_parallel(
        self, providers: list[LLMProvider], prompt: str
    ) -> list[LLMResponse]:
        """Fuert Provider-execute parallel via ThreadPoolExecutor aus.

        Pattern analog Welle-8-B parallel-subagent-dispatch.

        Args:
            providers: Available providers.
            prompt: LLM-Prompt.

        Returns:
            Liste von LLMResponse, sortiert nach Provider-Index.
        """
        responses: list[LLMResponse | None] = [None] * len(providers)
        if not providers:
            return []

        max_workers = min(len(providers), 8)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._execute_single, p, prompt): idx
                for idx, p in enumerate(providers)
            }
            for fut in as_completed(futures, timeout=self.timeout_s + 10):
                idx = futures[fut]
                try:
                    responses[idx] = fut.result(timeout=self.timeout_s + 5)
                except Exception as e:
                    # Defensive: Provider-Fehler zu LLMResponse
                    responses[idx] = LLMResponse(
                        provider=providers[idx].name,
                        model=providers[idx].default_model,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        raw_text="",
                        verdict=Verdict.UNKNOWN,
                        duration_s=0.0,
                        cooldown_detected=False,
                        error=f"thread-pool error: {e}",
                        family=providers[idx].family,
                        available=False,
                    )

        # None-Slots durch error-Response ersetzen (z.B. wenn timeout)
        final: list[LLMResponse] = []
        for idx, r in enumerate(responses):
            if r is None:
                p = providers[idx]
                final.append(
                    LLMResponse(
                        provider=p.name,
                        model=p.default_model,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        raw_text="",
                        verdict=Verdict.UNKNOWN,
                        duration_s=0.0,
                        cooldown_detected=False,
                        error="aggregation timeout",
                        family=p.family,
                        available=False,
                    )
                )
            else:
                final.append(r)
        return final

    def _execute_single(self, provider: LLMProvider, prompt: str) -> LLMResponse:
        """Fuert ein Provider.execute mit Circuit-Breaker-Wrapper aus."""
        cb = self.circuit_breakers.get(provider.name)
        if cb is not None and not cb.should_attempt():
            return LLMResponse(
                provider=provider.name,
                model=provider.default_model,
                timestamp=datetime.now(timezone.utc).isoformat(),
                raw_text="",
                verdict=Verdict.UNKNOWN,
                duration_s=0.0,
                cooldown_detected=False,
                error="circuit-breaker open",
                family=provider.family,
                available=False,
            )
        return provider.execute(prompt)

    # ============================================================
    # jsonl-Audit
    # ============================================================

    def _ensure_jsonl_dir(self) -> None:
        if self._jsonl_dir_ensured:
            return
        self.jsonl_audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._jsonl_dir_ensured = True

    def append_audit(
        self,
        score: ConsensusScore,
        prompt_hash: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Appended Konsens-Result an jsonl-Audit-Log.

        Args:
            score: ConsensusScore.
            prompt_hash: SHA256 des Prompts.
            context: Optional Audit-Kontext.

        Returns:
            True bei Erfolg.
        """
        try:
            self._ensure_jsonl_dir()
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "prompt_hash": prompt_hash,
                "context": context or {},
                "consensus": score.to_dict(),
            }
            line = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
            with self.jsonl_audit_path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
            return True
        except Exception:
            return False

    @staticmethod
    def _hash_prompt(prompt: str) -> str:
        """SHA256 des Prompts (idempotent-key fuer Audit + Replay)."""
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    # ============================================================
    # LC5 Health-Check
    # ============================================================

    def health_check(self) -> dict[str, Any]:
        """LC5 Health-Check.

        Returns:
            Dict mit Health-Status. Score [0.0-1.0].
        """
        stopped = self.is_stopped()
        jsonl_ok = self._jsonl_writable()

        provider_health: dict[str, dict[str, Any]] = {}
        n_avail = 0
        for p in self.providers:
            cb = self.circuit_breakers.get(p.name, CircuitBreaker())
            try:
                auth_ok = p.auth_check()
            except Exception:
                auth_ok = False
            cb_open = not cb.should_attempt()
            available = auth_ok and not cb_open
            if available:
                n_avail += 1
            provider_health[p.name] = {
                "auth_ok": auth_ok,
                "circuit_breaker_state": cb.state.value,
                "circuit_breaker_fail_count": cb.fail_count,
                "available": available,
                "family": p.family,
            }

        # eigene Funktion = mindestens jsonl + 1 Provider
        own_function_ok = jsonl_ok and not stopped and n_avail >= 1

        if not own_function_ok:
            score = 0.0
        elif n_avail >= 4:
            score = 1.0
        elif n_avail == 3:
            score = 0.85
        elif n_avail == 2:
            score = 0.7
        else:
            score = 0.5

        return {
            "score": score,
            "jsonl_ok": jsonl_ok,
            "stopped": stopped,
            "n_providers_available": n_avail,
            "n_providers_total": len(self.providers),
            "providers": provider_health,
            "mode": self.current_mode().value,
            "own_function_ok": own_function_ok,
        }

    def _jsonl_writable(self) -> bool:
        try:
            self._ensure_jsonl_dir()
            test_path = self.jsonl_audit_path.with_suffix(".test")
            test_path.write_text("test\n", encoding="utf-8")
            test_path.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def write_health_file(self) -> bool:
        """Schreibt Health-Status nach health_file_path."""
        try:
            self.health_file_path.parent.mkdir(parents=True, exist_ok=True)
            data = self.health_check()
            data["timestamp"] = time.time()
            self.health_file_path.write_text(
                json.dumps(data, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            return True
        except Exception:
            return False

    # ============================================================
    # Context Manager
    # ============================================================

    def __enter__(self) -> "QualityGate":
        if not self.skip_mutex_for_tests:
            ok = self.acquire_mutex()
            if not ok:
                raise K16ConcurrentSpawnError(
                    "K16-VETO mutex acquisition failed"
                )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release_mutex()
