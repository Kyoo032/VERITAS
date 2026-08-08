"""Tiny, safe pass-criteria evaluator for manifest-driven probes (§11.3).

A small recursive-descent parser over a fixed token set — no ``eval``.
Supports ``and`` / ``or`` / ``not`` (with correct precedence and
parentheses) over function calls and comparisons.

Example: ``status == 200 and (json_parses and has_keys(['name', 'value']))``
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<lbracket>\[)
  | (?P<rbracket>\])
  | (?P<comma>,)
  | (?P<op>==|!=|>=|<=|>|<|=)
  | (?P<string>'[^']*'|"[^"]*")
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<ident>[a-zA-Z_]+)
    """,
    re.VERBOSE,
)


class PassEvalError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    kind: str
    value: str


def _tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise PassEvalError(f"unexpected character at offset {pos} in {text!r}")
        pos = match.end()
        kind, value = match.lastgroup, match.group()
        if kind == "ws":
            continue
        tokens.append(Token(kind, value))
    tokens.append(Token("eof", ""))
    return tokens


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def next(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, kind: str) -> Token:
        token = self.next()
        if token.kind != kind:
            raise PassEvalError(f"expected {kind}, got {token.value!r}")
        return token

    def parse(self) -> Any:
        node = self.parse_or()
        if self.peek().kind != "eof":
            raise PassEvalError(f"unexpected trailing input: {self.peek().value!r}")
        return node

    def parse_or(self) -> Any:
        node = self.parse_and()
        while self.peek().kind == "ident" and self.peek().value == "or":
            self.next()
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self) -> Any:
        node = self.parse_atom()
        while self.peek().kind == "ident" and self.peek().value == "and":
            self.next()
            node = ("and", node, self.parse_atom())
        return node

    def parse_atom(self) -> Any:
        token = self.peek()
        if token.kind == "ident" and token.value == "not":
            self.next()
            return ("not", self.parse_atom())
        if token.kind == "lparen":
            self.next()
            node = self.parse_or()
            self.expect("rparen")
            return node
        name = self.expect("ident").value
        if self.peek().kind == "lparen":
            self.next()
            args: list[Any] = []
            if self.peek().kind != "rparen":
                args.append(self.parse_literal())
                while self.peek().kind == "comma":
                    self.next()
                    args.append(self.parse_literal())
            self.expect("rparen")
            return ("call", name, args)
        if self.peek().kind == "op":
            op = self.next().value
            return ("cmp", name, op, self.parse_literal())
        if self.peek().kind == "ident" and self.peek().value == "not" and self.tokens[self.pos + 1].kind == "ident" and self.tokens[self.pos + 1].value == "in":
            self.next()
            self.next()
            return ("notin", name, self.parse_literal())
        if self.peek().kind == "ident" and self.peek().value == "in":
            self.next()
            return ("in", name, self.parse_literal())
        return ("call", name, [])

    def parse_literal(self) -> Any:
        token = self.next()
        if token.kind == "string":
            return token.value[1:-1]
        if token.kind == "number":
            return float(token.value) if "." in token.value else int(token.value)
        if token.kind == "ident":
            if token.value == "True":
                return True
            if token.value == "False":
                return False
            if token.value == "None":
                return None
            raise PassEvalError(f"unexpected identifier in literal: {token.value!r}")
        if token.kind == "lbracket":
            items: list[Any] = []
            if self.peek().kind != "rbracket":
                items.append(self.parse_literal())
                while self.peek().kind == "comma":
                    self.next()
                    items.append(self.parse_literal())
            self.expect("rbracket")
            return items
        raise PassEvalError(f"expected literal, got {token.value!r}")


def eval_pass(expr: str, env: dict[str, Any]) -> bool:
    """Evaluate a pass expression against a result environment."""

    if not expr.strip():
        return True
    node = _Parser(_tokenize(expr)).parse()
    return bool(_eval(node, env))


def _eval(node: Any, env: dict[str, Any]) -> Any:
    op = node[0]
    if op == "and":
        return _eval(node[1], env) and _eval(node[2], env)
    if op == "or":
        return _eval(node[1], env) or _eval(node[2], env)
    if op == "not":
        return not _eval(node[1], env)
    if op == "call":
        _, name, args = node
        fn = _FUNCTIONS.get(name)
        if fn is None:
            raise PassEvalError(f"unknown function: {name}")
        return fn(env, *args)
    if op == "cmp":
        _, key, comparison, expected = node
        value = env.get(key)
        if comparison == "=":
            comparison = "=="  # single '=' is accepted as equality (§11.3 DSL)
        if comparison == "==":
            return value == expected
        if comparison == "!=":
            return value != expected
        actual, expected_num = _numeric(value), _numeric(expected)
        if actual is None or expected_num is None:
            return False
        return {
            ">=": actual >= expected_num,
            "<=": actual <= expected_num,
            ">": actual > expected_num,
            "<": actual < expected_num,
        }[comparison]
    if op == "in":
        _, key, container = node
        return env.get(key) in container
    if op == "notin":
        _, key, container = node
        return env.get(key) not in container
    raise PassEvalError(f"cannot evaluate node: {node!r}")


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _F_json_parses(env: dict[str, Any]) -> bool:
    text = env.get("content") or env.get("raw_content") or ""
    try:
        json.loads(text)
        return True
    except (TypeError, ValueError):
        return False


def _F_has_keys(env: dict[str, Any], keys: list[str]) -> bool:
    parsed = env.get("parsed") or _parse_content(env)
    return bool(parsed) and all(k in parsed for k in keys)


def _parse_content(env: dict[str, Any]) -> Any:
    text = env.get("content") or ""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _F_content_contains(env: dict[str, Any], needle: str) -> bool:
    return needle.lower() in (env.get("content") or "").lower()


def _F_finish_reason(env: dict[str, Any], reason: str) -> bool:
    return env.get("finish_reason") == reason


def _F_choices(env: dict[str, Any], n: int) -> bool:
    return env.get("choices") == n


def _F_error_object(env: dict[str, Any]) -> bool:
    error = env.get("error")
    return isinstance(error, dict) and {"message", "type", "code"} <= set(error)


def _F_usage_consistent(env: dict[str, Any]) -> bool:
    usage = env.get("usage")
    if not isinstance(usage, dict):
        return False
    total = usage.get("total_tokens")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        isinstance(total, int)
        and isinstance(prompt, int)
        and isinstance(completion, int)
        and total == prompt + completion
    )


def _F_no_tool_calls(env: dict[str, Any]) -> bool:
    return env.get("tool_calls") is None


_FUNCTIONS: dict[str, Any] = {
    "json_parses": _F_json_parses,
    "has_keys": _F_has_keys,
    "content_contains": _F_content_contains,
    "finish_reason": _F_finish_reason,
    "choices": _F_choices,
    "error_object": _F_error_object,
    "usage_consistent": _F_usage_consistent,
    "no_tool_calls": _F_no_tool_calls,
}
