"""Per-record structural validation (spec §5). Pure functions, no I/O.

``validate_record`` returns everything the file-level checks need (canonical
qid, normalized search rounds, final answer text) plus a list of issues. An
issue carries a *kind* and an example string; aggregation into counted
findings happens in :mod:`mast_validate.checks`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .languages import normalize

STEP_TYPES: frozenset[str] = frozenset({"reasoning", "tool_call", "output_text"})
REQUIRED_FIELDS: tuple[str, ...] = (
    "query_id", "language", "retriever", "llm", "tool_call_counts", "retrieved_docids", "result",
)
KNOWN_KEYS: frozenset[str] = frozenset(REQUIRED_FIELDS)
EXACT_ANSWER = "Exact Answer:"

FLAT_LIST_HINT = (
    "'retrieved_docids' is a flat list of docids; expected a list of search rounds, "
    "each a list of docid strings, e.g. [[\"81120\", \"10986\"], [\"9823\"]]"
)


@dataclass
class Issue:
    kind: str
    example: Optional[str] = None


@dataclass
class RecordResult:
    qid: Optional[str] = None
    reconstructed: bool = False
    rounds: Optional[list[list[str]]] = None
    search_count: Optional[int] = None
    final_output: Optional[str] = None
    has_output_text: bool = False
    unknown_keys: frozenset[str] = frozenset()
    issues: list[Issue] = field(default_factory=list)

    def kinds(self) -> set[str]:
        return {i.kind for i in self.issues}


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _short(v: Any, n: int = 40) -> str:
    s = repr(v)
    return s if len(s) <= n else s[: n - 3] + "..."


def parse_qid(value: Any, lang: str) -> tuple[Optional[str], Optional[Issue], bool]:
    """Resolve a submitted ``query_id`` against the declared language.

    Returns ``(canonical_qid, issue, reconstructed)``. Mode A: ``"zh-798"``
    (the prefix must be exactly the language code). Mode B: ``798`` or
    ``"798"``, rebuilt as ``f"{lang}-798"``. Anything else is malformed;
    nothing is guessed.
    """
    if _is_int(value):
        if value < 0:
            return None, Issue("qid.malformed", _short(value)), False
        return f"{lang}-{value}", None, True
    if not isinstance(value, str):
        return None, Issue("qid.malformed", _short(value)), False
    s = value.strip()
    if not s:
        return None, Issue("qid.malformed", "''"), False
    if s.isdigit():
        return f"{lang}-{int(s)}", None, True
    if "-" in s:
        prefix, _, num = s.rpartition("-")
        if not num.isdigit():
            return None, Issue("qid.malformed", _short(value)), False
        if prefix == lang:
            return f"{lang}-{int(num)}", None, False
        code = normalize(prefix)
        if code == lang:  # right language, wrong spelling: 'HI-798', 'hindi-798'
            return None, Issue("qid.malformed", f"{_short(value)} (prefix must be exactly '{lang}')"), False
        if code is None:
            return None, Issue("qid.malformed", _short(value)), False
        return None, Issue("qid.prefix_mismatch", f"{_short(value)} in a '{lang}' file"), False
    return None, Issue("qid.malformed", _short(value)), False


def validate_record(obj: Any, track: str, lang: str) -> RecordResult:  # noqa: C901 - one long, flat checklist
    rr = RecordResult()
    if not isinstance(obj, dict):
        rr.issues.append(Issue("json.not_object", _short(obj)))
        return rr

    rr.unknown_keys = frozenset(k for k in obj if k not in KNOWN_KEYS)
    missing = [f for f in REQUIRED_FIELDS if f not in obj]
    if missing:
        rr.issues.append(Issue("schema.missing_field", ", ".join(f"'{m}'" for m in missing)))
    wrong: list[str] = []

    # query_id
    if "query_id" in obj:
        qid, issue, recon = parse_qid(obj["query_id"], lang)
        rr.qid, rr.reconstructed = qid, recon
        if issue:
            rr.issues.append(issue)

    # language
    if "language" in obj:
        v = obj["language"]
        if not isinstance(v, str):
            wrong.append("'language' must be a string")
        else:
            code = normalize(v)
            if code != lang:
                why = "unrecognized" if code is None else f"= '{code}'"
                rr.issues.append(Issue("lang.mismatch", f"{_short(v)} ({why})"))

    # retriever / llm
    for key in ("retriever", "llm"):
        if key in obj:
            v = obj[key]
            if not isinstance(v, str) or not v.strip():
                wrong.append(f"'{key}' must be a non-empty string")

    # tool_call_counts
    if "tool_call_counts" in obj:
        v = obj["tool_call_counts"]
        if not isinstance(v, dict):
            wrong.append("'tool_call_counts' must be an object")
        else:
            bad = [k for k, c in v.items() if not isinstance(k, str) or not _is_int(c) or c < 0]
            if bad:
                wrong.append(f"'tool_call_counts' values must be non-negative integers ({_short(bad)})")
            elif _is_int(v.get("search")):
                rr.search_count = v["search"]

    # retrieved_docids
    if "retrieved_docids" in obj:
        v = obj["retrieved_docids"]
        if not isinstance(v, list):
            wrong.append("'retrieved_docids' must be a list of search rounds")
        elif any(isinstance(r, str) for r in v):
            wrong.append(FLAT_LIST_HINT)
        elif any(not isinstance(r, list) for r in v):
            wrong.append("'retrieved_docids' rounds must be lists of docid strings")
        else:
            rounds: list[list[str]] = []
            bad_entries: list[str] = []
            empty_rounds = 0
            for r in v:
                if not r:
                    empty_rounds += 1
                clean: list[str] = []
                for d in r:
                    if isinstance(d, str) and d.strip():
                        clean.append(d)
                    else:
                        bad_entries.append(_short(d))
                rounds.append(clean)
            rr.rounds = rounds
            if bad_entries:
                rr.issues.append(Issue("docid.bad_entry", ", ".join(bad_entries[:3])))
            if empty_rounds:
                rr.issues.append(Issue("rounds.empty", f"{empty_rounds} empty round(s)"))

    # result
    if "result" in obj:
        v = obj["result"]
        if not isinstance(v, list):
            wrong.append("'result' must be a list of steps")
        elif not v:
            rr.issues.append(Issue("schema.result_empty"))
        else:
            unknown_types: list[str] = []
            for i, step in enumerate(v):
                if not isinstance(step, dict):
                    wrong.append(f"result[{i}] is not an object")
                    continue
                t = step.get("type")
                if t not in STEP_TYPES:
                    unknown_types.append(_short(t))
                    continue
                for k in ("tool_name", "arguments"):
                    if k in step and step[k] is not None and not isinstance(step[k], str):
                        wrong.append(f"result[{i}].{k} must be a string or null")
                out = step.get("output")
                if t == "output_text":
                    if not isinstance(out, str):
                        wrong.append(f"result[{i}].output must be a string")
                    else:
                        rr.has_output_text = True
                        rr.final_output = out
                elif out is not None and not isinstance(out, str):
                    wrong.append(f"result[{i}].output must be a string or null")
            if unknown_types:
                rr.issues.append(Issue("schema.step_unknown_type", ", ".join(unknown_types[:3])))

    if wrong:
        rr.issues.append(Issue("schema.wrong_type", "; ".join(wrong[:3])))

    # cross-field warnings
    if rr.rounds is not None and rr.search_count is not None and len(rr.rounds) != rr.search_count:
        rr.issues.append(Issue("rounds.count_mismatch", f"{len(rr.rounds)} rounds vs search={rr.search_count}"))
    if "result" in obj and isinstance(obj["result"], list) and obj["result"]:
        if not rr.has_output_text:
            rr.issues.append(Issue("answer.no_output_text"))
        elif rr.final_output is not None and EXACT_ANSWER not in rr.final_output:
            rr.issues.append(Issue("answer.no_exact_answer", _short(rr.final_output[-60:], 64)))
    return rr
