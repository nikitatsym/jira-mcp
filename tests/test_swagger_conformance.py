"""Conformance check: every hand-written API call matches Atlassian's OpenAPI spec.

Jira silently ignores query params and body fields it does not know, so a
misspelled name is an invisible bug: the call still returns 200 and the filter
simply never applies. Neither the type system nor the integration suite can see
that class of typo, so this test reads every registered op in `jira_mcp.tools`
off its own AST and asserts each call's method, path, query-param names and
top-level body-field names against Atlassian's published OpenAPI 3 documents.

The specs are vendored gzipped under `tests/specs/` instead of fetched at test
time: this check belongs in the default `dev.py check` gate that runs on every
commit, and a gate that needs the network fails offline. Refresh a copy with
`curl -sSL <url> | gzip -9 > <file>` from its upstream document:

    https://developer.atlassian.com/cloud/jira/platform/swagger-v3.v3.json
        -> tests/specs/jira-platform-v3.json.gz
    https://developer.atlassian.com/cloud/jira/software/swagger.v3.json
        -> tests/specs/jira-software-v3.json.gz

Stated limits of the oracle, none of which can produce a false pass on a name
this test does claim to check:

- The names *inside* the `fields` envelope on issue create/update/transition
  are Jira field ids (`duedate`, `fixVersions`, ...) served as runtime metadata
  by `/rest/api/3/field`, and the spec types the envelope as a free-form
  object, so only the envelope key itself is checked.
- A payload the caller hands over whole is opaque: its keys are the caller's,
  not ours, so body comparison stands down for it. Query params are always ours
  to spell, so an opaque `params=` blocks the op instead.
- Enum checking maps an arg to its wire name best-effort (dict literals and
  subscript assigns); an arg whose value is transformed on the way to the wire
  stays unmapped and is not enum-checked, and spec enums are read off the
  parameter schema without resolving refs or compositions.
"""

from __future__ import annotations

import ast
import functools
import gzip
import inspect
import json
import re
import textwrap
import typing
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeGuard

import pytest

from jira_mcp import tools

# Ops whose call shape the extractor below cannot read. ONLY code shapes belong
# here - never a name mismatch, which is the whole point of this test.
UNANALYZABLE_OK: dict[str, str] = {}

# Ops with no wire call of their own: they only drive other registered ops,
# whose calls are checked in their own right.
NO_WIRE_CALL_OK: frozenset[str] = frozenset()

# Ops making one call whose endpoint is deliberately absent from the published
# spec: op -> (method, path, reason). Reviewed entries only; the tests fail if
# the op stops making exactly that call or the endpoint reappears in the spec.
SPEC_GAPS: dict[str, tuple[str, str, str]] = {}

# Atlassian splits the API across two documents: the platform one owns
# /rest/api/3, the Jira Software one owns /rest/agile/1.0. Their component
# schemas are document-local and collide by name, so each document's endpoints
# are indexed against its own schemas and the paths are asserted disjoint.
_SPEC_FILES = (
    Path(__file__).parent / "specs" / "jira-platform-v3.json.gz",
    Path(__file__).parent / "specs" / "jira-software-v3.json.gz",
)

# JiraClient method -> HTTP method.
_CLIENT_VERBS = {
    "get": "GET",
    "get_raw": "GET",
    "post": "POST",
    "post_multipart": "POST",
    "put": "PUT",
    "delete": "DELETE",
}
# Client kwarg carrying a named body -> the media kind it lands in.
_BODY_KWARGS = {"json": "json", "files": "form"}
_NO_PAYLOAD_KWARGS = frozenset({"headers"})

# The client factory the extractor already models; its own source trips the
# wire marker below. Everything else that reaches the marker inside a helper is
# a wire call hiding from the check.
_PLUMBING = frozenset({"_get_client"})
_WIRE_MARKER = re.compile(r"_get_client\(|httpx\.")

# Statically unknown payload: an object the caller hands over whole.
_OPAQUE: None = None


@functools.cache
def _hits_wire(target: Callable[..., Any]) -> bool:
    # getsource failing here is a loud test error by design: an unreadable
    # helper cannot be assumed clean.
    return bool(_WIRE_MARKER.search(inspect.getsource(target)))


@dataclass(frozen=True)
class _WireCall:
    """One outbound HTTP call, as read off the source of an op."""

    method: str
    path: str
    query: frozenset[str]
    # Body names per media kind; None where the payload is opaque.
    json_body: frozenset[str] | None
    form_body: frozenset[str] | None


# Stand-in for a call the extractor gave up on; dropped with the rest once the
# op is marked unreadable.
_UNREADABLE = _WireCall("", "", frozenset(), frozenset(), frozenset())


def _is_named(node: ast.expr | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _is_call_to(node: ast.AST | None, name: str) -> TypeGuard[ast.Call]:
    return isinstance(node, ast.Call) and _is_named(node.func, name)


def _freeze(names: set[str] | None) -> frozenset[str] | None:
    return None if names is None else frozenset(names)


# -- AST extraction ---------------------------------------------------------


class _OpExtractor:
    """Reads the wire calls an op makes straight off its source.

    Every shape outside the grammar records a reason in `blocked` and the op is
    reported as unanalyzable rather than half-checked.
    """

    def __init__(self, fn: Callable[..., Any]) -> None:
        self.params = list(inspect.signature(fn).parameters)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        self.stmts: list[ast.stmt] = tree.body[0].body  # type: ignore[attr-defined]
        self.blocked: str | None = None
        self.clients = self._bound_clients()

    def calls(self) -> list[_WireCall]:
        found = []
        called: set[int] = set()
        attributes: list[ast.Attribute] = []
        reached: set[int] = set()
        for node in self._walk():
            if isinstance(node, ast.Attribute) and self._is_client(node.value):
                attributes.append(node)
                reached.add(id(node.value))
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Attribute) and self._is_client(fn.value):
                called.add(id(fn))
                found.append(self._from_client(node, fn.attr))
            elif isinstance(fn, ast.Name):
                self._check_helper(fn.id)
        for attribute in attributes:
            if id(attribute) not in called:
                self._block(f"client method {attribute.attr!r} is passed around, not called here")
        self._check_bound_clients(reached)
        return [] if self.blocked else found

    def _bound_clients(self) -> set[str]:
        """Names bound to a client, as in `client = _get_client()`."""
        names: set[str] = set()
        for node in self._walk():
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            else:
                continue
            if _is_call_to(value, "_get_client"):
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
        return names

    def _is_client(self, node: ast.expr | None) -> bool:
        if _is_call_to(node, "_get_client"):
            return True
        return isinstance(node, ast.Name) and node.id in self.clients

    def _is_client_call(self, node: ast.Call) -> bool:
        return isinstance(node.func, ast.Attribute) and self._is_client(node.func.value)

    def _check_bound_clients(self, reached: set[int]) -> None:
        """A bound client going anywhere but `<client>.verb(...)` takes the
        calls it makes out of this extractor's sight."""
        for node in self._walk():
            if not isinstance(node, ast.Name) or node.id not in self.clients:
                continue
            if isinstance(node.ctx, ast.Store) or id(node) in reached:
                continue
            self._block(f"client {node.id!r} is passed around, not called here")

    def _check_helper(self, name: str) -> None:
        """Block ops whose helpers hit the wire where this test cannot see."""
        if name in _PLUMBING:
            return
        target = getattr(tools, name, None)
        # Registered ops another op drives are checked in their own right.
        if not inspect.isfunction(target) or hasattr(target, "_mcp_group"):
            return
        if _hits_wire(target):
            self._block(f"calls {name}(), which makes HTTP calls this extractor cannot read")

    def _walk(self) -> Iterator[ast.AST]:
        for stmt in self.stmts:
            yield from ast.walk(stmt)

    def _block(self, reason: str) -> None:
        if self.blocked is None:
            self.blocked = reason

    # -- literals -----------------------------------------------------------

    def _const_str(self, node: ast.expr | None) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        self._block(f"expected a string literal, found {type(node).__name__}")
        return ""

    def _dict_keys(self, node: ast.Dict) -> set[str]:
        if any(k is None for k in node.keys):
            self._block("dict literal uses ** unpacking")
            return set()
        return {self._const_str(k) for k in node.keys}

    # -- name derivation ----------------------------------------------------

    def _payload(self, value: ast.expr | None) -> set[str] | None:
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            return set()
        if isinstance(value, ast.Dict):
            return self._dict_keys(value)
        # _fields() is the {"fields": ...} envelope; its contents are Jira
        # field ids, which the spec does not model (see the module docstring).
        if _is_call_to(value, "_fields"):
            return {"fields"}
        if isinstance(value, ast.Name):
            return self._resolve_var(value.id)
        self._block(f"payload is a {type(value).__name__}, not a readable dict")
        return set()

    def _resolve_var(self, name: str) -> set[str] | None:
        """Union of the keys a local dict variable can end up carrying."""
        keys: set[str] = set()
        assigned = False
        for node in self._walk():
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign):
                targets, value = [node.target], node.value
            elif isinstance(node, ast.AugAssign) and _is_named(node.target, name):
                self._block(f"augmented assignment to {name!r}")
            elif isinstance(node, ast.Call):
                self._check_escape(name, node)
            for target in targets:
                if _is_named(target, name):
                    initial = self._payload(value)
                    if initial is None:
                        return _OPAQUE
                    keys |= initial
                    assigned = True
                elif isinstance(target, ast.Subscript) and _is_named(target.value, name):
                    keys.add(self._const_str(target.slice))
        if assigned:
            return keys
        if name in self.params:
            return _OPAQUE  # the caller supplies the object; its keys are its own
        self._block(f"no assignment to {name!r} found in the op")
        return set()

    def _check_escape(self, name: str, node: ast.Call) -> None:
        """A tracked payload a helper can reach may gain keys off-screen."""
        if isinstance(node.func, ast.Attribute) and _is_named(node.func.value, name):
            self._block(f"{name}.{node.func.attr}() mutates the payload")
            return
        # The wire call is where the payload is meant to go, and _fields() is
        # modelled plumbing; anything else could put a name on the wire.
        if self._is_client_call(node) or _is_call_to(node, "_fields"):
            return
        arguments = [*node.args, *(kw.value for kw in node.keywords)]
        if any(_is_named(argument, name) for argument in arguments):
            self._block(f"{name!r} is handed to another call, which could add keys")

    # -- call shapes --------------------------------------------------------

    def _from_client(self, node: ast.Call, verb: str) -> _WireCall:
        if verb not in _CLIENT_VERBS:
            self._block(f"unknown client method {verb!r}")
            return _UNREADABLE
        args = list(node.args)
        if not args:
            self._block(f"{verb}() called without a path")
            return _UNREADABLE
        if args[1:]:
            self._block(f"{verb}() passes a payload positionally")
            return _UNREADABLE
        path = self._path(args[0])
        query: set[str] = set()
        bodies: dict[str, set[str] | None] = {}
        for kw in node.keywords:
            kind = _BODY_KWARGS.get(kw.arg or "")
            if kw.arg == "params":
                names = self._payload(kw.value)
                if names is None:
                    # Query names are ours to spell whatever the caller passes,
                    # so an opaque dict is a hole, not a caller's business.
                    self._block(f"{verb}() is handed an opaque param dict")
                else:
                    query |= names
            elif kind is not None:
                bodies[kind] = self._payload(kw.value)
            elif kw.arg not in _NO_PAYLOAD_KWARGS:
                self._block(f"{verb}() carries a payload in {kw.arg!r}")
        return _WireCall(
            _CLIENT_VERBS[verb],
            path,
            frozenset(query),
            _freeze(bodies.get("json", set())),
            _freeze(bodies.get("form", set())),
        )

    def _path(self, node: ast.expr) -> str:
        if isinstance(node, ast.Constant):
            return self._const_str(node)
        if not isinstance(node, ast.JoinedStr):
            self._block(f"path is a {type(node).__name__}, not a literal")
            return ""
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant):
                parts.append(str(piece.value))
            elif isinstance(piece, ast.FormattedValue) and isinstance(piece.value, ast.Name):
                parts.append("{" + piece.value.id + "}")
            else:
                self._block("path f-string interpolates an expression")
                return ""
        return "".join(parts)


@dataclass(frozen=True)
class _Ops:
    analyzed: dict[str, list[_WireCall]]
    unanalyzable: dict[str, str]
    no_wire_call: list[str]


@functools.lru_cache(maxsize=1)
def _extract_ops() -> _Ops:
    analyzed: dict[str, list[_WireCall]] = {}
    unanalyzable: dict[str, str] = {}
    no_wire_call: list[str] = []
    members = inspect.getmembers(
        tools, lambda o: inspect.isfunction(o) and hasattr(o, "_mcp_group")
    )
    for name, fn in sorted(members):
        extractor = _OpExtractor(fn)
        calls = extractor.calls()
        if extractor.blocked:
            unanalyzable[name] = extractor.blocked
        elif calls:
            analyzed[name] = calls
        else:
            no_wire_call.append(name)
    return _Ops(analyzed, unanalyzable, no_wire_call)


# -- Spec index -------------------------------------------------------------


_PLACEHOLDER_SEGMENT = re.compile(r"^\{\w+\}$")
_MAX_SCHEMA_DEPTH = 8


def _segments(path: str) -> list[str | None]:
    """Split into /-segments; placeholder segments become None."""
    return [
        None if _PLACEHOLDER_SEGMENT.match(seg) else seg
        for seg in path.strip("/").split("/")
    ]


def _matches(ours: list[str | None], spec: list[str | None]) -> bool:
    """A spec placeholder accepts anything; our placeholder needs one."""
    if len(ours) != len(spec):
        return False
    return all(s is None or o == s for o, s in zip(ours, spec))


def _media_kind(media_type: str) -> str | None:
    """Which of the client's body kwargs lands in this media type."""
    name = media_type.split(";")[0].strip().lower()
    if name == "application/json" or name.endswith("+json"):
        return "json"
    if name in ("multipart/form-data", "application/x-www-form-urlencoded"):
        return "form"
    return None


class _Spec:
    """Query/body name sets per (path template, method), matched structurally."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.endpoints: dict[tuple[str, str], frozenset[str]] = {}
        self.query_enums: dict[tuple[str, str], dict[str, frozenset[str]]] = {}
        # Writable body names per media kind; a None entry is a body the spec
        # declares unnamed (a binary upload or a bare JSON scalar), a missing
        # entry is a media kind the endpoint does not accept at all.
        self.bodies: dict[tuple[str, str], dict[str, frozenset[str] | None]] = {}
        self._templates: dict[str, list[str | None]] = {}
        for doc in docs:
            schemas = (doc.get("components") or {}).get("schemas") or {}
            for path, item in doc["paths"].items():
                if path in self._templates:
                    raise ValueError(
                        f"path {path!r} is defined in two spec documents; their "
                        "component schemas are document-local and cannot be mixed"
                    )
                self._templates[path] = _segments(path)
                for method, operation in item.items():
                    if isinstance(operation, dict):
                        self._read_operation(path, method, operation, schemas)

    def _read_operation(
        self, path: str, method: str, operation: dict[str, Any], schemas: dict[str, Any]
    ) -> None:
        query: set[str] = set()
        enums: dict[str, frozenset[str]] = {}
        for param in operation.get("parameters") or []:
            if param.get("in") != "query":
                continue
            query.add(param["name"])
            # Formal enums only; prose-documented value sets are not
            # machine-checkable and are skipped.
            schema = param.get("schema") or {}
            values = schema.get("enum") or (schema.get("items") or {}).get("enum")
            if values:
                enums[param["name"]] = frozenset(values)
        key = (path, method.upper())
        self.endpoints[key] = frozenset(query)
        self.query_enums[key] = enums
        self.bodies[key] = self._body_kinds(operation.get("requestBody"), schemas)

    def _body_kinds(
        self, request_body: Any, schemas: dict[str, Any]
    ) -> dict[str, frozenset[str] | None]:
        kinds: dict[str, set[str] | None] = {}
        if isinstance(request_body, dict):
            for media_type, media in (request_body.get("content") or {}).items():
                kind = _media_kind(media_type)
                if kind is None:
                    continue
                names = self._properties((media or {}).get("schema"), schemas)
                if kind not in kinds:
                    kinds[kind] = names
                elif names is None or kinds[kind] is None:
                    kinds[kind] = None
                else:
                    kinds[kind] = kinds[kind] | names
        return {kind: _freeze(names) for kind, names in kinds.items()}

    def _properties(
        self, schema: Any, schemas: dict[str, Any], depth: int = 0
    ) -> set[str] | None:
        """Writable field names a body schema declares, or None when it names
        none - a scalar, a binary, or a free-form object."""
        if depth > _MAX_SCHEMA_DEPTH:
            raise ValueError(f"schema nesting deeper than {_MAX_SCHEMA_DEPTH} refs")
        if not isinstance(schema, dict):
            return None
        ref = schema.get("$ref")
        if ref:
            return self._properties(self._resolve(ref, schemas), schemas, depth + 1)
        declared = schema.get("properties") or {}
        names = {
            name
            for name, prop in declared.items()
            if not self._read_only(prop, schemas)
        }
        composed = False
        for keyword in ("allOf", "anyOf", "oneOf"):
            for member in schema.get(keyword) or []:
                composed = True
                members = self._properties(member, schemas, depth + 1)
                if members is None:
                    return None  # a free-form branch accepts any name
                names |= members
        if declared or composed:
            return names
        # Nothing declared: free-form unless the schema forbids extra keys.
        return set() if schema.get("additionalProperties") is False else None

    def _read_only(self, prop: Any, schemas: dict[str, Any]) -> bool:
        """readOnly properties are response-only; sending one is a no-op."""
        if not isinstance(prop, dict):
            return False
        if prop.get("readOnly"):
            return True
        ref = prop.get("$ref")
        if not ref:
            return False
        target = self._resolve(ref, schemas)
        return isinstance(target, dict) and bool(target.get("readOnly"))

    def _resolve(self, ref: str, schemas: dict[str, Any]) -> Any:
        """A dangling ref is a broken index, never a silent pass: it fails the
        build instead of quietly disabling the body check for that endpoint."""
        name = ref.rsplit("/", 1)[-1]
        if name not in schemas:
            raise ValueError(f"spec reference {ref!r} does not resolve to a schema")
        return schemas[name]

    def _pool(self, path: str) -> list[str]:
        # An exact template match is unambiguous by construction; structural
        # matching is the fallback for paths whose placeholders differ.
        if path in self._templates:
            return [path]
        ours = _segments(path)
        return [p for p, template in self._templates.items() if _matches(ours, template)]

    def candidates(self, path: str, method: str) -> list[str]:
        return [p for p in self._pool(path) if (p, method) in self.endpoints]

    def describe_pool(self, path: str) -> str:
        known = sorted(
            f"{m} {p}" for p in self._pool(path) for (p2, m) in self.endpoints if p2 == p
        )
        return ", ".join(known) if known else "nothing with this path shape"


@pytest.fixture(scope="session")
def spec() -> _Spec:
    docs = []
    for path in _SPEC_FILES:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            docs.append(json.load(handle))
    return _Spec(docs)


def _is_spec_gap(op: str, call: _WireCall) -> bool:
    gap = SPEC_GAPS.get(op)
    return gap is not None and (call.method, call.path) == (gap[0], gap[1])


# -- Tests ------------------------------------------------------------------


def test_every_unanalyzable_op_is_allowlisted() -> None:
    unknown = {
        name: reason
        for name, reason in _extract_ops().unanalyzable.items()
        if name not in UNANALYZABLE_OK
    }
    assert not unknown, (
        "Ops whose calls this test cannot read are missing from UNANALYZABLE_OK. "
        "Reshape the op into a readable form, teach the extractor the shape, or "
        "allowlist it - code shapes only, NEVER a name mismatch:\n"
        + "\n".join(f"  {name}: {reason}" for name, reason in sorted(unknown.items()))
    )


def test_allowlist_has_no_stale_entries() -> None:
    ops = _extract_ops()
    stale = sorted(set(UNANALYZABLE_OK) - set(ops.unanalyzable))
    assert not stale, (
        "These ops are analyzable now - drop them from UNANALYZABLE_OK so the "
        f"allowlist can only shrink: {stale}"
    )
    orphaned = sorted(
        f"{op}: {method} {path}"
        for op, (method, path, _) in SPEC_GAPS.items()
        if not any(_is_spec_gap(op, call) for call in ops.analyzed.get(op, ()))
    )
    assert not orphaned, (
        "SPEC_GAPS entries name a call their op no longer makes, so the gap "
        f"covers nothing and the real call goes unchecked: {orphaned}"
    )


def test_no_wire_call_ops_are_expected() -> None:
    ops = _extract_ops()
    unexpected = sorted(set(ops.no_wire_call) - NO_WIRE_CALL_OK)
    assert not unexpected, (
        "Ops with no readable wire call of their own. If they truly only "
        f"drive other registered ops, add them to NO_WIRE_CALL_OK: {unexpected}"
    )
    stale = sorted(NO_WIRE_CALL_OK - set(ops.no_wire_call))
    assert not stale, f"NO_WIRE_CALL_OK entries no longer match reality: {stale}"


def _arg_wire_names(fn: Callable[..., Any]) -> dict[str, str]:
    """Best-effort map from a signature arg to the wire name it is sent under.

    Sources: dict literals `{"key": arg}` and subscript assigns
    `params["key"] = arg`. Args sent under their own name need no entry; args
    whose value is transformed before sending stay unmapped and are simply not
    enum-checked.
    """
    mapping: dict[str, str] = {}
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
            and isinstance(node.value, ast.Name)
        ):
            mapping[node.value.id] = node.targets[0].slice.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(value, ast.Name):
                    mapping[value.id] = key.value
    return mapping


def _literal_values(annotation: Any) -> frozenset[str]:
    """String values of every Literal reachable inside the annotation."""
    values: set[str] = set()
    stack = [annotation]
    while stack:
        ann = stack.pop()
        if typing.get_origin(ann) is Literal:
            values |= {a for a in typing.get_args(ann) if isinstance(a, str)}
        else:
            stack.extend(typing.get_args(ann))
    return frozenset(values)


def test_query_enum_values_match_the_spec(spec: _Spec) -> None:
    """A Literal value the spec's enum lacks is the silent-lie class again:
    Jira quietly falls back to its default instead of erroring. Query params
    only - body enums are rare and not modeled here."""
    findings: list[str] = []
    for op, calls in sorted(_extract_ops().analyzed.items()):
        fn = getattr(tools, op)
        hints = typing.get_type_hints(fn, include_extras=True, localns=vars(tools))
        wire_of = _arg_wire_names(fn)
        for call in calls:
            matches = spec.candidates(call.path, call.method)
            if _is_spec_gap(op, call) or len(matches) != 1:
                continue
            enums = spec.query_enums.get((matches[0], call.method), {})
            for arg, annotation in hints.items():
                values = _literal_values(annotation)
                wire = wire_of.get(arg, arg)
                if not values or wire not in call.query:
                    continue
                extra = sorted(values - enums[wire]) if wire in enums else []
                if extra:
                    findings.append(
                        f"{op}.{arg} -> {call.method} {call.path} ?{wire}: Literal "
                        f"values {extra} are not in the spec enum {sorted(enums[wire])}"
                    )
    assert not findings, (
        f"{len(findings)} Literal(s) advertise values the spec enum lacks; Jira "
        "silently substitutes its default for these:\n"
        + "\n".join(f"  {f}" for f in findings)
    )


def _body_findings(
    where: str, call: _WireCall, allowed: dict[str, frozenset[str] | None]
) -> list[str]:
    findings: list[str] = []
    for kind, sent in (("json", call.json_body), ("form", call.form_body)):
        # A None on either side is a payload with no names to compare: ours
        # caller-supplied, the spec's an unnamed binary or scalar body.
        accepted = allowed.get(kind, frozenset())
        if sent is None or accepted is None:
            continue
        bad = sorted(sent - accepted)
        if bad:
            findings.append(
                f"{where}: {kind} body fields {bad} are not in the spec; "
                f"it accepts {sorted(accepted)}"
            )
    return findings


def test_wire_calls_match_the_spec(spec: _Spec) -> None:
    findings: list[str] = []
    for op, calls in sorted(_extract_ops().analyzed.items()):
        for call in calls:
            where = f"{op}: {call.method} {call.path}"
            matches = spec.candidates(call.path, call.method)
            if _is_spec_gap(op, call):
                if matches:
                    findings.append(
                        f"{where}: endpoint is back in the spec - drop it from SPEC_GAPS"
                    )
                continue
            if not matches:
                findings.append(
                    f"{where}: no such endpoint in the spec; it has "
                    f"{spec.describe_pool(call.path)}"
                )
                continue
            if len(matches) > 1:
                findings.append(f"{where}: ambiguous, matches spec paths {matches}")
                continue
            allowed_query = spec.endpoints[matches[0], call.method]
            bad_query = sorted(call.query - allowed_query)
            if bad_query:
                findings.append(
                    f"{where}: query params {bad_query} are not in the spec; "
                    f"it accepts {sorted(allowed_query)}"
                )
            findings += _body_findings(where, call, spec.bodies[matches[0], call.method])
    assert not findings, (
        f"{len(findings)} call(s) disagree with the Jira spec. Jira drops "
        "unknown names silently, so each of these is a request that quietly "
        "does not do what it says:\n" + "\n".join(f"  {f}" for f in findings)
    )
