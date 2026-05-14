# DF-W8-12 LLMProvider Tests [CRUX-MK]
"""
Tests fuer Bash-Wrapper, Auth-Check, Cooldown-Detection, Timeout, Verdict-Klassifikation.

Mocks: subprocess.run via unittest.mock (KEINE echten LLM-Calls).
"""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

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
        detect_cooldown,
        classify_verdict,
        DEFAULT_COOLDOWN_PATTERNS,
        make_default_providers,
    )
    return {
        "LLMProvider": LLMProvider,
        "LLMResponse": LLMResponse,
        "Verdict": Verdict,
        "CodexProvider": CodexProvider,
        "GeminiProvider": GeminiProvider,
        "CopilotProvider": CopilotProvider,
        "GrokProvider": GrokProvider,
        "detect_cooldown": detect_cooldown,
        "classify_verdict": classify_verdict,
        "make_default_providers": make_default_providers,
    }


def _mock_subprocess_result(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> MagicMock:
    """Erzeugt einen Mock-CompletedProcess."""
    res = MagicMock(spec=subprocess.CompletedProcess)
    res.stdout = stdout
    res.stderr = stderr
    res.returncode = returncode
    return res


# ============================================================
# Test 1 - Provider-Init mit cli_command-Path Pflicht
# ============================================================

def test_provider_init_with_path():
    M = _import_modules()
    codex = M["CodexProvider"]()
    assert codex.name == "codex"
    assert codex.family == "openai"
    assert codex.cli_command == "codex"
    assert "exec" in codex.cli_args
    assert codex.timeout_s == 60

    gemini = M["GeminiProvider"]()
    assert gemini.name == "gemini"
    assert gemini.family == "google"
    assert gemini.cli_command == "gemini"

    copilot = M["CopilotProvider"]()
    assert copilot.name == "copilot"
    assert copilot.family == "github_openai"

    grok = M["GrokProvider"]()
    assert grok.name == "grok"
    assert grok.family == "xai"
    # Phase-1: Grok ist mock_mode by default
    assert grok.mock_mode is True

    # Override-Pfad
    custom = M["CodexProvider"](cli_command="/usr/local/bin/codex", timeout_s=15)
    assert custom.cli_command == "/usr/local/bin/codex"
    assert custom.timeout_s == 15


# ============================================================
# Test 2 - Auth-Check via Help-Output (Returncode-0)
# ============================================================

def test_auth_check_via_help_output():
    M = _import_modules()
    codex = M["CodexProvider"]()

    # Returncode 0 + kein Cooldown -> Auth ok
    with patch("src.llm_provider.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess_result(
            stdout="codex 0.125.0", returncode=0
        )
        assert codex.auth_check() is True

    # Returncode 1 -> Auth fail
    with patch("src.llm_provider.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess_result(
            stdout="", stderr="auth required", returncode=1
        )
        assert codex.auth_check() is False

    # Returncode 0 aber Cooldown-Pattern in Output -> Auth fail
    with patch("src.llm_provider.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess_result(
            stdout="rate-limit hit, please wait", returncode=0
        )
        assert codex.auth_check() is False

    # Mock-Mode -> immer True (auch ohne CLI-Call)
    grok_mock = M["GrokProvider"](mock_mode=True)
    assert grok_mock.auth_check() is True


# ============================================================
# Test 3 - Cooldown-Detection via Pattern-Match
# ============================================================

def test_cooldown_detection_rate_limit():
    M = _import_modules()
    detect = M["detect_cooldown"]

    # Default-Patterns: rate-limit
    assert detect("rate-limit reached", "") is True
    assert detect("", "rate limit hit") is True
    assert detect("Quota exceeded", "") is True
    assert detect("RESOURCE_EXHAUSTED", "") is True
    assert detect("INVALID_GRANT", "") is True
    assert detect("HTTP 429: too many requests", "") is True
    assert detect("auth_expired", "") is True
    assert detect("premium-request-quota", "") is True

    # Negativ-Faelle: kein Pattern
    assert detect("OK 200", "") is False
    assert detect("response generated successfully", "") is False
    assert detect("", "") is False

    # Custom-Patterns
    assert detect(
        "x-search-quota exhausted", "", custom_patterns=["x-search-quota"]
    ) is True

    # Case-Insensitive
    assert detect("RATE-LIMIT REACHED", "") is True
    assert detect("Rate Limit Hit", "") is True


# ============================================================
# Test 4 - Timeout-Enforcement
# ============================================================

def test_timeout_enforcement():
    M = _import_modules()
    codex = M["CodexProvider"](timeout_s=5)

    with patch("src.llm_provider.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["codex", "exec"], timeout=5
        )
        response = codex.execute("test prompt")

        assert response.error is not None
        assert "timeout" in response.error.lower()
        assert response.available is False
        assert response.verdict == M["Verdict"].UNKNOWN
        assert response.duration_s >= 0.0
        assert response.provider == "codex"

    # FileNotFoundError (CLI fehlt)
    with patch("src.llm_provider.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("codex not found")
        response = codex.execute("test prompt")
        assert response.error is not None
        assert "not found" in response.error.lower()
        assert response.available is False


# ============================================================
# Test 5 - Bash-Wrapper baut korrekte subprocess-Args
# ============================================================

def test_bash_wrapper_args_correct():
    M = _import_modules()

    # Codex: codex exec --skip-git-repo-check "<prompt>"
    codex = M["CodexProvider"]()
    cmd = codex.build_command("Hallo Welt")
    assert cmd[0] == "codex"
    assert "exec" in cmd
    assert "--skip-git-repo-check" in cmd
    assert "Hallo Welt" in cmd

    # Gemini: gemini -p "<prompt>"
    gemini = M["GeminiProvider"]()
    cmd = gemini.build_command("Hallo")
    assert cmd[0] == "gemini"
    assert "-p" in cmd
    assert "Hallo" in cmd

    # Copilot: copilot -p "<prompt>" --allow-all-tools
    copilot = M["CopilotProvider"]()
    cmd = copilot.build_command("Test")
    assert cmd[0] == "copilot"
    assert "-p" in cmd
    assert "Test" in cmd
    assert "--allow-all-tools" in cmd

    # Verifiziere subprocess.run wird mit erwarteten args aufgerufen
    with patch("src.llm_provider.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess_result(
            stdout="ADOPT - good idea", returncode=0
        )
        response = codex.execute("Frage X?")
        assert mock_run.called
        call_args = mock_run.call_args
        # erstes positional-arg ist die cmd-list
        called_cmd = call_args.args[0] if call_args.args else call_args.kwargs.get("args")
        # subprocess.run nimmt cmd als 1. arg
        assert called_cmd[0] == "codex"
        assert "Frage X?" in called_cmd

        # response korrekt parsed
        assert response.provider == "codex"
        assert response.raw_text == "ADOPT - good idea"
        assert response.verdict == M["Verdict"].ADOPT


# ============================================================
# Test 6 - Response-Parsing (rohes stdout -> LLMResponse)
# ============================================================

def test_response_parsing_basic():
    M = _import_modules()
    Verdict = M["Verdict"]
    classify = M["classify_verdict"]

    # Verdict-Klassifikation
    assert classify("ADOPT - this is great") == Verdict.ADOPT
    assert classify("I fully agree with this") == Verdict.ADOPT
    assert classify("looks good to me") == Verdict.ADOPT

    assert classify("MODIFY-LIGHT - small tweak needed") == Verdict.MODIFY_LIGHT
    assert classify("suggest a small change") == Verdict.MODIFY_LIGHT
    assert classify("minor adjustment please") == Verdict.MODIFY_LIGHT

    assert classify("MODIFY-STRONG - major rewrite needed") == Verdict.MODIFY_STRONG
    assert classify("substantially modify the entire approach") == Verdict.MODIFY_STRONG
    assert classify("fundamentally flawed reasoning") == Verdict.MODIFY_STRONG

    assert classify("REJECT - cannot accept") == Verdict.REJECT
    assert classify("I strongly reject this") == Verdict.REJECT
    assert classify("absolutely not viable") == Verdict.REJECT

    # Empty / unknown
    assert classify("") == Verdict.UNKNOWN
    assert classify("just some random text") == Verdict.UNKNOWN

    # Reihenfolge: REJECT > MODIFY_STRONG > MODIFY_LIGHT > ADOPT
    # "ADOPT but reject if X" -> REJECT (REJECT priority)
    assert classify("ADOPT but I reject the alternative") == Verdict.REJECT

    # Komplette LLMResponse via execute (mit Mock)
    codex = M["CodexProvider"]()
    with patch("src.llm_provider.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess_result(
            stdout="ADOPT - excellent analysis. model: gpt-5.4",
            returncode=0,
        )
        response = codex.execute("Question?")
        assert isinstance(response, M["LLMResponse"])
        assert response.provider == "codex"
        assert response.family == "openai"
        assert response.verdict == Verdict.ADOPT
        assert response.timestamp  # ISO-8601 not empty
        assert response.duration_s >= 0.0
        assert response.cooldown_detected is False
        assert response.error is None
        assert response.available is True
        # parse_model extrahiert "gpt-5.4"
        assert response.model == "gpt-5.4"


# ============================================================
# Test 7 - Mock-Mode liefert pre-defined Response (Tests)
# ============================================================

def test_mock_mode_returns_predefined():
    M = _import_modules()
    Verdict = M["Verdict"]

    # Default-Mock
    grok = M["GrokProvider"]()
    response = grok.execute("any prompt")
    assert response.provider == "grok"
    assert response.family == "xai"
    assert response.error is None
    assert response.available is True
    assert "MOCK" in response.raw_text or response.verdict == Verdict.ADOPT

    # Custom Mock-Response
    grok2 = M["GrokProvider"](
        mock_mode=True,
        mock_response="REJECT - completely wrong",
        mock_verdict=Verdict.REJECT,
    )
    r2 = grok2.execute("test")
    assert r2.verdict == Verdict.REJECT
    assert r2.raw_text == "REJECT - completely wrong"


# ============================================================
# Test 8 - make_default_providers liefert 4 Provider
# ============================================================

def test_make_default_providers_returns_four():
    M = _import_modules()
    providers = M["make_default_providers"]()
    assert len(providers) == 4
    names = {p.name for p in providers}
    assert names == {"codex", "gemini", "copilot", "grok"}
    families = {p.family for p in providers}
    assert "openai" in families
    assert "google" in families
    assert "github_openai" in families
    assert "xai" in families
