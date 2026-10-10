"""Check a tool's arguments against its input schema before anything reaches Blender.

A small validator for the part of JSON Schema that the input schemas in tools.py use: ``type`` (one
name or a list), ``enum``, ``required``, ``properties``, ``additionalProperties``, ``items``,
``anyOf``, ``minItems``, ``maxItems`` and ``pattern``. Every other keyword is there for the model
to read, and is not checked. A violation is a refusal of kind ``bad_argument`` that names the
argument and what it accepts, so the model can correct the call; a JSON-RPC protocol error would
not tell it how. Revision 2025-11-25 of the MCP specification asks for this: it counts input
validation errors as tool execution errors, which a server reports in the tool result with
``isError`` set.
"""

from __future__ import annotations

import json
import math
import re

from blenderlens_mcp.errors import ToolFailure

_NOUNS = {"string": "a string", "number": "a number", "integer": "an integer", "boolean": "true or false",
          "object": "an object", "array": "an array", "null": "null"}
_PLURALS = {"string": "strings", "number": "numbers", "integer": "integers", "boolean": "booleans",
            "object": "objects", "array": "arrays", "null": "nulls"}


def check_arguments(tool: str, schema: dict, arguments: dict) -> None:
    """Raise a ``bad_argument`` refusal unless ``arguments`` fit ``schema``, naming the first argument that does not."""
    problem = _problem(tool, arguments, schema, "")
    if problem is not None:
        path, message, hint = problem
        raise ToolFailure("bad_argument", message, hint, argument=path)


def describe(schema) -> str:
    """Return what ``schema`` accepts, in words: "an array of 3 numbers or nulls", "one of "cube", "plane""."""
    if not isinstance(schema, dict):
        return "any value"
    if "anyOf" in schema:
        return " or ".join(describe(option) for option in schema["anyOf"])
    if "enum" in schema:
        return "one of " + ", ".join(json.dumps(value) for value in schema["enum"])
    names = _types(schema)
    if names == ["array"]:
        return "an array of " + _count(schema) + _plural(schema.get("items"))
    if names == ["string"] and "pattern" in schema:
        return f"a string matching {schema['pattern']}"
    return " or ".join(_NOUNS.get(name, name) for name in names) or "any value"


def _types(schema: dict) -> list[str]:
    """Return the type names a schema allows, as a list, which is empty when it names no type."""
    kind = schema.get("type")
    if kind is None:
        return []
    return [kind] if isinstance(kind, str) else list(kind)


def _count(schema: dict) -> str:
    """Return the number of items an array schema allows, in words followed by a space, or an empty string."""
    low, high = schema.get("minItems"), schema.get("maxItems")
    if low is not None and low == high:
        return f"{low} "
    if low is not None and high is not None:
        return f"{low} to {high} "
    if low is not None:
        return f"at least {low} "
    if high is not None:
        return f"at most {high} "
    return ""


def _plural(items) -> str:
    """Return what an array's items are, in the plural, as ``describe`` words it."""
    if not isinstance(items, dict):
        return "values"
    if "anyOf" in items or "enum" in items:
        return f"values, each {describe(items)}"
    names = _types(items)
    if names == ["array"]:
        return "arrays of " + _count(items) + _plural(items.get("items"))
    return " or ".join(_PLURALS.get(name, name) for name in names) or "values"


def _is(value, kind: str) -> bool:
    """Return whether ``value`` is of the JSON Schema type ``kind``."""
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        # Only true and false: the string "false" is not a boolean, and Python's bool("false") is True.
        return isinstance(value, bool)
    if kind == "null":
        return value is None
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if kind == "number":
        # NaN and Infinity are not JSON numbers, though Python's JSON reader accepts them.
        return math.isfinite(value)
    if kind == "integer":
        # JSON Schema counts 4.0 as an integer: a number with no fraction.
        return isinstance(value, int) or (math.isfinite(value) and value.is_integer())
    return True  # A type name this validator does not know refuses nothing.


def _same(value, allowed) -> bool:
    """Return whether ``value`` equals the enum value ``allowed`` and has the same type.

    The type must match, so that true never matches 1, and 1 never matches 1.0.
    """
    if isinstance(value, bool) or isinstance(allowed, bool):
        return value is allowed
    return type(value) is type(allowed) and value == allowed


def _show(value) -> str:
    """Return a value as a refusal message shows it: its JSON, cut to 60 characters, and its type."""
    kind = "a boolean" if isinstance(value, bool) else {
        str: "a string", int: "an integer", float: "a number", list: "an array", dict: "an object",
        type(None): "null"}.get(type(value), type(value).__name__)
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return text if value is None else f"{text} ({kind})"


def _takes(path: str, schema: dict) -> str:
    """Return the hint for a value that does not fit: what the argument takes, and that nothing was sent to Blender."""
    return f"'{path}' takes {describe(schema)}. Nothing was sent to Blender."


def _join(path: str, key: str) -> str:
    """Return the path of ``key`` inside the argument at ``path``, such as ``settings.width``."""
    return f"{path}.{key}" if path else key


def _problem(tool: str, value, schema, path: str) -> tuple[str, str, str] | None:
    """Return (the argument's path, the message, the hint) for the first place ``value`` breaks ``schema``, or None."""
    if not isinstance(schema, dict):
        return None
    wrong = (path, f"'{path}' does not take {_show(value)}.", _takes(path, schema))
    if "anyOf" in schema:
        if not any(_problem(tool, value, option, path) is None for option in schema["anyOf"]):
            return wrong
    names = _types(schema)
    if names and not any(_is(value, name) for name in names):
        return wrong
    if "enum" in schema and not any(_same(value, allowed) for allowed in schema["enum"]):
        return wrong
    if isinstance(value, str) and isinstance(schema.get("pattern"), str) and not re.search(schema["pattern"], value):
        return wrong
    if isinstance(value, list):
        return _array_problem(tool, value, schema, path)
    if isinstance(value, dict):
        return _object_problem(tool, value, schema, path)
    return None


def _array_problem(tool: str, value: list, schema: dict, path: str) -> tuple[str, str, str] | None:
    """Return the first problem with an array: its length, then each item in turn; or None."""
    low, high = schema.get("minItems"), schema.get("maxItems")
    if (low is not None and len(value) < low) or (high is not None and len(value) > high):
        return path, f"'{path}' holds {len(value)} items.", _takes(path, schema)
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            problem = _problem(tool, item, items, f"{path}[{index}]")
            if problem is not None:
                return problem
    return None


def _object_problem(tool: str, value: dict, schema: dict, path: str) -> tuple[str, str, str] | None:
    """Return the first problem with an object: an unknown key, then a missing required key, then a bad value."""
    properties = schema.get("properties") or {}
    extra = schema.get("additionalProperties", True)
    owner = f"{tool} has no argument" if not path else f"'{path}' has no key"
    # A misspelt key comes first: it is usually why a required one seems missing.
    for key in value:
        if key not in properties and extra is False:
            accepted = ", ".join(properties) or "nothing"
            return _join(path, key), f"{owner} '{key}'.", f"It accepts: {accepted}."
    for key in schema.get("required") or []:
        if key not in value:
            needs = f"{tool} needs '{key}'." if not path else f"'{path}' needs '{key}'."
            return _join(path, key), needs, f"'{key}' takes {describe(properties.get(key))}."
    for key, item in value.items():
        rule = properties.get(key, extra if isinstance(extra, dict) else None)
        problem = _problem(tool, item, rule, _join(path, key))
        if problem is not None:
            return problem
    return None
