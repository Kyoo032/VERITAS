"""Pass-criteria DSL (§11.3)."""

from __future__ import annotations

import pytest

from supgate.passdsl import PassEvalError, eval_pass

ENV = {
    "status": 200,
    "content": '{"name": "x", "value": 1}',
    "finish_reason": "stop",
    "choices": 2,
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    "error": None,
    "tool_calls": None,
}


def test_basic_comparisons():
    assert eval_pass("status == 200", ENV)
    assert not eval_pass("status == 201", ENV)
    assert eval_pass("status != 201", ENV)
    assert eval_pass("status >= 200", ENV)
    assert eval_pass("status in [200, 400]", ENV)


def test_functions():
    assert eval_pass("json_parses", ENV)
    assert eval_pass("has_keys(['name', 'value'])", ENV)
    assert not eval_pass("has_keys(['missing'])", ENV)
    assert eval_pass("finish_reason('stop')", ENV)
    assert eval_pass("choices == 2", ENV)
    assert eval_pass("usage_consistent", ENV)
    assert eval_pass("no_tool_calls", ENV)
    assert eval_pass("error_object", {**ENV, "error": {"message": "m", "type": "t", "code": "c"}})
    assert not eval_pass("error_object", ENV)


def test_and_or_not():
    assert eval_pass("status == 200 and json_parses and has_keys(['name'])", ENV)
    assert not eval_pass("status == 200 and json_parses and has_keys(['zzz'])", ENV)
    assert eval_pass("status == 400 or status == 200", ENV)
    assert eval_pass("not status == 201", ENV)
    assert eval_pass("status == 400 or (status == 200 and has_keys(['name']))", ENV)


def test_content_contains_is_case_insensitive():
    assert eval_pass("content_contains('NAME')", ENV)


def test_empty_expr_passes():
    assert eval_pass("", ENV)
    assert eval_pass("   ", ENV)


def test_unknown_function_raises():
    with pytest.raises(PassEvalError):
        eval_pass("mystery_fn(1)", ENV)


def test_garbage_atom_raises():
    with pytest.raises(PassEvalError):
        eval_pass("this is not valid", ENV)
