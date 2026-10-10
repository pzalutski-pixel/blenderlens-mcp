"""Check an asset's description against a project's own rules, given as a JSON object.

This module is pure Python and does not import bpy, so the server can load it outside Blender.
``check_asset`` in the add-on describes the open .blend file, and the server's ``check`` command describes a
.glb file, in the same neutral form, and both pass that description here. One set of rules therefore reads
the same for a .blend file as for the .glb file exported from it.

The description, in Blender's axes (Z up, metres)::

    {"objects": [{"name", "parent", "type", "triangles", "extras", "bounds": [[x, y, z], [x, y, z]] or None,
                  "origin": [x, y, z], "materials": [name, ...]}],
     "materials": [{"name", "base_color": [r, g, b] or None, "metallic", "roughness",
                    "emission": [r, g, b] (colour times strength) or None}]}

``bounds`` and ``origin`` are in world space. ``triangles`` and ``bounds`` cover the object alone, and the
rules that measure a subtree (size, budget and origin_at_base) add its descendants. A value of None is one
the describer could not read, such as a colour that a texture or shader nodes set, and no palette entry
matches it. Where names may repeat, as among a .glb file's nodes or the objects that collection instances
show, an object also carries an ``id`` and its parent's ``parent_id``, and the tree is built from those.

The rule set, in which every key is optional::

    required: [selector or "Parent/Child"]
        Each entry must match at least one object.
    single_root: true
        Exactly one object has no parent.
    retired: [selector]
        No object or material may have a name the selector matches.
    palette: {material: {base_color, metallic, roughness, emission, strength}}
        Each material named must have these values.
    palette_tolerance: number (default 0.001)
        How far each linear channel may differ from the palette. Emission may also differ by 2 %.
    palette_only: true
        Every material must be in the palette.
    extras: [{objects, keys: {key: type}, only}]
        The objects must carry these extras, of these types: number, integer, string, boolean, array,
        object or any. With only true, no other object may carry them.
    size: [{objects, min: [x, y, z], max: [x, y, z]}]
        Each object's size, with its descendants, in metres. null means no limit on that axis.
    origin_at_base: true, or a selector
        The origin sits on the bottom of the bounds, inside the footprint: of every root object with true,
        or of each object the selector matches.
    budget: number, or [{objects, triangles}]
        The most triangles: of the whole asset for a number, or of each matching object's subtree.
    apart: [{a, b, allow: [[name, name]]}]
        No object that a matches may cross one that b matches, or lie inside it, except the pairs allowed.

A selector is a name, "re:<regular expression>" searched for in the name (anchor it with ^ and $), or a
list of them. A plain name also matches that name with Blender's .001-style suffix. A pattern is searched
for in at most the first MAX_NAME_SEARCHED characters of a name. A pattern that could make Python's regular
expression engine take extremely long to give up on a name is refused. Such a pattern has a repeated group
that holds a repeat or a choice, as in (a+)+ or (a|aa)*, or more than MAX_REPEATS open-ended repeats.
"""

from __future__ import annotations

import json
import math
import re

#: The most names an issue lists. The issue's ``count`` says how many there are in all.
LISTED = 32
#: How far an origin may sit from the bottom of its bounds, and outside its footprint, in metres.
ORIGIN_TOLERANCE = 0.001
#: The most object pairs an apart rule tests, so a rule matching hundreds of objects stays quick.
MAX_PAIRS = 20_000
#: The most characters of a name that a pattern searches. Blender 4.2 keeps names of at most 63 bytes, and
#: Blender 5.0 names of datablocks such as objects and materials of up to 255. Searching only the first 128
#: characters keeps the worst-case search bounded for a pattern of up to MAX_REPEATS open-ended repeats. On a
#: longer name, an anchor at the end ($) therefore matches at character 128, as though the name ended there.
MAX_NAME_SEARCHED = 128
#: The most open-ended repeats (*, + and {n,}) a pattern may hold. Placed side by side, their cost grows with
#: the length searched, raised to the power of their number plus one.
MAX_REPEATS = 3

#: The types an extras rule may ask a key's value to have.
TYPES = ("number", "integer", "string", "boolean", "array", "object", "any")
#: The values a palette entry may give.
PALETTE_KEYS = ("base_color", "metallic", "roughness", "emission", "strength")
#: The keys a rule set may have.
KEYS = ("required", "single_root", "retired", "palette", "palette_tolerance", "palette_only", "extras", "size",
        "origin_at_base", "budget", "apart")


class RulesError(ValueError):
    """An error raised when a rule set does not fit the format. The message names the key and what it takes."""


# ---------------------------------------------------------------------------
# Reading the rules
# ---------------------------------------------------------------------------


def _skip_class(pattern: str, i: int) -> int:
    """Return the index just past the character class that opens at ``pattern[i]``."""
    i += 1
    if i < len(pattern) and pattern[i] == "^":
        i += 1
    if i < len(pattern) and pattern[i] == "]":
        i += 1
    while i < len(pattern) and pattern[i] != "]":
        i += 2 if pattern[i] == "\\" else 1
    return i + 1


def _quantifier(pattern: str, i: int):
    """Return ``(end, open_ended, many)`` for a quantifier at ``pattern[i]``, or None if there is none there.

    ``end`` is the index just past the quantifier, ``open_ended`` tells whether it has no upper bound, and
    ``many`` whether it can repeat more than once.
    """
    char = pattern[i]
    if char in "*+":
        return i + 1, True, True
    if char == "?":
        return i + 1, False, False
    found = re.match(r"\{(\d*)(,?)(\d*)\}", pattern[i:])
    if char != "{" or found is None or not (found.group(1) or found.group(3)):
        return None
    most = found.group(3) if found.group(2) else found.group(1)
    return i + found.end(), not most, not most or int(most) > 1


def _backtracks(pattern: str) -> str | None:
    """Return why ``pattern`` could make Python's regular expression engine take extremely long, or None."""
    groups = [{"repeat": False, "choice": False}]
    closed, repeats, i = None, 0, 0
    while i < len(pattern):
        char = pattern[i]
        quantifier = _quantifier(pattern, i) if char in "*+?{" else None
        if quantifier is not None:
            i, open_ended, many = quantifier
            if i < len(pattern) and pattern[i] in "?+":
                i += 1  # A trailing ? or + makes the quantifier lazy or possessive.
            repeats += open_ended
            if many and closed is not None and (closed["repeat"] or closed["choice"]):
                return "a repeated group holds a repeat or a choice, which can match one name in countless ways"
            if many:
                groups[-1]["repeat"] = True
            closed = None
            continue
        closed = None
        if char == "\\":
            i += 2
        elif char == "[":
            i = _skip_class(pattern, i)
        elif char == "(":
            groups.append({"repeat": False, "choice": False})
            i += 1
            if pattern.startswith("?", i):
                # Skip the opening of an extension group such as (?:, (?=, (?!, (?<=, (?<! or (?P<name>.
                # Inline flags such as (?i) are skipped up to their ')', which then closes the group.
                while i < len(pattern) and pattern[i] not in ":=!>)":
                    i += 1
                if i < len(pattern) and pattern[i] != ")":
                    i += 1
        elif char == ")":
            closed = groups.pop() if len(groups) > 1 else {"repeat": False, "choice": False}
            groups[-1]["repeat"] |= closed["repeat"]
            groups[-1]["choice"] |= closed["choice"]
            i += 1
        else:
            if char == "|":
                groups[-1]["choice"] = True
            i += 1
    if repeats > MAX_REPEATS:
        return f"it holds {repeats} open-ended repeats, more than the {MAX_REPEATS} allowed"
    return None


def _selector(value, where: str) -> list:
    """Return a selector as a list of names and compiled patterns, raising RulesError for an item that does not fit."""
    items = value if isinstance(value, list) else [value]
    if not items:
        raise RulesError(f"'{where}' is an empty list: give a name, \"re:<pattern>\", or a list of them.")
    out = []
    for item in items:
        if not isinstance(item, str) or not item:
            raise RulesError(f"'{where}' takes a name, \"re:<pattern>\", or a list of them, got {item!r}.")
        if item.startswith("re:"):
            try:
                compiled = re.compile(item[3:])
            except re.error as exc:
                raise RulesError(f"The pattern {item[3:]!r} in '{where}' does not compile: {exc}.") from None
            why = _backtracks(item[3:])
            if why is not None:
                raise RulesError(f"The pattern {item[3:]!r} in '{where}' is refused, because {why}. Use a character "
                                 "class ([ab]+) or list several selectors instead.")
            out.append(compiled)
        else:
            out.append(item)
    return out


#: Blender's suffix for a name already taken, a dot and three or more digits, as in Crate.001.
BLENDER_SUFFIX = re.compile(r"^(.+)\.\d{3,}$")


def _named(item: str, name: str) -> bool:
    """Return whether ``name`` is ``item``, or ``item`` with Blender's suffix.

    Crate.001 in a .blend file becomes Crate in a .glb file that export_glb writes with ``clean_names``, so
    one rule matches both.
    """
    if name == item:
        return True
    suffixed = BLENDER_SUFFIX.match(name)
    return suffixed is not None and suffixed.group(1) == item


def _matches(selector: list, name: str) -> bool:
    """Return whether any item of ``selector`` matches ``name``."""
    return any(_named(item, name) if isinstance(item, str) else item.search(name[:MAX_NAME_SEARCHED]) is not None
               for item in selector)


def _shown(selector: list) -> str:
    """Return a selector as the rules wrote it, its items joined by commas."""
    return ", ".join(item if isinstance(item, str) else f"re:{item.pattern}" for item in selector)


def _number(value, where: str, minimum: float | None = None) -> float:
    """Return ``value`` as a float, raising RulesError unless it is a finite number of at least ``minimum``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RulesError(f"'{where}' takes a number, got {value!r}.")
    if minimum is not None and value < minimum:
        raise RulesError(f"'{where}' must be at least {minimum}, got {value!r}.")
    return float(value)


def _flag(value, where: str) -> bool:
    """Return ``value``, raising RulesError unless it is true or false."""
    if not isinstance(value, bool):
        raise RulesError(f"'{where}' takes true or false, got {value!r}.")
    return value


def _entries(value, where: str) -> list:
    """Return ``value`` when it is a list of JSON objects, raising RulesError otherwise."""
    if not isinstance(value, list):
        raise RulesError(f"'{where}' takes a list of JSON objects, got {value!r}.")
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise RulesError(f"'{where}[{i}]' must be a JSON object, got {entry!r}.")
    return value


def _keys(entry: dict, where: str, allowed: tuple, needed: tuple = ()) -> None:
    """Raise RulesError when ``entry`` has a key not in ``allowed``, or lacks one in ``needed``."""
    for key in entry:
        if key not in allowed:
            raise RulesError(f"'{where}' does not take the key '{key}'. It takes {', '.join(allowed)}.")
    for key in needed:
        if key not in entry:
            raise RulesError(f"'{where}' needs '{key}'.")


def _srgb_to_linear(c: float) -> float:
    """Return an sRGB channel from 0 to 1 as a linear one."""
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def colour(value, where: str) -> list:
    """Return a colour as linear [r, g, b], given as an sRGB "#RRGGBB" or as a linear [r, g, b] list.

    A list is read as Blender and glTF store colours: linear. A fourth value in the list, alpha, is ignored.
    """
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
            raise RulesError(f"'{where}' takes \"#RRGGBB\" or [r, g, b], got {value!r}.")
        return [_srgb_to_linear(int(text[i:i + 2], 16) / 255.0) for i in (0, 2, 4)]
    if isinstance(value, list) and len(value) in (3, 4):
        return [_number(c, where) for c in value[:3]]
    raise RulesError(f"'{where}' takes \"#RRGGBB\" or [r, g, b], got {value!r}.")


def _triple(value, where: str) -> list:
    """Return a size limit [x, y, z], each a number of at least 0 or null, raising RulesError for anything else."""
    if not isinstance(value, list) or len(value) != 3:
        raise RulesError(f"'{where}' takes [x, y, z] in metres (null for no limit), got {value!r}.")
    return [None if v is None else _number(v, where, 0.0) for v in value]


def validate(given) -> dict:
    """Return the rule set ``given``, checked and made ready to evaluate.

    Raise RulesError naming the first part that does not fit the format.
    """
    if not isinstance(given, dict):
        raise RulesError(f"The rules must be a JSON object, got {_json_type(given)}.")
    _keys(given, "rules", KEYS)
    out: dict = {}
    if "required" in given:
        if not isinstance(given["required"], list):
            raise RulesError("'required' takes a list of names, \"re:<pattern>\"s or \"Parent/Child\" paths.")
        out["required"] = []
        for i, item in enumerate(given["required"]):
            where = f"required[{i}]"
            if isinstance(item, str) and "/" in item:
                # Split at the last /, so the parent half may contain a / and the child half may not. Each half
                # is a name or a "re:<pattern>".
                parent, _, child = item.rpartition("/")
                if not parent or not child:
                    raise RulesError(f"'{where}' takes \"Parent/Child\", got {item!r}.")
                out["required"].append((item, ("path", _selector(parent, where), _selector(child, where))))
            else:
                out["required"].append((item, ("select", _selector(item, where))))
    if "single_root" in given:
        out["single_root"] = _flag(given["single_root"], "single_root")
    if "retired" in given:
        out["retired"] = _selector(given["retired"], "retired")
    if "palette" in given:
        palette = given["palette"]
        if not isinstance(palette, dict):
            raise RulesError("'palette' takes a JSON object that maps material names to {base_color, metallic, "
                             "roughness, emission, strength}.")
        out["palette"] = {}
        for name, spec in palette.items():
            where = f"palette.{name}"
            if not isinstance(spec, dict):
                raise RulesError(f"'{where}' takes a JSON object of {', '.join(PALETTE_KEYS)}.")
            _keys(spec, where, PALETTE_KEYS)
            entry = {}
            for key in ("base_color", "emission"):
                if key in spec:
                    entry[key] = colour(spec[key], f"{where}.{key}")
            for key in ("metallic", "roughness"):
                if key in spec:
                    entry[key] = _number(spec[key], f"{where}.{key}", 0.0)
                    if entry[key] > 1:
                        raise RulesError(f"'{where}.{key}' must be between 0 and 1, got {spec[key]!r}.")
            if "strength" in spec:
                entry["strength"] = _number(spec["strength"], f"{where}.strength", 0.0)
            out["palette"][name] = entry
    if "palette_tolerance" in given:
        out["palette_tolerance"] = _number(given["palette_tolerance"], "palette_tolerance", 0.0)
    if "palette_only" in given:
        out["palette_only"] = _flag(given["palette_only"], "palette_only")
    if "extras" in given:
        out["extras"] = []
        for i, entry in enumerate(_entries(given["extras"], "extras")):
            where = f"extras[{i}]"
            _keys(entry, where, ("objects", "keys", "only"), ("objects", "keys"))
            keys = entry["keys"]
            if not isinstance(keys, dict) or not keys:
                raise RulesError(f"'{where}.keys' takes a JSON object that maps extra names to types "
                                 f"({', '.join(TYPES)}).")
            for key, kind in keys.items():
                if kind not in TYPES:
                    raise RulesError(f"'{where}.keys.{key}' takes one of {', '.join(TYPES)}, got {kind!r}.")
            out["extras"].append({"objects": _selector(entry["objects"], f"{where}.objects"), "keys": dict(keys),
                                  "only": _flag(entry.get("only", False), f"{where}.only")})
    if "size" in given:
        out["size"] = []
        for i, entry in enumerate(_entries(given["size"], "size")):
            where = f"size[{i}]"
            _keys(entry, where, ("objects", "min", "max"), ("objects",))
            if "min" not in entry and "max" not in entry:
                raise RulesError(f"'{where}' needs 'min' or 'max'.")
            out["size"].append({"objects": _selector(entry["objects"], f"{where}.objects"),
                                "min": _triple(entry["min"], f"{where}.min") if "min" in entry else [None] * 3,
                                "max": _triple(entry["max"], f"{where}.max") if "max" in entry else [None] * 3})
    if "origin_at_base" in given:
        value = given["origin_at_base"]
        if value is True or value is False:
            out["origin_at_base"] = value
        elif isinstance(value, (str, list)):
            out["origin_at_base"] = _selector(value, "origin_at_base")
        else:
            raise RulesError(f"'origin_at_base' takes true (every root) or names, got {value!r}.")
    if "budget" in given:
        value = given["budget"]
        if isinstance(value, list):
            out["budget"] = []
            for i, entry in enumerate(_entries(value, "budget")):
                where = f"budget[{i}]"
                _keys(entry, where, ("objects", "triangles"), ("objects", "triangles"))
                if isinstance(entry["triangles"], bool) or not isinstance(entry["triangles"], int) \
                        or entry["triangles"] < 0:
                    raise RulesError(f"'{where}.triangles' takes a whole number of triangles, "
                                     f"got {entry['triangles']!r}.")
                out["budget"].append({"objects": _selector(entry["objects"], f"{where}.objects"),
                                      "triangles": entry["triangles"]})
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out["budget"] = value
        else:
            raise RulesError(f"'budget' takes a whole number of triangles or a list of {{objects, triangles}}, "
                             f"got {value!r}.")
    if "apart" in given:
        out["apart"] = []
        for i, entry in enumerate(_entries(given["apart"], "apart")):
            where = f"apart[{i}]"
            _keys(entry, where, ("a", "b", "allow"), ("a", "b"))
            allow = entry.get("allow", [])
            if not isinstance(allow, list) or not all(
                    isinstance(pair, list) and len(pair) == 2 and all(isinstance(n, str) for n in pair)
                    for pair in allow):
                raise RulesError(f"'{where}.allow' takes a list of [name, name] pairs.")
            out["apart"].append({"a": _selector(entry["a"], f"{where}.a"), "b": _selector(entry["b"], f"{where}.b"),
                                 "allow": {frozenset(pair) for pair in allow}})
    return out


# ---------------------------------------------------------------------------
# Checking an asset against them
# ---------------------------------------------------------------------------


def _json_type(value) -> str:
    """Return the JSON name of the type of ``value``, with "a" or "an": "a list", "a string", "null"."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    return {str: "a string", list: "a list", dict: "an object"}.get(type(value), f"a {type(value).__name__}")


def _article(word: str) -> str:
    """Return ``word`` with "a" or "an" before it, chosen by its first letter."""
    return f"{'an' if word[:1] in 'aeiou' else 'a'} {word}"


def _value(value) -> str:
    """Return ``value`` written as JSON, such as null or true, shortened past 80 characters."""
    try:
        written = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        written = repr(value)
    return written if len(written) <= 80 else written[:77] + "..."


def _issue(code: str, message: str, severity: str = "error", count: int = 1, **extra) -> dict:
    """Return an issue that a rule found."""
    out = {"code": code, "severity": severity, "count": count, "message": message}
    out.update(extra)
    return out


def _listed(names: list) -> list:
    """Return at most LISTED of ``names``."""
    return names[:LISTED]


def object_key(o: dict):
    """Return the key that tells an object from every other: its ``id`` where names may repeat, else its name."""
    return o.get("id", o.get("name"))


def parent_key(o: dict):
    """Return the key of an object's parent: its ``parent_id`` where names may repeat, else its parent's name."""
    return o.get("parent_id", o.get("parent"))


class Tree:
    """The objects of a description, with each one's children, for the rules that measure a subtree."""

    def __init__(self, description: dict):
        """Read the objects of ``description`` and group them under their parents."""
        self.objects = [o for o in description.get("objects", []) if isinstance(o, dict)]
        self.children: dict = {}
        for o in self.objects:
            self.children.setdefault(parent_key(o), []).append(o)
        self.keys = {object_key(o) for o in self.objects}

    def roots(self) -> list:
        """Return the objects whose parent is not among the described objects."""
        return [o for o in self.objects if parent_key(o) not in self.keys]

    def subtree(self, top: dict) -> list:
        """Return ``top`` and every object below it."""
        out, stack, seen = [], [top], set()
        while stack:
            current = stack.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            out.append(current)
            stack.extend(self.children.get(object_key(current), []))
        return out

    def matching(self, selector: list) -> list:
        """Return the objects whose names ``selector`` matches."""
        return [o for o in self.objects if _matches(selector, o["name"])]

    def bounds(self, top: dict):
        """Return the bounds of ``top`` and every object below it, as ``(lo, hi)``, or None when none has bounds."""
        lo, hi = [math.inf] * 3, [-math.inf] * 3
        for o in self.subtree(top):
            box = o.get("bounds")
            if box:
                for i in range(3):
                    lo[i] = min(lo[i], box[0][i])
                    hi[i] = max(hi[i], box[1][i])
        return (lo, hi) if lo[0] <= hi[0] else None

    def triangles(self, top: dict) -> int:
        """Return the triangles of ``top`` and every object below it."""
        return sum(int(o.get("triangles") or 0) for o in self.subtree(top))


def _nothing(rule: str, selector: list) -> dict:
    """Return the warning that a rule's selector matches no object."""
    return _issue("rule_matches_nothing", f"The {rule} rule names {_shown(selector)}, and no object matches.",
                  "warning")


def _check_type(value, kind: str) -> bool:
    """Return whether ``value`` has the extras type ``kind``; an integer may also be a float with no fraction."""
    if kind == "any":
        return True
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if kind == "integer":
        return isinstance(value, int) or float(value).is_integer()
    return True


def _palette(rules: dict, materials: list) -> list:
    """Return the off_palette and not_in_palette issues of the materials."""
    out = []
    palette = rules.get("palette")
    tolerance = rules.get("palette_tolerance", 0.001)
    if palette is not None:
        off, why = [], []
        for m in materials:
            spec = palette.get(m["name"])
            if spec is None:
                continue
            problems = []
            for key in ("base_color", "metallic", "roughness"):
                if key not in spec:
                    continue
                have = m.get(key)
                if have is None:
                    problems.append(f"{key} could not be read (a texture or shader nodes set it)")
                elif key == "base_color":
                    if any(abs(a - b) > tolerance for a, b in zip(have[:3], spec[key], strict=False)):
                        problems.append(f"base_color is {[round(c, 4) for c in have[:3]]}, not "
                                        f"{[round(c, 4) for c in spec[key]]}")
                elif abs(have - spec[key]) > tolerance:
                    problems.append(f"{key} is {round(have, 4)}, not {spec[key]}")
            if "emission" in spec or "strength" in spec:
                want = [c * spec.get("strength", 1.0) for c in spec.get("emission", [1.0, 1.0, 1.0])]
            else:
                want = [0.0, 0.0, 0.0]
            have = m.get("emission")
            if have is None:
                if "emission" in spec:
                    problems.append("emission could not be read (a texture or shader nodes set it)")
            else:
                slack = max(tolerance, 0.02 * max(want))
                if any(abs(a - b) > slack for a, b in zip(have[:3], want, strict=False)):
                    problems.append(f"emission (colour times strength) is {[round(c, 4) for c in have[:3]]}, not "
                                    f"{[round(c, 4) for c in want]}")
            if problems:
                off.append(m["name"])
                why.append(f"In {m['name']}, {'; '.join(problems)}.")
        if off:
            out.append(_issue("off_palette", "Some materials differ from the palette. " + " ".join(why[:LISTED]),
                              count=len(off), materials=_listed(off)))
    if rules.get("palette_only"):
        stray = [m["name"] for m in materials if m["name"] not in (palette or {})]
        if stray:
            out.append(_issue("not_in_palette", f"These materials are not in the palette: "
                                                f"{', '.join(stray[:LISTED])}.",
                              count=len(stray), materials=_listed(stray)))
    return out


def _required(rules: dict, objects: list) -> list:
    """Return a list holding the missing_required issue for the required names no object has, or an empty list.

    For each missing "Parent/Child" path, the issue names the children that the parent does have.
    """
    names = {o["name"] for o in objects}
    missing, children, notes = [], {}, []
    for shown, (kind, *what) in rules["required"]:
        if kind == "select":
            if not any(_matches(what[0], name) for name in names):
                missing.append(shown)
            continue
        parent, child = what
        under = [o["name"] for o in objects if o.get("parent") is not None and _matches(parent, str(o["parent"]))]
        if any(_matches(child, name) for name in under):
            continue
        missing.append(shown)
        children[shown] = _listed(under)
        parents = [str(o["name"]) for o in objects if _matches(parent, o["name"])]
        if not parents:
            notes.append(f"No object matches {_shown(parent)}.")
        elif under:
            notes.append(f"{', '.join(parents[:4])} {'has' if len(parents) == 1 else 'have'} the children "
                         f"{', '.join(under[:LISTED])}.")
        else:
            notes.append(f"{', '.join(parents[:4])} {'has' if len(parents) == 1 else 'have'} no children.")
    if not missing:
        return []
    words = f"Required objects are missing: {', '.join(missing[:LISTED])}."
    if notes:
        words += " " + " ".join(notes[:LISTED])
    extra = {"children": {k: children[k] for k in list(children)[:LISTED]}} if children else {}
    return [_issue("missing_required", words, count=len(missing), names=_listed(missing), **extra)]


def evaluate(rules: dict, description: dict, overlap=None, narrowed: bool = False) -> list:
    """Return the issues that the validated ``rules`` find in ``description``.

    ``overlap(a, b)`` takes two objects' keys (see ``object_key``) and returns True when the objects cross
    each other, the inner object's key when one lies wholly inside the other, False when neither, and None
    when it cannot tell. Without ``overlap``, the apart rules are reported as not checked. ``narrowed`` means
    that the description holds only the objects a check was narrowed to. One rule set can then serve many
    assets, so a rule that matches none of the objects is skipped silently rather than reported as
    rule_matches_nothing.
    """
    tree = Tree(description)
    objects = tree.objects
    roots = tree.roots()
    materials = [m for m in description.get("materials", []) if isinstance(m, dict)]
    out: list = []

    def nothing(rule: str, selector: list) -> None:
        """Report that a rule matches no object, unless the check was narrowed to some objects."""
        if not narrowed:
            out.append(_nothing(rule, selector))

    if rules.get("required"):
        out.extend(_required(rules, objects))

    if rules.get("single_root"):
        root_names = [str(o["name"]) for o in roots]
        if len(root_names) != 1:
            roots_found = f"there are {len(root_names)}: {', '.join(root_names[:LISTED])}" if root_names else \
                "there are none"
            out.append(_issue("not_single_root", f"single_root asks for exactly one object without a parent, and "
                                                 f"{roots_found}.",
                              count=len(root_names), objects=_listed(root_names)))

    if rules.get("retired"):
        bad_objects = [o["name"] for o in objects if _matches(rules["retired"], o["name"])]
        bad_materials = [m["name"] for m in materials if _matches(rules["retired"], m["name"])]
        if bad_objects or bad_materials:
            shown = ", ".join((bad_objects + bad_materials)[:LISTED])
            out.append(_issue("retired_name", f"Retired names are in use: {shown}.",
                              count=len(bad_objects) + len(bad_materials), objects=_listed(bad_objects),
                              materials=_listed(bad_materials)))

    out.extend(_palette(rules, materials))

    for entry in rules.get("extras", []):
        matched = tree.matching(entry["objects"])
        if not matched:
            nothing("extras", entry["objects"])
        missing, wrong, missing_notes, wrong_notes = [], [], [], []
        for o in matched:
            extras = o.get("extras") or {}
            for key, kind in entry["keys"].items():
                if key not in extras:
                    missing.append(o["name"])
                    missing_notes.append(f"{o['name']} lacks '{key}'.")
                elif not _check_type(extras[key], kind):
                    wrong.append(o["name"])
                    wrong_notes.append(f"{o['name']}.{key} is {_value(extras[key])}, not {_article(kind)}.")
        if missing:
            missing = list(dict.fromkeys(missing))
            out.append(_issue("missing_extra", " ".join(missing_notes[:LISTED]), count=len(missing),
                              objects=_listed(missing)))
        if wrong:
            wrong = list(dict.fromkeys(wrong))
            out.append(_issue("wrong_extra_type", " ".join(wrong_notes[:LISTED]), count=len(wrong),
                              objects=_listed(wrong)))
        if entry["only"]:
            others = [o["name"] for o in objects if o not in matched
                      and any(key in (o.get("extras") or {}) for key in entry["keys"])]
            if others:
                carry = "carries them" if len(others) == 1 else "carry them"
                out.append(_issue("extra_not_allowed", f"Only {_shown(entry['objects'])} may carry the extras "
                                                       f"{', '.join(entry['keys'])}, but "
                                                       f"{', '.join(others[:LISTED])} also {carry}.",
                                  count=len(others), objects=_listed(others)))

    for entry in rules.get("size", []):
        matched = tree.matching(entry["objects"])
        if not matched:
            nothing("size", entry["objects"])
        bad, sizes = [], {}
        for o in matched:
            box = tree.bounds(o)
            if box is None:
                continue
            size = [box[1][i] - box[0][i] for i in range(3)]
            low = any(m is not None and s < m - 1e-6 for s, m in zip(size, entry["min"], strict=True))
            high = any(m is not None and s > m + 1e-6 for s, m in zip(size, entry["max"], strict=True))
            if low or high:
                bad.append(o["name"])
                sizes[o["name"]] = [round(s, 5) for s in size]
        if bad:
            # A limit of [null, null, null] is one the rule did not give, so it is left out of the message.
            limits = " and ".join(f"{end} {_value(entry[end])}" for end in ("min", "max")
                                  if any(m is not None for m in entry[end]))
            out.append(_issue("size_out_of_range", f"These sizes (X, Y, Z in metres) fall outside {limits}: "
                                                   + "; ".join(f"{n} is {sizes[n]}" for n in bad[:LISTED]) + ".",
                              count=len(bad), objects=_listed(bad), sizes={n: sizes[n] for n in bad[:LISTED]}))

    origin_rule = rules.get("origin_at_base")
    if origin_rule:
        if origin_rule is True:
            matched = roots
        else:
            matched = tree.matching(origin_rule)
            if not matched:
                nothing("origin_at_base", origin_rule)
        bad = []
        for o in matched:
            box = tree.bounds(o)
            origin = o.get("origin")
            if box is None or origin is None:
                continue
            if not origin_at_base(origin, box):
                bad.append(o["name"])
        if bad:
            out.append(_issue("origin_not_at_base", "These origins are not on the bottom of their bounds, or lie "
                                                    f"outside their footprints: {', '.join(bad[:LISTED])}.",
                              count=len(bad), objects=_listed(bad)))

    budget = rules.get("budget")
    if isinstance(budget, int):
        total = sum(int(o.get("triangles") or 0) for o in objects)
        if total > budget:
            out.append(_issue("over_budget", f"The asset has {total:,} triangles in all, more than the budget of "
                                             f"{budget:,}.",
                              objects=["all"], triangles={"all": total}))
    elif budget:
        over, counted = [], {}
        for entry in budget:
            matched = tree.matching(entry["objects"])
            if not matched:
                nothing("budget", entry["objects"])
            for o in matched:
                count = tree.triangles(o)
                if count > entry["triangles"]:
                    over.append(o["name"])
                    counted[o["name"]] = count
        if over:
            out.append(_issue("over_budget", "These objects are over their triangle budgets: " + "; ".join(
                f"{n} has {counted[n]:,}" for n in over[:LISTED]) + ".", count=len(over), objects=_listed(over),
                triangles={n: counted[n] for n in over[:LISTED]}))

    for entry in rules.get("apart", []):
        a = tree.matching(entry["a"])
        b = tree.matching(entry["b"])
        for side, selector in ((a, entry["a"]), (b, entry["b"])):
            if not side:
                nothing("apart", selector)
        if not a or not b:
            continue
        # The pairs are counted before any is built, so a rule matching thousands of objects never builds
        # millions of pairs.
        if overlap is None or len(a) * len(b) > MAX_PAIRS:
            why = "it needs the objects' geometry, which this check does not have" if overlap is None else \
                f"it matches {len(a) * len(b):,} pairs, more than the {MAX_PAIRS:,} it tests"
            out.append(_issue("not_checked", f"The apart rule for {_shown(entry['a'])} and {_shown(entry['b'])} was "
                                             f"not checked, because {why}.", "info"))
            continue
        pairs = {}
        for x in a:
            for y in b:
                if x is not y and frozenset((x["name"], y["name"])) not in entry["allow"]:
                    pairs.setdefault(frozenset((id(x), id(y))), (x, y))
        crossing, inside, unknown = [], [], 0
        for x, y in pairs.values():
            met = overlap(object_key(x), object_key(y))
            if met is None:
                unknown += 1
            elif met is True:
                crossing.append([x["name"], y["name"]])
            elif met:
                crossing.append([x["name"], y["name"]])
                inner, outer = (x, y) if met == object_key(x) else (y, x)
                inside.append([inner["name"], outer["name"]])
        if crossing:
            words = "Objects that must stay apart cross, or lie one inside the other: " + ", ".join(
                f"{x} and {y}" for x, y in crossing[:LISTED]) + "."
            extra = {"inside": inside[:LISTED]} if inside else {}
            if inside:
                words += " Of these, " + "; ".join(f"{x} lies inside {y}" for x, y in inside[:LISTED]) + "."
            out.append(_issue("sets_intersect", words, count=len(crossing), pairs=crossing[:LISTED], **extra))
        if unknown:
            pairs_word = "pair" if unknown == 1 else "pairs"
            out.append(_issue("not_checked", f"{unknown:,} {pairs_word} of the apart rule could not be measured, "
                                             "because at least one object in each pair has no mesh.",
                              "info", count=unknown))
    return out


def origin_at_base(origin, box) -> bool:
    """Return whether ``origin`` sits on the bottom of ``box`` and inside its footprint, within ORIGIN_TOLERANCE."""
    (lo, hi) = box
    if abs(origin[2] - lo[2]) > ORIGIN_TOLERANCE:
        return False
    return all(lo[i] - ORIGIN_TOLERANCE <= origin[i] <= hi[i] + ORIGIN_TOLERANCE for i in (0, 1))
