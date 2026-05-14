# DF-W8-12 LLMProvider Bash-Wrapper [CRUX-MK]
"""
LLM-Provider-Abstract-Class + 4 Implementations (Codex/Gemini/Copilot/Grok).

Pattern: Bash-Wrapper-Subprocess (analog DF-86 NLM-Daily-Sync).

Phase-1:
- subprocess.run mit timeout
- auth-check via help-output-Returncode
- cooldown-detection via Pattern-Match in stdout/stderr
- Verdict-Klassifikation aus rohem LLM-Output (heuristic)
- LLMResponse mit Provenance (provider+model+timestamp)

Phase-2:
- Real Grok-MCP-Integration (heute mock_mode=true)
- Token-Prob-Distribution-Variance (G3.2 Proxy 3)
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ============================================================
# Verdict Enum
# ============================================================

class Verdict(str, Enum):
    """Verdict-Klassifikation pro LLM-Output (analog Cross-LLM-Audit-Pattern).

    ADOPT          - LLM stimmt voll zu, keine Aenderung
    MODIFY_LIGHT   - Kleine Aenderungen (<20% Output-Diff)
    MODIFY_STRONG  - Substantielle Aenderungen (>=20% Output-Diff)
    REJECT         - LLM lehnt ab, fundamentale Probleme
    UNKNOWN        - Verdict nicht klassifizierbar (Phase-1 fallback)
    """
    ADOPT = "ADOPT"
    MODIFY_LIGHT = "MODIFY_LIGHT"
    MODIFY_STRONG = "MODIFY_STRONG"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


# ============================================================
# LLMResponse Dataclass (Provenance-Pflicht)
# ============================================================

@dataclass
class LLMResponse:
    """Response von einem LLM-Call mit Provenance-Pflicht.

    Pflicht-Felder fuer K12-Distillation-Resistenz:
    - provider, model, timestamp, duration_s
    """
    provider: str
    model: str
    timestamp: str
    raw_text: str
    verdict: Verdict
    duration_s: float
    cooldown_detected: bool = False
    error: str | None = None
    family: str = ""
    available: bool = True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Enum-Wert exportieren
        d["verdict"] = self.verdict.value if isinstance(self.verdict, Enum) else self.verdict
        return d


# ============================================================
# Cooldown-Detection Helper
# ============================================================

DEFAULT_COOLDOWN_PATTERNS = [
    r"rate[-_ ]?limit",
    r"quota",
    r"resource[-_ ]?exhausted",
    r"permission[-_ ]?denied",
    r"invalid[-_ ]?grant",
    r"auth[-_ ]?(?:expired|required)",
    r"premium[-_ ]?request[-_ ]?quota",
    r"cooldown",
    r"x[-_ ]?search[-_ ]?quota",
    r"429",  # HTTP rate-limit
    r"too many requests",
]


def detect_cooldown(stdout: str, stderr: str, custom_patterns: list[str] | None = None) -> bool:
    """Detektiert Cooldown / Rate-Limit / Auth-Expired Patterns.

    Args:
        stdout: subprocess stdout-text.
        stderr: subprocess stderr-text.
        custom_patterns: Provider-spezifische Patterns (zusaetzlich).

    Returns:
        True wenn Cooldown-Indicator detektiert.
    """
    combined = f"{stdout}\n{stderr}".lower()
    patterns = list(DEFAULT_COOLDOWN_PATTERNS)
    if custom_patterns:
        patterns.extend(custom_patterns)
    for p in patterns:
        if re.search(p, combined, re.IGNORECASE):
            return True
    return False


# ============================================================
# Verdict-Klassifikation (Heuristic, Phase-1)
# ============================================================

ADOPT_PATTERNS = [
    r"\bADOPT\b",
    r"\bagree(?:d)?\b",
    r"\bno (?:changes?|modifications?) (?:needed|required)\b",
    r"\bI (?:fully )?agree\b",
    r"\bsounds (?:correct|right|good)\b",
    r"\blooks good\b",
    r"\bapprov(?:ed?|al)\b",
]
MODIFY_LIGHT_PATTERNS = [
    r"\bMODIFY[-_ ]?LIGHT\b",
    r"\bsmall (?:change|modification|tweak)\b",
    r"\bminor (?:adjustment|fix)\b",
    r"\bsuggest (?:a )?small\b",
]
MODIFY_STRONG_PATTERNS = [
    r"\bMODIFY[-_ ]?STRONG\b",
    r"\bsubstantial(?:ly)? (?:change|modify|rewrite)\b",
    r"\bmajor (?:rewrite|overhaul|change)\b",
    r"\brefactor (?:significantly|the entire|everything)\b",
    r"\bfundamental(?:ly)? (?:flawed|wrong|incorrect)\b",
]
REJECT_PATTERNS = [
    r"\bREJECT\b",
    r"\bdisagree(?:d)?\b",
    r"\b(?:cannot|can't) accept\b",
    r"\bI (?:strongly )?reject\b",
    r"\bnot (?:viable|acceptable|sound)\b",
    r"\babsolutely not\b",
]


def classify_verdict(raw_text: str) -> Verdict:
    """Heuristic verdict classification from raw LLM output.

    Phase-1: regex-basierte Pattern-Matching. Reihenfolge: REJECT > MODIFY_STRONG >
    MODIFY_LIGHT > ADOPT > UNKNOWN.

    Args:
        raw_text: rohe LLM-Output.

    Returns:
        Verdict-Enum.
    """
    if not raw_text:
        return Verdict.UNKNOWN
    text = raw_text.lower()

    # REJECT priority highest
    for p in REJECT_PATTERNS:
        if re.search(p, raw_text, re.IGNORECASE):
            return Verdict.REJECT

    # MODIFY_STRONG
    for p in MODIFY_STRONG_PATTERNS:
        if re.search(p, raw_text, re.IGNORECASE):
            return Verdict.MODIFY_STRONG

    # MODIFY_LIGHT
    for p in MODIFY_LIGHT_PATTERNS:
        if re.search(p, raw_text, re.IGNORECASE):
            return Verdict.MODIFY_LIGHT

    # ADOPT
    for p in ADOPT_PATTERNS:
        if re.search(p, raw_text, re.IGNORECASE):
            return Verdict.ADOPT

    return Verdict.UNKNOWN


# ============================================================
# LLMProvider Abstract Base
# ============================================================

class LLMProvider:
    """Abstract Base fuer Bash-Wrapper-LLM-Calls.

    Subklassen muessen mindestens implementieren:
    - build_command(prompt) -> list[str] (CLI-Args)
    - parse_model(stdout) -> str          (Model-Identifier extrahieren)

    Default-Implementations:
    - execute(prompt) -> LLMResponse (subprocess.run + Parse)
    - auth_check() -> bool             (auth_check_cmd Returncode 0)
    - is_in_cooldown() -> bool          (auth_check + Cooldown-Pattern)
    """

    name: str = "abstract"
    family: str = "unknown"
    cli_command: str = ""
    cli_args: list[str] = []
    auth_check_cmd: list[str] = []
    cooldown_patterns: list[str] = []
    timeout_s: int = 60
    default_model: str = "unknown"
    mock_mode: bool = False

    def __init__(
        self,
        cli_command: str | None = None,
        cli_args: list[str] | None = None,
        auth_check_cmd: list[str] | None = None,
        cooldown_patterns: list[str] | None = None,
        timeout_s: int | None = None,
        mock_mode: bool | None = None,
        mock_response: str | None = None,
        mock_verdict: Verdict | None = None,
    ):
        """
        Args:
            cli_command: ueberschreibt Klassen-Default.
            cli_args: ueberschreibt Klassen-Default.
            auth_check_cmd: ueberschreibt Klassen-Default.
            cooldown_patterns: ueberschreibt Klassen-Default (additiv).
            timeout_s: subprocess-Timeout in Sekunden.
            mock_mode: Wenn True, gibt mock_response statt subprocess-Call.
            mock_response: Roher Mock-Output fuer Tests.
            mock_verdict: Vorgegebenes Verdict fuer Mock.
        """
        if cli_command is not None:
            self.cli_command = cli_command
        if cli_args is not None:
            self.cli_args = list(cli_args)
        if auth_check_cmd is not None:
            self.auth_check_cmd = list(auth_check_cmd)
        if cooldown_patterns is not None:
            self.cooldown_patterns = list(cooldown_patterns)
        if timeout_s is not None:
            self.timeout_s = timeout_s
        if mock_mode is not None:
            self.mock_mode = mock_mode
        self.mock_response = mock_response
        self.mock_verdict = mock_verdict

    # ============================================================
    # build_command (Subclass-Override)
    # ============================================================

    def build_command(self, prompt: str) -> list[str]:
        """Default: cli_command + cli_args + prompt-as-arg.

        Subklassen ueberschreiben fuer stdin-mode oder andere Patterns.
        """
        return [self.cli_command, *self.cli_args, prompt]

    def build_stdin_input(self, prompt: str) -> str | None:
        """Default: kein stdin. Subklassen koennen stdin-mode aktivieren."""
        return None

    def parse_model(self, stdout: str) -> str:
        """Subklassen koennen Model-Identifier aus stdout extrahieren.

        Default: gibt default_model zurueck.
        """
        return self.default_model

    # ============================================================
    # execute (Hauptpfad)
    # ============================================================

    def execute(self, prompt: str) -> LLMResponse:
        """Fuehrt LLM-Call aus.

        Returns:
            LLMResponse mit raw_text + Verdict + Provenance.
        """
        if self.mock_mode:
            return self._execute_mock(prompt)

        timestamp = datetime.now(timezone.utc).isoformat()
        start = time.time()

        try:
            cmd = self.build_command(prompt)
            stdin_input = self.build_stdin_input(prompt)

            result = subprocess.run(
                cmd,
                input=stdin_input,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
            duration = time.time() - start

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            cooldown = detect_cooldown(stdout, stderr, self.cooldown_patterns)
            verdict = classify_verdict(stdout)
            model = self.parse_model(stdout)

            error = None
            if result.returncode != 0:
                error = f"non-zero returncode={result.returncode}: {stderr[:500]}"

            return LLMResponse(
                provider=self.name,
                model=model,
                timestamp=timestamp,
                raw_text=stdout,
                verdict=verdict,
                duration_s=duration,
                cooldown_detected=cooldown,
                error=error,
                family=self.family,
                available=(error is None and not cooldown),
            )

        except subprocess.TimeoutExpired:
            duration = time.time() - start
            return LLMResponse(
                provider=self.name,
                model=self.default_model,
                timestamp=timestamp,
                raw_text="",
                verdict=Verdict.UNKNOWN,
                duration_s=duration,
                cooldown_detected=False,
                error=f"timeout after {self.timeout_s}s",
                family=self.family,
                available=False,
            )
        except FileNotFoundError as e:
            duration = time.time() - start
            return LLMResponse(
                provider=self.name,
                model=self.default_model,
                timestamp=timestamp,
                raw_text="",
                verdict=Verdict.UNKNOWN,
                duration_s=duration,
                cooldown_detected=False,
                error=f"cli not found: {e}",
                family=self.family,
                available=False,
            )
        except Exception as e:  # pragma: no cover - defensive
            duration = time.time() - start
            return LLMResponse(
                provider=self.name,
                model=self.default_model,
                timestamp=timestamp,
                raw_text="",
                verdict=Verdict.UNKNOWN,
                duration_s=duration,
                cooldown_detected=False,
                error=f"unexpected error: {e}",
                family=self.family,
                available=False,
            )

    def _execute_mock(self, prompt: str) -> LLMResponse:
        """Mock-Ausfuehrung fuer Phase-1 (Grok) und Tests."""
        timestamp = datetime.now(timezone.utc).isoformat()
        text = self.mock_response or f"[MOCK {self.name}] ADOPT"
        verdict = self.mock_verdict if self.mock_verdict is not None else classify_verdict(text)
        return LLMResponse(
            provider=self.name,
            model=self.default_model,
            timestamp=timestamp,
            raw_text=text,
            verdict=verdict,
            duration_s=0.001,
            cooldown_detected=False,
            error=None,
            family=self.family,
            available=True,
        )

    # ============================================================
    # auth_check
    # ============================================================

    def auth_check(self) -> bool:
        """Prueft ob CLI verfuegbar + auth ok via auth_check_cmd.

        Returns:
            True wenn returncode == 0 und kein Cooldown-Pattern.
        """
        if self.mock_mode:
            return True
        if not self.auth_check_cmd:
            return False
        try:
            result = subprocess.run(
                self.auth_check_cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False
            cooldown = detect_cooldown(
                result.stdout or "", result.stderr or "", self.cooldown_patterns
            )
            return not cooldown
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
        except Exception:  # pragma: no cover - defensive
            return False

    def is_in_cooldown(self) -> bool:
        """Prueft auth_check + interpretiert Failure als Cooldown-Indikator."""
        return not self.auth_check()


# ============================================================
# Concrete Provider Implementations
# ============================================================

class CodexProvider(LLMProvider):
    """OpenAI Codex via `codex exec --skip-git-repo-check "<prompt>"`."""

    name = "codex"
    family = "openai"
    cli_command = "codex"
    cli_args = ["exec", "--skip-git-repo-check"]
    auth_check_cmd = ["codex", "--version"]
    cooldown_patterns = ["INVALID_GRANT", "rate-limit", "quota"]
    default_model = "gpt-5.4"

    def parse_model(self, stdout: str) -> str:
        m = re.search(r"model[:\s=]+([\w.-]+)", stdout)
        if m:
            return m.group(1)
        return self.default_model


class GeminiProvider(LLMProvider):
    """Google Gemini via `echo "<prompt>" | gemini -p stdin`.

    Default: nutzt -p arg-mode. Bei stdin-mode: build_stdin_input override.
    """

    name = "gemini"
    family = "google"
    cli_command = "gemini"
    cli_args = ["-p"]
    auth_check_cmd = ["gemini", "--help"]
    cooldown_patterns = ["RESOURCE_EXHAUSTED", "PERMISSION_DENIED"]
    default_model = "gemini-2.5-pro"

    def build_command(self, prompt: str) -> list[str]:
        # Gemini -p kann sowohl arg als auch stdin akzeptieren.
        # Phase-1: arg-mode (kompatibler).
        return [self.cli_command, *self.cli_args, prompt]

    def parse_model(self, stdout: str) -> str:
        m = re.search(r"model[:\s=]+([\w.-]+)", stdout)
        if m:
            return m.group(1)
        return self.default_model


class CopilotProvider(LLMProvider):
    """GitHub Copilot via `copilot -p "<prompt>" --allow-all-tools`."""

    name = "copilot"
    family = "github_openai"
    cli_command = "copilot"
    cli_args = ["-p"]
    auth_check_cmd = ["copilot", "--help"]
    cooldown_patterns = ["premium-request-quota", "rate-limit", "auth required"]
    default_model = "gpt-5"

    def build_command(self, prompt: str) -> list[str]:
        # copilot -p "<prompt>" --allow-all-tools
        return [self.cli_command, "-p", prompt, "--allow-all-tools"]

    def parse_model(self, stdout: str) -> str:
        m = re.search(r"using model[:\s]+([\w.-]+)", stdout, re.IGNORECASE)
        if m:
            return m.group(1)
        return self.default_model


class GrokProvider(LLMProvider):
    """xAI Grok via grok-mcp (Phase-1: mock-mode default)."""

    name = "grok"
    family = "xai"
    cli_command = "grok-mcp"
    cli_args = []
    auth_check_cmd = ["grok-mcp", "--version"]
    cooldown_patterns = ["x-search-quota", "rate-limit", "cooldown"]
    default_model = "grok-4.20-reasoning"
    mock_mode = True   # Phase-1 default; Phase-2 disable

    def parse_model(self, stdout: str) -> str:
        m = re.search(r"model[:\s=]+([\w.-]+)", stdout)
        if m:
            return m.group(1)
        return self.default_model


# ============================================================
# Provider-Registry (Convenience)
# ============================================================

PROVIDER_CLASSES = {
    "codex": CodexProvider,
    "gemini": GeminiProvider,
    "copilot": CopilotProvider,
    "grok": GrokProvider,
}


def make_default_providers(
    enable_mock_mode: bool = True,
) -> list[LLMProvider]:
    """Erzeugt Standard-Provider-Set fuer Phase-1.

    Args:
        enable_mock_mode: Wenn True, wird Grok in mock_mode.
                          Codex/Gemini/Copilot verwenden echte CLI.

    Returns:
        Liste aller 4 Provider.
    """
    providers: list[LLMProvider] = [
        CodexProvider(),
        GeminiProvider(),
        CopilotProvider(),
        GrokProvider(mock_mode=enable_mock_mode),
    ]
    return providers
