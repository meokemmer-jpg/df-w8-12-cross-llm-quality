---
type: spec-df
df_id: DF-W8-12
df_name: cross-llm-quality-gate
version: 0.1.0-PHASE-1
status: PHASE-1-SCAFFOLD
crux-mk: true
predecessor: SPEC-W8-DF-WAVE-1-SCAFFOLDS-2026-05-07.md
---

# DF-W8-12 Cross-LLM-Quality-Gate Spec [CRUX-MK]

## Beschreibung

Multi-Provider-LLM-Output-Konsens-Pruefung als **Pre-Production-Gate** fuer alle Plattform-LLM-Outputs:

- HeyLou-OTA AI-Personalisierung (Suchergebnisse, Recommendations)
- 9OS-ABS-Pricing-AI (Adaptive Booking Suggestions)
- 9OS-Voice-GSA (Voice-LLM-Outputs vor Mitarbeiter-Anzeige)

**Pattern-Reuse:**
- DF-86 NLM-Daily-Sync (Multi-Provider-Bash-Wrapper-Pattern)
- DF-95 Cross-LLM-Wargame-Engine (Konsens-Algorithmus + G3.2-Divergenz-Proxies)

**Gate-Funktion:** Vor jedem Production-Output prueft das Gate ob mindestens N von 4 LLMs konsensieren. Bei Konsens-Failure: BLOCK + Audit-Trail + Eskalation.

## Architektur

### Bash-Wrapper + Konsens-Engine + Quality-Gate

```
[Production-Caller]
        |
        | check(prompt, context) -> ConsensusScore
        v
[QualityGate] ----- K16 Mutex (lock + pgrep) -----+
        |                                          |
        | parallel-spawn (Bash-background)         |
        |                                          |
        +--> [LLMProvider:codex]   --+             |
        +--> [LLMProvider:gemini]  --+             |
        +--> [LLMProvider:copilot] --+             |
        +--> [LLMProvider:grok]    --+             |
                                     |              |
                       (timeout-aggregation)        |
                                     v              |
                            [ConsensusEngine]        |
                                     |              |
                            ConsensusScore          |
                                     |              |
                  +------------------+-+----+       |
                  v                    v    v       v
              jsonl-Audit         Postgres  Health-File
              (LC2 fallback)      (Phase-2) (LC5 indep)
```

### LLMProvider Abstract-Class (Bash-Wrapper-Pattern)

```python
class LLMProvider:
    name: str            # "codex" | "gemini" | "copilot" | "grok"
    family: str          # "openai" | "google" | "github_openai" | "xai"
    cli_command: str
    cli_args: list[str]
    cooldown_patterns: list[str]
    timeout_s: int

    def execute(self, prompt: str) -> LLMResponse: ...
    def auth_check(self) -> bool: ...
    def is_in_cooldown(self) -> bool: ...
```

**Implementations (Phase-1):**
- `CodexProvider` (`codex exec --skip-git-repo-check "<prompt>"`)
- `GeminiProvider` (`echo "<prompt>" | gemini -p stdin`)
- `CopilotProvider` (`copilot -p "<prompt>" --allow-all-tools`)
- `GrokProvider` (mock-mode in Phase-1; real MCP-call in Phase-2)

### LLMResponse (Provenance-Pflicht)

```python
@dataclass
class LLMResponse:
    provider: str         # "codex" | "gemini" | ...
    model: str            # "gpt-5.4" | "gemini-2.5-pro" | ...
    timestamp: str        # ISO-8601 UTC
    raw_text: str         # rohe LLM-Output
    verdict: str          # "ADOPT" | "MODIFY_LIGHT" | "MODIFY_STRONG" | "REJECT"
    duration_s: float
    cooldown_detected: bool
    error: str | None
```

### ConsensusScore Dataclass

```python
@dataclass
class ConsensusScore:
    overall_score: float          # 0.0 - 1.0
    convergence_class: ConvergenceClass
    llm_responses: list[LLMResponse]
    g3_2_divergence_proxies: list[str]
    tier_recommendation: TierRecommendation
    n_providers_available: int
    n_providers_total: int
```

### Konsens-Tier-Mapping (Spec-Tabelle)

| LLM-Pattern (Anzahl verfuegbarer Provider, Verdict-Konsens) | tier_recommendation |
|---|---|
| 3/3 ADOPT (mit 2+ G3.2 Proxies) | HARDENED |
| 3/3 ADOPT (1 G3.2 Proxy) | TWOOFTHREE_HARDENED |
| 3/3 MODIFY-light | SIM_HARDENED |
| 2/3 ADOPT/MODIFY-light | TWOOFTHREE_HARDENED |
| 1/3 ADOPT + 2/3 MODIFY-strong | CONDITIONAL |
| 3/3 REJECT (oder Mehrheit) | REJECT |
| 2/3 verfuegbar (1 down) | max SIM_HARDENED |
| 1/2 verfuegbar (2 down) | max CONDITIONAL |
| 0 Provider verfuegbar | NO_CONSENSUS / SINGLE_LLM mode error |

### G3.2 Divergenz-Proxies (Pflicht fuer HARDENED)

Mindestens 2 von 3 muessen erfuellt sein fuer HARDENED-Tier:

1. **Provider-Family-Diversity** — mindestens 2 verschiedene Familien (openai/google/github_openai/xai)
2. **Lineage-Distance** — Provider mit unterschiedlichen Base-Models (z.B. nicht 3x gpt-5)
3. **Token-Prob-Variance** (Phase-2) — Token-Wahrscheinlichkeits-Varianz > Schwelle T

Phase-1: nur Proxy 1+2 implementiert. Proxy 3 ist Phase-2-Item.

### QualityGate-API

```python
class QualityGate:
    def __init__(
        self,
        providers: list[LLMProvider],
        jsonl_audit_path: str | Path,
        stop_flag_path: str | Path,
        lock_dir: str | Path,
        health_file_path: str | Path,
        timeout_s: int = 60,
        skip_mutex_for_tests: bool = False,
        engine_pgrep_check: bool = True,
    )

    def check(self, prompt: str, context: dict | None = None) -> ConsensusScore

    # K16
    def acquire_mutex(self) -> bool
    def release_mutex(self) -> None
    def check_concurrent_engines(self) -> list[int]

    # LC1
    def current_mode(self) -> GateMode

    # LC3
    def get_circuit_breaker_for(self, provider_name: str) -> CircuitBreaker

    # LC5
    def health_check(self) -> dict
    def write_health_file(self) -> bool

    # STOP.flag
    def is_stopped(self) -> bool

    # Audit-Log
    def append_audit(self, score: ConsensusScore, prompt_hash: str) -> bool
```

## K11-K16 + LC1-LC5

Siehe `config.yaml`. Alle Felder Pflicht-vorhanden.

## Test-Pflicht (>=18 Tests)

### test_llm_provider.py (>= 6 Tests)

1. `test_provider_init_with_path` - Provider-Init mit cli_command-Path Pflicht
2. `test_auth_check_via_help_output` - help-output return-code 0 -> Auth ok
3. `test_cooldown_detection_rate_limit` - "rate-limit" in stderr -> cooldown=True
4. `test_timeout_enforcement` - Wenn subprocess timeout -> error captured
5. `test_bash_wrapper_args_correct` - subprocess.run wird mit erwarteten args aufgerufen
6. `test_response_parsing_basic` - rohe stdout wird in LLMResponse gepackt mit timestamp+provider+model

### test_consensus_engine.py (>= 8 Tests)

1. `test_consensus_3of3_adopt` - 3/3 ADOPT + 2+ G3.2 -> HARDENED
2. `test_consensus_3of3_modify_light` - 3/3 MODIFY_LIGHT -> SIM_HARDENED
3. `test_consensus_2of3_adopt` - 2/3 ADOPT + 1/3 MODIFY -> TWOOFTHREE_HARDENED
4. `test_consensus_1of3_adopt_2of3_modify_strong` - gemischt -> CONDITIONAL
5. `test_consensus_3of3_reject` - 3/3 REJECT -> REJECT
6. `test_consensus_2llm_only_max_sim_hardened` - 2 verfuegbar -> max SIM_HARDENED
7. `test_consensus_1llm_only_max_conditional` - 1 verfuegbar -> max CONDITIONAL
8. `test_g3_2_divergence_proxy_detection` - Provider-Family-check funktioniert

### test_quality_gate.py (>= 4 Tests)

1. `test_gate_check_3llm_consensus_full_mode` - mit Provider-Mocks: 3 ADOPT -> HARDENED-Tier
2. `test_k16_mutex_blocks_concurrent_spawn` - 2x acquire_mutex -> 2. blockt
3. `test_lc1_degraded_mode_grok_unavailable` - Grok-Mock down -> mode=degraded_grok
4. `test_stop_flag_blocks_check` - STOP.flag aktiv -> check returns early

## Phase-1 Scope (Heute)

- Phase-1 = Foundation-Skelett (Code + Tests + Spec)
- KEINE echten LLM-Calls in Tests (subprocess.run via unittest.mock)
- KEINE Postgres-Migration (Phase-2)
- KEINE Pipeline-Integration (Phase-3)
- KEIN Live-Mode (Phase-4)

## Aktivierung

NUR via STOP.flag-Mechanik:
- `/tmp/df-w8-12.stop` existiert -> Gate pausiert
- jsonl-Audit-Pfad muss schreibbar sein
- Mindestens 1 Provider muss verfuegbar (sonst NO_CONSENSUS)

## Phase-2 Pending-Items

- Konsens-Algo Verfeinerung (echtes 3OF4 + 2OF3 mit gewichteten Scores)
- Postgres-State (consensus_history Tabelle)
- Provider-Divergenz-Proxies erweitern (Lineage-Distance + Token-Prob-Variance)
- Replay-Mechanik (jsonl -> Postgres bei Recovery)
- Real Grok-MCP-Integration statt Mock-Mode
- Pre-Action-Verification-Hook (PocketOS-Lehre, K13)

## Phase-3 Pending-Items

- Integration als Pre-Production-Hook
- KMO-Pipeline-Approval-Gate-Erweiterung (cross_llm_quality_gate als Pflicht-Step)
- DF-W8-18-Lineage-Integration (Konsens-Events zur Lineage-DB)
- Cooldown-Recovery-Strategie (priorisierte Provider-Wiederherstellung)

## Phase-4 Pending-Items

- Shadow-Mode 14 Tage Live-Probe
- Production-Aktivierung
- launchd-Service (com.kemmer.df-w8-12.plist)
- Monitoring-Dashboard (Konsens-Rate / Provider-Outage-Frequency / Tier-Distribution)

## KMO-Pipeline-Approval-Gate-Hook-Spec (Phase-3)

```python
# In KMO-Pipeline:
from df_w8_12.quality_gate import QualityGate
from df_w8_12.consensus_engine import TierRecommendation

gate = QualityGate(...)

@pre_production_hook
def consensus_gate_check(prompt: str, context: dict) -> bool:
    score = gate.check(prompt, context)
    if score.tier_recommendation in (TierRecommendation.HARDENED,
                                     TierRecommendation.TWOOFTHREE_HARDENED):
        return True
    if score.tier_recommendation == TierRecommendation.SIM_HARDENED:
        # warn + allow
        log.warning(f"Konsens nur SIM_HARDENED: {score}")
        return True
    if score.tier_recommendation in (TierRecommendation.CONDITIONAL,
                                     TierRecommendation.REJECT):
        log.error(f"Gate-BLOCK: tier={score.tier_recommendation}")
        return False
    return False
```

[CRUX-MK]
