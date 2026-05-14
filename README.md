# DF-W8-12 Cross-LLM-Quality-Gate [CRUX-MK]

Foundation-DF fuer **Multi-Provider-LLM-Output-Konsens-Pruefung** als Pre-Production-Gate.

**Status:** Phase-1 Scaffold (Welle-8-E DF-Wave-1). Build-Trigger 2026-05-07 architekt-autonom.
**Predecessor-Spec:** `branch-hub/blueprints/SPEC-W8-DF-WAVE-1-SCAFFOLDS-2026-05-07.md`
**Pattern-Reuse:** DF-86 (NLM-Daily-Sync) + DF-95 (Cross-LLM-Wargame-Engine)

## Was er macht

Vor jedem Production-LLM-Output (HeyLou-OTA AI-Personalisierung, 9OS-ABS-Pricing-AI, 9OS-Voice-GSA) prueft das Gate ob mindestens N von 4 LLMs konsensieren:

1. **Codex** (OpenAI-Familie, gpt-5.x via `codex exec`)
2. **Gemini** (Google-Familie, gemini-2.5-pro via `gemini -p`)
3. **Copilot** (GitHub-OpenAI-Familie, via `copilot -p`)
4. **Grok** (xAI-Familie, via grok-mcp; Phase-1: mock-mode default)

Pro LLM wird:
- Bash-Wrapper-Subprocess gespawned (parallel via ThreadPool)
- Auth-Check + Cooldown-Detection durchgefuehrt
- Verdict klassifiziert (ADOPT / MODIFY_LIGHT / MODIFY_STRONG / REJECT / UNKNOWN)
- Provenance dokumentiert (provider+model+timestamp)

Aggregat-Output:
- `ConsensusScore` mit `tier_recommendation` (HARDENED / TWOOFTHREE_HARDENED / SIM_HARDENED / CONDITIONAL / REJECT)
- `convergence_class` (3OF3-CONVERGENT / 2OF3-CONVERGENT / 1OF2-PARTIAL / NO-CONSENSUS / SINGLE-LLM)
- `g3_2_divergence_proxies` (family-diversity, lineage-distance)
- `verdict_counts` pro Klasse

## Tier-Mapping (Spec-Tabelle)

| Pattern (Anzahl Verfuegbar / Konsens) | tier_recommendation |
|---|---|
| 3+/3+ ADOPT mit 2+ G3.2 Proxies | HARDENED |
| 3+/3+ ADOPT mit 1 G3.2 Proxy | TWOOFTHREE_HARDENED |
| 3+/3+ ADOPT ohne Proxy | SIM_HARDENED |
| 3/3 MODIFY_LIGHT | SIM_HARDENED |
| 2/3 zustimmend mit Proxy | TWOOFTHREE_HARDENED |
| 2/3 zustimmend ohne Proxy | SIM_HARDENED |
| 1/3 ADOPT + 2/3 MODIFY_STRONG | CONDITIONAL |
| 3/3 oder Mehrheit REJECT | REJECT |
| 2 verfuegbar (1 down) | max SIM_HARDENED |
| 1 verfuegbar (2 down) | max CONDITIONAL |
| 0 verfuegbar | NO_PROVIDERS / REJECT |

## Quick-Start (Phase-1)

```bash
cd ~/Projects/dark-factories/df-w8-12-cross-llm-quality
pip install pytest  # Pydantic NICHT erforderlich (dataclass-Fallback)
pytest tests/ -v    # 29 Tests, alle passing
```

### Code-Snippet

```python
from src.llm_provider import make_default_providers
from src.quality_gate import QualityGate

# Provider-Set (Grok in mock-mode)
providers = make_default_providers(enable_mock_mode=True)

# Gate initialisieren
gate = QualityGate(
    providers=providers,
    jsonl_audit_path="~/.df-w8-12/audit.jsonl",
    skip_mutex_for_tests=False,
    engine_pgrep_check=True,
)

# Mit Mutex (Production-Pattern)
with gate:
    score = gate.check(
        prompt="Soll Hotel-Preis-Update fuer Hildesheim auf EUR 89 gesetzt werden?",
        context={"caller": "9os-abs-pricing", "tenant": "hotel-hildesheim"},
    )

print(f"Tier: {score.tier_recommendation.value}")
print(f"Convergence: {score.convergence_class.value}")
print(f"Score: {score.overall_score:.2f}")
print(f"G3.2 Proxies: {score.g3_2_divergence_proxies}")
```

## Architektur-Modi (LC1 Graceful-Degradation)

| Mode | Provider verfuegbar | Use-Case |
|------|---------------------|----------|
| `full` | 4 | Production normal (Codex+Gemini+Copilot+Grok) |
| `degraded_grok` | 3 (Grok cooldown) | Grok-Cooldown active -> max TWOOFTHREE_HARDENED |
| `degraded_2llm` | 2 | 2 Provider down -> max SIM_HARDENED |
| `standalone` | 1 | 3 Provider down -> max CONDITIONAL |
| `no_providers` | 0 | 4 Provider down -> REJECT (Error-Mode) |
| `stopped` | n/a | STOP.flag aktiv |

## K11-K16 Akzeptanz-Kriterien

Alle Pflicht-Felder in `config.yaml`:
- **K11** Cascade-Containment: hard, blast_radius=1
- **K12** Distillation-Resistenz: provenance_required=true, output_feeds_into_training=false
- **K13** Independent-Ground-Truth: github-actions-test-runner als external_anchor + pre_action_domain_check (PocketOS-Lehre)
- **K14** Human-Override-Decay: STOP.flag, weekly Martin-Review
- **K15** Entropy-Budget: 800 LOC, justified by 65k EUR/J + Q_0-Schutz
- **K16** Concurrent-Spawn-Mutex: lock_dir + pgrep + EXIT_INT_TERM
- **K11.b** Pipeline-Cost-Estimate: quota_budget_ceiling=50 LLM-Calls/Run

## LC1-LC5 Lose-Coupling

Alle Pflicht-Felder in `config.yaml`:
- **LC1** Graceful-Degradation: 4+1 Modi (full/degraded_grok/degraded_2llm/standalone/stopped)
- **LC2** Direct-Mode-Capability: 0.6 (1-LLM-Fallback)
- **LC3** Circuit-Breaker pro Provider: 30s timeout, 3 fails -> open, 300s half-open
- **LC4** Failure-Isolation: state externalized (jsonl), idempotent, separate DLQ pro Provider
- **LC5** Health-Check: independent (eigene Funktion = jsonl + 1 Provider), score 0.7 in degraded mode

## Test-Pflicht (Phase-1)

```bash
pytest tests/ -v
```

- `tests/test_llm_provider.py` - 8 Tests (Provider-Init, Auth, Cooldown, Timeout, Bash-Wrapper, Verdict, Mock-Mode, Default-Set)
- `tests/test_consensus_engine.py` - 10 Tests (3OF3, MODIFY_LIGHT, 2OF3, CONDITIONAL, REJECT, 2-LLM, 1-LLM, G3.2-Proxies, NO-PROVIDERS, to_dict)
- `tests/test_quality_gate.py` - 11 Tests (Gate-check, K16-Mutex, LC1-Modes, STOP.flag, Health, Circuit-Breaker, REJECT, Audit-Log, Stale-Lock, Health-File, Degraded-2LLM)

**Status:** alle 29 Tests passing.

## STOP.flag-Mechanik (K14 Override)

```bash
# Gate pausieren (single-command-override):
touch /tmp/df-w8-12.stop

# Re-aktivieren:
rm /tmp/df-w8-12.stop
```

Bei aktivem STOP.flag: `gate.check()` returnt `tier_recommendation=REJECT` mit `convergence_class=NO_PROVIDERS` (Caller muss Production-Aktion blockieren).

## G3.2 Divergenz-Proxies (Phase-1)

Mindestens 2 von 3 Proxies sind erforderlich fuer **HARDENED**-Tier:

1. **family-diversity** (Phase-1 implementiert) - mindestens 2 verschiedene Provider-Familien
2. **lineage-distance** (Phase-1 implementiert) - mindestens 2 verschiedene Base-Model-Lineages
   - Gleicher Lineage-Group: codex (openai-line) + copilot (openai-line) -> NICHT unabhaengig
   - Verschiedene Lineage-Groups: codex + gemini + grok -> 3 unabhaengige Lineages
3. **token-prob-variance** (Phase-2) - Token-Wahrscheinlichkeits-Varianz > Schwelle T

Phase-1 prueft Proxy 1+2. Proxy 3 ist Phase-2-Item.

## Phase-2 Pending

- Konsens-Algo Verfeinerung (gewichtete Verdict-Scores)
- Postgres-State (consensus_history Tabelle)
- Real Grok-MCP-Integration (statt mock_mode)
- Token-Prob-Variance G3.2 Proxy 3
- Replay-Mechanik (jsonl -> Postgres)
- Pre-Action-Verification-Hook (PocketOS-Lehre, K13)

## Phase-3 Pending

- Integration als Pre-Production-Hook
- KMO-Pipeline-Approval-Gate-Erweiterung (siehe DEPLOY.md)
- DF-W8-18-Lineage-Integration (Konsens-Events zur Lineage-DB)
- Cooldown-Recovery-Strategie

## Phase-4 Pending

- Shadow-Mode 14 Tage Live-Probe
- Production-Aktivierung
- launchd-Service (com.kemmer.df-w8-12.plist)
- Monitoring-Dashboard (Konsens-Rate / Provider-Outage-Rate / Tier-Distribution)

## rho-Schaetzung

**+€65k/J:** Vermiedene Production-Bugs durch Multi-LLM-Konsens-Pruefung. Pattern-Reuse DF-86 (NLM-Sync ROI) + DF-95 (Wargame-Engine) belegt fuer LLM-Pipeline-Kontexte.

## Files

```
df-w8-12-cross-llm-quality/
|- README.md                  <- Diese Datei
|- DEPLOY.md                  <- Aktivierungs-Anleitung + KMO-Pipeline-Hook-Spec
|- config.yaml                <- K11-K16 + LC1-LC5 + Build-Plan + Provider-Config
|- spec/
|  |- DF-W8-12-SPEC.md        <- Detaillierte Spec
|- src/
|  |- __init__.py
|  |- llm_provider.py         <- LLMProvider Bash-Wrapper (Codex/Gemini/Copilot/Grok)
|  |- consensus_engine.py     <- ConsensusEngine + ConsensusScore + G3.2-Proxies
|  |- quality_gate.py         <- QualityGate Hauptklasse (K16 + LC1-LC5)
|- tests/
|  |- __init__.py
|  |- conftest.py
|  |- test_llm_provider.py    <- 8 Tests
|  |- test_consensus_engine.py <- 10 Tests
|  |- test_quality_gate.py    <- 11 Tests
```

## Bekannte Phase-2-Items

- Real Grok-MCP-Integration
- Konsens-Algo Verfeinerung mit gewichteten Verdict-Scores
- Postgres-State (consensus_history)
- Token-Prob-Variance G3.2 Proxy 3
- Pre-Action-Verification-Hook (PocketOS K13)

[CRUX-MK]
