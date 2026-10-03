"""P0.5.1 边界收尾单测：递归不可变、反序列化去静默转换、事件/决策边界校验。

全部使用 Mock / 纯构造，不依赖真实模型 / ACP / 网络（需求11）。
"""

from __future__ import annotations

import json

import pytest

from agentboard.contracts import BoardDecision, TaskCard, WorkerResult, WorkerTaskInput
from agentboard.events import BoardEvent
from agentboard.util import freeze_payload, thaw_payload

# TaskCard.from_dict 要求 error/session_id 为字符串，构造最小合法载荷时一并提供。
_CARD_BASE = {"title": "t", "assigned_agent": "research", "goal": "g", "context": "c", "error": "", "session_id": ""}


# ---- 1. 递归不可变 ----
def test_event_nested_payload_cannot_be_mutated_externally():
    payload = {"items": [1, 2, 3], "nested": {"a": "b"}}
    evt = BoardEvent("task_started", data=payload)
    # 外部继续改原始对象不应影响已冻结的事件
    payload["items"].append(4)
    payload["nested"]["a"] = "c"
    assert evt.data["items"] == (1, 2, 3)
    assert evt.data["nested"]["a"] == "b"
    # 事件内部也不能写
    with pytest.raises(TypeError):
        evt.data["new"] = 1
    with pytest.raises((TypeError, AttributeError)):
        evt.data["items"].append(5)
    with pytest.raises(TypeError):
        evt.data["nested"]["a"] = "z"


def test_event_data_outer_read_only():
    evt = BoardEvent("task_started", data={"k": "v"})
    with pytest.raises(TypeError):
        evt.data["leak"] = "leaked"
    assert evt.data["k"] == "v"


def test_upstream_results_values_frozen():
    r = WorkerResult(
        task_id="up", agent_name="research", facts=(), evidence=(), risks=(), open_questions=(), confidence=1.0
    )
    inp = WorkerTaskInput(
        task_id="c1", title="t", assigned_agent="research", goal="g", context="c",
        upstream_results={"up": r},
    )
    with pytest.raises(TypeError):
        inp.upstream_results["other"] = r


# ---- 2. 反序列化去静默转换: depends_on ----
def test_depends_on_non_string_rejected():
    with pytest.raises(ValueError):
        TaskCard.from_dict({**_CARD_BASE, "task_id": "a1", "depends_on": [123]})


def test_depends_on_non_string_in_tuple_rejected():
    with pytest.raises(ValueError):
        TaskCard.from_dict({**_CARD_BASE, "task_id": "a1", "depends_on": ("ok", 7)})


# ---- 3. 反序列化去静默转换: acceptance / forbidden ----
def test_acceptance_non_string_rejected():
    with pytest.raises(ValueError):
        WorkerTaskInput.from_dict(
            {"task_id": "c1", "title": "t", "assigned_agent": "research", "goal": "g", "context": "c", "acceptance": [5]}
        )


def test_forbidden_non_string_rejected():
    with pytest.raises(ValueError):
        WorkerTaskInput.from_dict(
            {"task_id": "c1", "title": "t", "assigned_agent": "research", "goal": "g", "context": "c", "forbidden": [None]}
        )


# ---- 4. 可选字符串字段存在但类型错误必须拒绝（不回退空串） ----
def test_optional_string_field_wrong_type_rejected():
    with pytest.raises(ValueError):
        WorkerResult.from_dict({"task_id": "t", "agent_name": "research", "summary": 123})


def test_optional_string_field_missing_uses_default():
    r = WorkerResult.from_dict({"task_id": "t", "agent_name": "research"})
    assert r.summary == ""
    assert r.session_id == ""


# ---- 5. result 非法类型必须拒绝 ----
def test_result_non_dict_rejected():
    with pytest.raises(ValueError):
        TaskCard.from_dict({**_CARD_BASE, "task_id": "a1", "result": "not a dict"})


def test_result_null_is_ok():
    card = TaskCard.from_dict({**_CARD_BASE, "task_id": "a1", "result": None})
    assert card.result is None


# ---- 6. BoardEvent 边界校验 ----
def test_event_attempt_negative_rejected():
    with pytest.raises(ValueError):
        BoardEvent("task_started", attempt=-1)


def test_event_attempt_bool_rejected():
    with pytest.raises(ValueError):
        BoardEvent("task_started", attempt=True)


def test_event_timestamp_non_numeric_rejected():
    with pytest.raises(ValueError):
        BoardEvent("task_started", timestamp="now")


def test_event_timestamp_bool_rejected():
    with pytest.raises(ValueError):
        BoardEvent("task_started", timestamp=True)


def test_event_type_empty_rejected():
    with pytest.raises(ValueError):
        BoardEvent("")


def test_event_type_non_string_rejected():
    with pytest.raises(ValueError):
        BoardEvent(123)


def test_event_task_id_non_string_rejected():
    with pytest.raises(ValueError):
        BoardEvent("task_started", task_id=123)


def test_event_agent_non_string_rejected():
    with pytest.raises(ValueError):
        BoardEvent("task_started", agent=123)


def test_event_data_non_mapping_rejected():
    with pytest.raises(ValueError):
        BoardEvent("task_started", data=[1, 2])


# ---- 7. BoardDecision 边界校验 ----
def test_decision_illegal_action_rejected():
    with pytest.raises(ValueError):
        BoardDecision("launch_missiles")


def test_decision_task_id_wrong_type_rejected():
    with pytest.raises(ValueError):
        BoardDecision("dispatch", task_id=123)


def test_decision_reason_wrong_type_rejected():
    with pytest.raises(ValueError):
        BoardDecision("dispatch", reason=123)


def test_decision_valid_actions_accepted():
    for action in ("dispatch", "retry", "finish", "blocked"):
        assert BoardDecision(action).action == action


# ---- 8. upstream_results 键/值边界 ----
def test_upstream_results_bad_value_rejected():
    with pytest.raises(ValueError):
        WorkerTaskInput(
            task_id="c1", title="t", assigned_agent="research", goal="g", context="c",
            upstream_results={"up": "not a result"},
        )


# ---- 9. freeze/thaw 稳定且可 JSON 序列化 ----
def test_freeze_thaw_json_stable():
    payload = {"a": [1, 2], "b": {"c": "d"}, "s": {1, 2}, "n": None, "flag": True}
    j1 = json.dumps(thaw_payload(freeze_payload(payload)), sort_keys=True)
    j2 = json.dumps(thaw_payload(freeze_payload(payload)), sort_keys=True)
    assert j1 == j2
    thawed = thaw_payload(freeze_payload(payload))
    assert isinstance(thawed["a"], list)
    assert isinstance(thawed["b"], dict)
    assert isinstance(thawed["s"], list)


def test_freeze_thaw_round_trip_scalar():
    assert thaw_payload(freeze_payload("x")) == "x"
    assert thaw_payload(freeze_payload(3)) == 3
    assert thaw_payload(freeze_payload(True)) is True
