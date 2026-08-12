"""Unit tests for the contract evaluator Lambda handler.

Run against REAL captured AgentCore Evaluations payloads (tests/fixtures/),
so they verify the span shim and contract logic without any AWS access.

    cd sample && python -m pytest tests/ -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SAMPLE_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(SAMPLE_ROOT / "evaluator"))

import handler  # noqa: E402
from handler import ContractEntry, route_contracts  # noqa: E402
from span_shim import extract_service_names, shim_to_otlp  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"

CLAIMS_ADAPTER = handler.REGISTRY[0].adapter


def load_fixture(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


class TestSpanShim:
    def test_compliant_payload_produces_spans(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        spans = payload["evaluationInput"]["sessionSpans"]
        otlp = shim_to_otlp(spans)
        # HTTP server plumbing spans (POST /invocations) are dropped; all
        # agent-behavior spans are kept.
        assert len(otlp["spans"]) == len(spans) - 1
        names = {s.get("name") for s in otlp["spans"]}
        assert "POST /invocations" not in names
        assert "invoke_agent Strands Agents" in names

    def test_target_trace_filtering(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        spans = payload["evaluationInput"]["sessionSpans"]
        target = set(payload["evaluationTarget"]["traceIds"])
        otlp = shim_to_otlp(spans, target)
        assert otlp["spans"], "target trace should match its own spans"
        assert not shim_to_otlp(spans, {"nonexistent-trace"})["spans"]

    def test_timestamps_are_nanoseconds(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        for span in otlp["spans"]:
            assert int(span["startTimeUnixNano"]) > 1e17

    def test_output_messages_promoted(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        promoted = [
            s for s in otlp["spans"] if "gen_ai.output.messages" in s["attributes"]
        ]
        assert promoted, "span_events message content should be promoted to attributes"


class TestNormalization:
    def test_compliant_execution_has_tools_and_output(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        execution = CLAIMS_ADAPTER.normalize(otlp)

        step_names = execution.step_names()
        for tool in ("lookup_formulary", "check_coverage", "render_decision"):
            assert tool in step_names, f"{tool} missing from {step_names}"

        assert execution.output_text, "final output text must be extracted"
        assert "Section 4.2.1" in execution.output_text

        sources = execution.data_source_names()
        assert "formulary-db" in sources
        assert "coverage-policy-api" in sources


class TestDefaultInstrumentationFormat:
    """The same session captured WITHOUT experimental semconv env vars.

    Default AgentCore observability encodes span_events message content
    differently (Bedrock content blocks under content.message / content.content).
    The sample must work with stock observability -- no env vars required.
    """

    def test_default_format_compliant_trace_passes(self):
        payload = load_fixture("evaluator_payload_default_format.json")
        result = handler.handler(payload, None)
        assert result.get("label") == "PASS", result
        assert "8/8" in result["explanation"]

    def test_default_format_output_text_extracted(self):
        payload = load_fixture("evaluator_payload_default_format.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        execution = CLAIMS_ADAPTER.normalize(otlp)
        assert execution.output_text
        assert "Section 4.2.1" in execution.output_text

    def test_step_ordering_is_chronological(self):
        payload = load_fixture("evaluator_payload_default_format.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        execution = CLAIMS_ADAPTER.normalize(otlp)
        names = execution.step_names()
        assert names.index("lookup_formulary") < names.index("render_decision")
        assert names.index("check_coverage") < names.index("render_decision")


class TestHandler:
    def test_compliant_trace_passes(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        result = handler.handler(payload, None)
        assert result.get("label") == "PASS", result
        assert result.get("value") == 1.0
        assert "8/8" in result["explanation"]

    def test_lazy_trace_fails_with_named_violations(self):
        payload = load_fixture("evaluator_payload_lazy.json")
        result = handler.handler(payload, None)
        assert result.get("label") == "FAIL", result
        assert result["value"] < 1.0
        for expected in ("must_retrieve_from", "check_coverage"):
            assert expected in result["explanation"], result["explanation"]

    def test_lazy_trace_conditionals_skip_but_core_fails(self):
        """The lazy agent never called check_coverage, so the coverage-gated
        formulary constraints skip -- but the unconditional core still fails.
        This is the 'strong unconditional core' principle: conditions gate on
        facts, and skipping the fact-producing step fails the core."""
        payload = load_fixture("evaluator_payload_lazy.json")
        result = handler.handler(payload, None)
        assert result.get("label") == "FAIL"
        # Conditional formulary constraints skipped: not named as violations
        assert "must_include_steps(['lookup_formulary'])" not in result["explanation"]
        # Core violations named
        assert "must_not_use_only_parametric_knowledge" in result["explanation"]


class TestConditionalConstraints:
    """Multiple valid paths: coverage-gated constraints fire only when the
    check_coverage result says coverage is active."""

    def test_tool_outputs_promoted_to_steps(self):
        """tool_result_matches conditions need step outputs -- verify the shim
        promotes tool results from span_events into step outputs."""
        payload = load_fixture("evaluator_payload_default_format.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        execution = CLAIMS_ADAPTER.normalize(otlp)
        coverage_steps = [s for s in execution.steps if s.name == "check_coverage"]
        assert coverage_steps, execution.step_names()
        outputs = coverage_steps[0].outputs
        assert isinstance(outputs, dict), outputs
        assert outputs.get("coverage_active") is True

    def test_active_coverage_fires_formulary_constraints(self):
        """MEM-001 has active coverage: all 4 conditional constraints fire and
        are satisfied -> PASS 8/8."""
        payload = load_fixture("evaluator_payload_default_format.json")
        result = handler.handler(payload, None)
        assert result.get("label") == "PASS", result
        assert "8/8" in result["explanation"]

    def test_empty_spans_returns_error_schema(self):
        result = handler.handler(
            {"evaluationInput": {"sessionSpans": []}, "evaluationTarget": {}}, None
        )
        assert result.get("errorCode") == "NO_SPANS"

    def test_unmatched_target_returns_error_schema(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        event = {
            "evaluationInput": payload["evaluationInput"],
            "evaluationTarget": {"traceIds": ["does-not-exist"]},
        }
        result = handler.handler(event, None)
        assert result.get("errorCode") == "NO_TARGET_SPANS"


class TestLangGraphFramework:
    """The SAME contract validates a LangGraph implementation of the agent.

    Fixtures are real captured payloads from the langgraph_claims_agent
    runtime (LangGraph create_react_agent + opentelemetry-instrumentation-
    langchain on AgentCore Runtime). No contract or evaluator changes --
    the contract checks the process, not the framework.
    """

    def test_langgraph_compliant_trace_passes(self):
        payload = load_fixture("evaluator_payload_langgraph_compliant.json")
        result = handler.handler(payload, None)
        assert result.get("label") == "PASS", result
        assert result.get("value") == 1.0
        assert "8/8" in result["explanation"]

    def test_langgraph_lazy_trace_fails_with_named_violations(self):
        payload = load_fixture("evaluator_payload_langgraph_lazy.json")
        result = handler.handler(payload, None)
        assert result.get("label") == "FAIL", result
        assert result["value"] < 1.0
        for expected in ("must_not_use_only_parametric_knowledge", "check_coverage"):
            assert expected in result["explanation"], result["explanation"]

    def test_langgraph_tool_results_promoted(self):
        """tool_result_matches conditionals work cross-framework: the shim
        promotes LangChain-instrumented tool outputs into step outputs."""
        payload = load_fixture("evaluator_payload_langgraph_compliant.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        execution = CLAIMS_ADAPTER.normalize(otlp)
        coverage_steps = [s for s in execution.steps if s.name == "check_coverage"]
        assert coverage_steps, execution.step_names()
        outputs = coverage_steps[0].outputs
        assert isinstance(outputs, dict), outputs
        assert outputs.get("coverage_active") is True

    def test_langgraph_routes_to_same_claims_contract(self):
        payload = load_fixture("evaluator_payload_langgraph_compliant.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        names = extract_service_names(otlp["spans"])
        assert any("langgraph_claims_agent" in n for n in names), names
        matched = route_contracts(handler.REGISTRY, names)
        assert [e.contract.name for e in matched] == ["claims-processing-v1"]


def _entry(name: str, applies_to: list[str]) -> ContractEntry:
    """Minimal registry entry for routing tests (contract engine not exercised)."""
    from agent_validator.contracts.base import Contract
    from agent_validator.adapters.otel import OTELAdapter

    return ContractEntry(
        contract=Contract(name=name, version="1.0.0", constraints=[]),
        adapter=OTELAdapter(),
        applies_to=applies_to,
        path=f"{name}.yaml",
    )


class TestContractRouting:
    """One evaluator Lambda, many agents: contracts route by service name."""

    def test_service_names_extracted_from_fixture(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        names = extract_service_names(otlp["spans"])
        assert any("claims_agent" in n for n in names), names

    def test_fixture_routes_to_claims_contract(self):
        payload = load_fixture("evaluator_payload_compliant.json")
        otlp = shim_to_otlp(payload["evaluationInput"]["sessionSpans"])
        matched = route_contracts(handler.REGISTRY, extract_service_names(otlp["spans"]))
        assert [e.contract.name for e in matched] == ["claims-processing-v1"]

    def test_multi_contract_routing_selects_by_pattern(self):
        registry = [
            _entry("claims", ["*claims_agent*"]),
            _entry("kyc", ["*kyc_agent*"]),
        ]
        matched = route_contracts(registry, {"claimsdemo_claims_agent.DEFAULT"})
        assert [e.contract.name for e in matched] == ["claims"]
        matched = route_contracts(registry, {"bank_kyc_agent.DEFAULT"})
        assert [e.contract.name for e in matched] == ["kyc"]

    def test_multiple_matching_contracts_all_selected(self):
        registry = [
            _entry("claims", ["*claims_agent*"]),
            _entry("org-baseline", ["*"]),
        ]
        matched = route_contracts(registry, {"claimsdemo_claims_agent.DEFAULT"})
        assert {e.contract.name for e in matched} == {"claims", "org-baseline"}

    def test_single_contract_without_applies_to_is_catch_all(self):
        registry = [_entry("only", [])]
        assert route_contracts(registry, {"anything.DEFAULT"}) == registry

    def test_no_match_with_multiple_contracts_returns_empty(self):
        registry = [
            _entry("claims", ["*claims_agent*"]),
            _entry("kyc", ["*kyc_agent*"]),
        ]
        assert route_contracts(registry, {"unrelated_agent.DEFAULT"}) == []

    def test_handler_returns_unknown_agent_for_unrouted_service(self, monkeypatch):
        payload = load_fixture("evaluator_payload_compliant.json")
        monkeypatch.setattr(
            handler, "REGISTRY",
            [_entry("kyc", ["*kyc_agent*"]), _entry("fraud", ["*fraud_agent*"])],
        )
        result = handler.handler(payload, None)
        assert result.get("errorCode") == "UNKNOWN_AGENT"
        assert "claims_agent" in result["errorMessage"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
