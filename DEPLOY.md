# DF-W8-12 Deployment-Anleitung [CRUX-MK]

Phase-1 Aktivierungs-Anleitung. Phase-2/3/4 in `config.yaml` build_plan.

## Phase-1 Aktivierungs-Schritte

### 1. Pre-Aktivierungs-Check

```bash
cd ~/Projects/dark-factories/df-w8-12-cross-llm-quality

# Tests muessen passing
pytest tests/ -v
```

Erwartetes Output: **29 passed**.

Falls Tests fail: STOP, Phase-1 nicht aktivieren.

### 2. CLI-Setup pruefen (optional fuer real-mode)

```bash
# Codex
codex --version

# Gemini (Google AI Studio)
gemini --help

# Copilot (GitHub CLI)
copilot --help

# Grok (Phase-1: mock-mode default; Phase-2: grok-mcp install)
# grok-mcp --version  # Phase-2
```

Falls eine CLI fehlt: Provider wird als unavailable markiert (auth_check returns False), Mode degradiert automatisch.

### 3. STOP.flag-Mechanik testen

```bash
# Pausieren
touch /tmp/df-w8-12.stop

# In Python: gate.check sollte tier_recommendation=REJECT zurueckgeben
python3 -c "
from src.llm_provider import make_default_providers
from src.quality_gate import QualityGate

providers = make_default_providers(enable_mock_mode=True)
g = QualityGate(providers=providers, skip_mutex_for_tests=True, engine_pgrep_check=False)
score = g.check('test')
print(f'tier={score.tier_recommendation.value}, mode={g.current_mode().value}')
"

# Re-aktivieren
rm /tmp/df-w8-12.stop
```

Erwartet: `tier=REJECT, mode=stopped`.

### 4. K16 Mutex-Test

```bash
# Engine soll als Prozess "df-w8-12-engine" laufen damit pgrep es findet.
# Phase-1: Manuell, Phase-4: launchd-Service

python3 -m src.quality_gate  # Phase-2 wird Engine-Loop bauen
```

### 5. Health-Check

```bash
python3 -c "
from src.llm_provider import make_default_providers
from src.quality_gate import QualityGate
import json

providers = make_default_providers(enable_mock_mode=True)
g = QualityGate(providers=providers, skip_mutex_for_tests=True, engine_pgrep_check=False)
print(json.dumps(g.health_check(), indent=2, default=str))
"
```

Erwartetes Output (mock-mode):

```json
{
  "score": 1.0,
  "jsonl_ok": true,
  "stopped": false,
  "n_providers_available": 4,
  "n_providers_total": 4,
  "providers": {
    "codex": {"auth_ok": ..., "available": ...},
    "gemini": {"auth_ok": ..., "available": ...},
    "copilot": {"auth_ok": ..., "available": ...},
    "grok": {"auth_ok": true, "available": true}
  },
  "mode": "full",
  "own_function_ok": true
}
```

## Operative Pfade

| Pfad | Zweck |
|------|-------|
| `/tmp/df-w8-12.lock` | K16 Mutex-Dir |
| `/tmp/df-w8-12.stop` | STOP.flag (single-command-override) |
| `/tmp/df-w8-12-health.json` | LC5 Health-Output |
| `~/.df-w8-12/quality-gate-audit.jsonl` | Default jsonl-Audit-Pfad |
| `branch-hub/audit/df-w8-12-quality-gate.jsonl` | Production-jsonl-Pfad (Phase-4) |

## Rollback-Mechanik

### Single-Command-Override

```bash
touch /tmp/df-w8-12.stop
```

Gate pausiert sofort. `gate.check()` gibt `tier_recommendation=REJECT` zurueck. KMO-Pipeline-Approval-Gate (Phase-3) blockiert dann automatisch alle LLM-Outputs.

### Komplett-Deaktivierung (Phase-2 ueber launchd)

```bash
# launchctl unload ~/Library/LaunchAgents/com.kemmer.df-w8-12.plist
# (Phase-4 build)
```

## Pre-Action-Verification (K13 PocketOS-Lehre)

**Pflicht** vor jeder Production-Pipeline-Erweiterung in Phase-2+:

```python
# Pseudocode (Phase-2 implementation)
def pre_action_verify(action: str, prompt_hash: str) -> bool:
    # 1. env_tag check
    env = os.getenv("DF_W8_12_ENV")
    assert env in ("dev", "staging", "prod"), f"Invalid env={env}"

    # 2. quota check (K11.b Pipeline-Cost-Estimate)
    quota_used = get_today_quota_consumption()
    quota_ceiling = config["k11b_pipeline_cost_estimate"]["quota_budget_ceiling"]
    if quota_used + 4 > quota_ceiling:  # 4 LLMs pro Run
        return False  # quota-overrun

    # 3. circuit-breaker check
    n_open = sum(1 for cb in gate.circuit_breakers.values()
                  if cb.state == CircuitBreakerState.OPEN)
    if n_open >= 3:
        return False  # zu viele Provider down

    # 4. backup check
    audit_path_writable = gate._jsonl_writable()
    assert audit_path_writable, "audit-log nicht schreibbar"

    return True
```

## KMO-Pipeline-Approval-Gate-Hook-Spec (Phase-3)

```python
# In KMO-Pipeline (Phase-3 Integration):
from df_w8_12.quality_gate import QualityGate
from df_w8_12.consensus_engine import TierRecommendation
from df_w8_12.llm_provider import make_default_providers

# Lazy-Init der Gate-Instanz (Singleton)
_gate_singleton = None

def get_quality_gate() -> QualityGate:
    global _gate_singleton
    if _gate_singleton is None:
        providers = make_default_providers(enable_mock_mode=False)  # Phase-3 real-mode
        _gate_singleton = QualityGate(
            providers=providers,
            jsonl_audit_path="branch-hub/audit/df-w8-12-quality-gate.jsonl",
        )
    return _gate_singleton


@pre_production_hook
def consensus_gate_check(prompt: str, context: dict) -> bool:
    """Wird vor jedem Production-LLM-Output aufgerufen.

    Returns:
        True wenn Output approved (HARDENED / TWOOFTHREE_HARDENED / SIM_HARDENED).
        False wenn Output BLOCK (CONDITIONAL / REJECT / NO_PROVIDERS).
    """
    gate = get_quality_gate()
    score = gate.check(prompt, context)

    APPROVED_TIERS = {
        TierRecommendation.HARDENED,
        TierRecommendation.TWOOFTHREE_HARDENED,
    }
    WARN_TIERS = {
        TierRecommendation.SIM_HARDENED,
    }
    BLOCK_TIERS = {
        TierRecommendation.CONDITIONAL,
        TierRecommendation.REJECT,
    }

    if score.tier_recommendation in APPROVED_TIERS:
        log.info(f"Gate-APPROVED: tier={score.tier_recommendation.value}")
        return True

    if score.tier_recommendation in WARN_TIERS:
        # Log + allow (Production-Decision: Phase-3 koennte enger sein)
        log.warning(
            f"Gate-WARN tier={score.tier_recommendation.value} "
            f"convergence={score.convergence_class.value} "
            f"score={score.overall_score:.2f}"
        )
        return True

    if score.tier_recommendation in BLOCK_TIERS:
        log.error(
            f"Gate-BLOCK tier={score.tier_recommendation.value} "
            f"convergence={score.convergence_class.value} "
            f"prompt_hash={QualityGate._hash_prompt(prompt)[:16]}"
        )
        return False

    # Unknown tier -> defensive BLOCK
    log.error(f"Gate-UNKNOWN tier={score.tier_recommendation}")
    return False
```

### Integration in HeyLou-OTA / 9OS-ABS

```python
# Beispiel HeyLou-OTA AI-Personalisierung
def get_personalized_recommendations(guest_id: str, context: dict) -> list:
    prompt = build_personalization_prompt(guest_id, context)

    # Pre-Production-Gate
    if not consensus_gate_check(prompt, {"caller": "heylou-ota", "guest": guest_id}):
        # BLOCK: Fallback auf statische Recommendations
        return fallback_static_recommendations(context)

    # Approved: LLM-Call ausfuehren
    return llm_personalize(prompt, guest_id)


# Beispiel 9OS-ABS-Pricing-AI
def get_pricing_suggestion(hotel_id: str, demand_data: dict) -> dict:
    prompt = build_pricing_prompt(hotel_id, demand_data)

    if not consensus_gate_check(prompt, {"caller": "9os-abs", "hotel": hotel_id}):
        # BLOCK: konservative Default-Pricing-Logik
        return rule_based_fallback_pricing(demand_data)

    return llm_pricing_suggestion(prompt, hotel_id)


# Beispiel 9OS-Voice-GSA
def get_voice_response(transcript: str, context: dict) -> str:
    prompt = build_voice_prompt(transcript, context)

    if not consensus_gate_check(prompt, {"caller": "9os-voice", "session_id": context.get("session_id")}):
        # BLOCK: Eskalation an Mensch-Mitarbeiter
        return escalate_to_human_agent(transcript, context)

    return llm_voice_response(prompt, context)
```

## Monitoring (Phase-4)

- **Konsens-Rate** (% Calls mit HARDENED + TWOOFTHREE_HARDENED Tier)
- **Provider-Outage-Frequency** (Anzahl OPEN Circuit-Breaker / Tag)
- **Tier-Distribution** (Histogramm HARDENED / SIM_HARDENED / CONDITIONAL / REJECT)
- **Cooldown-Detection-Rate** (% Calls mit Cooldown-Pattern)
- **Mode-Distribution** (% in full / degraded_grok / degraded_2llm / standalone)
- **G3.2-Proxy-Coverage** (Anteil Calls mit 2+ Proxies)

## Bekannte Phase-2-Items

- Konsens-Algo Verfeinerung (gewichtete Verdict-Scores)
- Postgres-State (consensus_history)
- Real Grok-MCP-Integration (statt mock_mode default)
- Token-Prob-Variance G3.2 Proxy 3
- Replay-Mechanik (jsonl -> Postgres)
- Pre-Action-Verification-Hook (PocketOS K13)

## Bekannte Phase-3-Items

- Integration als Pre-Production-Hook (consensus_gate_check)
- KMO-Pipeline-Approval-Gate-Erweiterung
- DF-W8-18-Lineage-Integration (Konsens-Events zur Lineage-DB)
- Cooldown-Recovery-Strategie (priorisierte Provider-Wiederherstellung)

## Bekannte Phase-4-Items

- Shadow-Mode 14 Tage Live-Probe
- Production-Aktivierung
- launchd-Service (com.kemmer.df-w8-12.plist)
- Monitoring-Dashboard
- Cross-LLM-2OF3-HARDENED-Pflicht-Wargame vor Live (per rules/wargame-first-pflicht.md)

[CRUX-MK]
