"""Focused regression probe for non-capability materialization drift."""

import json

import pytest
from test_agent_capabilities import (  # noqa: F401 -- editor is a pytest fixture used by name
    editor,
    save,
    spec_for,
)

from kiro_crew import agent_state
from kiro_crew.agent_capabilities import CapabilityError, prepare_member_capabilities


def test_empty_review_refuses_hidden_permission_shortcut_drift(
    editor,  # noqa: F811 -- pytest fixture by name
):
    service, home, specs, _ = editor
    save(service, enroll=True)
    spec = spec_for(home, specs)
    target = spec["name"]
    original_intent = agent_state.get_capabilities(target)
    path = specs / (target + ".json")
    spec["toolsSettings"] = {"shell": {"autoAllowReadonly": True}}
    path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(CapabilityError, match="alternate_permissions_require_review"):
        save(service)

    assert agent_state.get_capabilities(target) == original_intent
    assert spec_for(home, specs) == spec
    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")


def test_empty_review_restamps_noncapability_drift(editor):  # noqa: F811 -- pytest fixture by name
    service, home, specs, _ = editor
    save(service, enroll=True)
    spec = spec_for(home, specs)
    target = spec["name"]
    original_intent = agent_state.get_capabilities(target)
    path = specs / (target + ".json")
    spec["toolsSettings"] = {"read": {"setting": "custom"}}
    path.write_text(json.dumps(spec), encoding="utf-8")

    with pytest.raises(CapabilityError, match="materialization_changed"):
        prepare_member_capabilities("A")

    save(service)

    repaired_intent = agent_state.get_capabilities(target)
    assert spec_for(home, specs) == spec
    assert repaired_intent["revision"] != original_intent["revision"]
    assert repaired_intent["materialized"] != original_intent["materialized"]
    assert prepare_member_capabilities("A")["status"] == "unverified"
