"""Small authoring helpers; generated manifests include all fixtures and oracles."""

from .schema import TOOLS


def call(name, **arguments):
    return {"name": name, "arguments": arguments}


def make_case(dimension, family, variant, prompt, fixture, calls, *, answer="done",
              expected=None, status="completed", edges=None, parallel=None,
              available=None, approved=None, writable=None, validation=False):
    calls = [{"id": f"c{i}", "ok": True, **value} for i, value in enumerate(calls)]
    return {
        "schema_version": 1, "id": f"{dimension}_{family}_{variant + 1:02d}",
        "dimension": dimension, "family": family, "variant": variant + 1,
        "prompt": prompt, "fixture": fixture, "expected_files": expected if expected is not None else fixture.copy(),
        "available_tools": list(available if available is not None else TOOLS),
        "approved_tools": approved if approved is not None else ["write_file", "edit_file"],
        "calls": calls, "edges": edges or [], "parallel_groups": parallel or [],
        "max_calls": len(calls), "expected_status": status, "answer": answer,
        "writable_paths": writable or [], "require_validation": validation,
    }
