"""Four opt-in delegation lane tools, thinly routed over existing backends."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any, Dict, Mapping

from tools.delegate_lane_specs import (
    LANE_NAMES, LANE_TOOL_NAMES, RECEIPT_FIELDS,
    canonical_lane_name, lanes_configured, read_soul, resolve_lane_path,
    resolve_lane_spec,
)
from tools.delegate_tool_config import _cfg
from tools.delegate_tool_omp import is_quota_error, run_omp_lane
from tools.registry import no_cache_check_fn, registry


def _prompt(spec: Mapping[str, Any], task: str, context: str, *, with_soul: bool) -> str:
    sections = []
    if with_soul and spec.get("soul"):
        sections.append("# Lane instructions\n" + read_soul(spec["soul"]).strip())
    for skill_dir in spec.get("skills", []):
        directory = resolve_lane_path(skill_dir)
        skill_file = directory / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(f"lane skill file does not exist: {skill_file}")
        sections.append(f"# Injected skill: {skill_dir}\n" + skill_file.read_text(encoding="utf-8"))
    if context.strip():
        sections.append("# Additional context\n" + context.strip())
    sections.append("# Task\n" + task.strip())
    return "\n\n".join(sections)


def _run_omp(spec, task, workdir, model, timeout_seconds, _context):
    return run_omp_lane(
        spec, task, workdir, model, timeout_seconds=timeout_seconds,
        omp_config=_cfg().get("omp") or {},
    )


def _run_external(spec, task, workdir, model, timeout_seconds, context):
    from tools.delegate_tool import _delegate_agent_handler

    args = {
        "backend": spec["harness"], "goal": task, "workdir": workdir,
        "model": model, "timeout_seconds": timeout_seconds, "background": False,
    }
    if spec["harness"] == "claude" and spec.get("tools_allowlist"):
        args["allowed_tools"] = ",".join(spec["tools_allowlist"])
    forwarded = {key: context[key] for key in ("task_id", "session_id", "tool_call_id") if key in context}
    return _delegate_agent_handler(args, **forwarded)


def _run_subagent(spec, task, workdir, model, _timeout_seconds, context):
    from tools.delegate_tool import delegate_agent

    parent = context.get("parent_agent")
    if parent is None:
        from tools.agent_dispatch_context import get_agent_dispatch_context
        parent = get_agent_dispatch_context()
    return delegate_agent(
        goal=task,
        context=f"Run all workspace operations in this existing directory: {workdir}",
        background=False, parent_agent=parent,
        credentials_cfg={**_cfg(), "model": model},
    )


_RUNNERS = {
    "omp": _run_omp,
    "claude": _run_external,
    "cursor": _run_external,
    "subagent": _run_subagent,
}


def _payload(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "error": str(result)}
    return parsed if isinstance(parsed, dict) else {"success": False, "error": str(parsed)}


def _succeeded(payload: Mapping[str, Any]) -> bool:
    if "success" in payload:
        return payload.get("success") is True
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return False
    return all(isinstance(item, dict) and item.get("status") == "completed" for item in results)


def _report(payload: Mapping[str, Any]) -> str:
    for key in ("final_report", "summary", "result"):
        if isinstance(payload.get(key), str):
            return payload[key]
    results = payload.get("results")
    if isinstance(results, list):
        return "\n\n".join(
            str(item.get("summary") or "") for item in results
            if isinstance(item, dict) and item.get("summary")
        )
    return ""


def _error(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if error:
        return error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
    results = payload.get("results")
    if isinstance(results, list):
        failures = [
            str(item.get("error") or item.get("failure_reason") or item.get("summary") or "failed")
            for item in results if isinstance(item, dict) and item.get("status") != "completed"
        ]
        if failures:
            return "; ".join(failures)
    return "" if _succeeded(payload) else "lane backend did not report success"


def _one_line(value: Any) -> str:
    return " ".join(str(value or "unknown").split())[:240]


def _quota_failure(payload: Mapping[str, Any]) -> bool:
    return payload.get("quota") is True or is_quota_error(payload) or is_quota_error(_error(payload))


def _format_result(spec, payload, model, attempts):
    success = _succeeded(payload)
    error = None if success else _error(payload)
    verdict = "completed" if success else f"failed: {_one_line(error)}"
    values = {
        "lane": spec["name"], "harness": spec["harness"], "model": model,
        "reviewer": spec.get("reviewer") or "requesting agent", "verdict": verdict,
    }
    receipt = " | ".join(f"{field}: {_one_line(values[field])}" for field in RECEIPT_FIELDS)
    report = (_report(payload) or error or "No report returned").rstrip()
    final_report = f"{report}\n{receipt}"
    receipt_data = {
        field: values[field] for field in spec.get("receipt_fields", RECEIPT_FIELDS)
        if field in values
    }
    return json.dumps({
        "success": success, "error": error, "final_report": final_report,
        **values, "receipt": receipt, "receipt_data": receipt_data,
        "attempts": attempts, "backend_result": payload,
    }, ensure_ascii=False)


def _configuration_error(name: str, error: Exception) -> str:
    spec = {
        "name": canonical_lane_name(name), "harness": "unknown",
        "reviewer": "requesting agent", "receipt_fields": list(RECEIPT_FIELDS),
    }
    return _format_result(spec, {"success": False, "error": str(error)}, "none", [])


def delegate_lane(
    lane_name: str, task: str, workdir: str, *, context: str = "",
    timeout_seconds: int = 0, **dispatch_context,
) -> str:
    """Execute a lane's ordered model chain, advancing only on quota failures."""
    if not lanes_configured():
        return _configuration_error(
            lane_name, ValueError("delegation.lanes is empty; configure it to enable lane tools")
        )
    try:
        spec = resolve_lane_spec(lane_name)
    except (OSError, ValueError) as exc:
        return _configuration_error(lane_name, exc)
    if not task.strip() or not workdir.strip():
        return _format_result(
            spec, {"success": False, "error": "task and workdir are required"}, "none", []
        )
    if not Path(workdir).is_absolute() or not Path(workdir).is_dir():
        return _format_result(
            spec, {"success": False, "error": "workdir must be an existing absolute directory"},
            "none", [],
        )
    try:
        prompt = _prompt(spec, task, context, with_soul=spec["harness"] != "omp")
    except (OSError, ValueError) as exc:
        return _format_result(spec, {"success": False, "error": str(exc)}, "none", [])
    attempts = []
    payload: Dict[str, Any] = {"success": False, "error": "no model attempted"}
    selected = "none"
    for model in spec["models"]:
        selected = model
        try:
            payload = _payload(_RUNNERS[spec["harness"]](
                spec, prompt, workdir, model, timeout_seconds, dispatch_context
            ))
        except Exception as exc:
            payload = {"success": False, "error": str(exc)}
        error = _error(payload)
        attempts.append({
            "model": model, "success": _succeeded(payload),
            "error": error or None, "quota": not _succeeded(payload) and _quota_failure(payload),
        })
        if _succeeded(payload) or not _quota_failure(payload):
            break
    return _format_result(spec, payload, selected, attempts)


def _handle_registered_lane(lane_name, args, **kwargs):
    return delegate_lane(
        lane_name, str(args.get("task") or args.get("goal") or ""),
        str(args.get("workdir") or ""), context=str(args.get("context") or ""),
        timeout_seconds=args.get("timeout_seconds", 0), **kwargs,
    )


@no_cache_check_fn
def check_lane_requirements() -> bool:
    return lanes_configured()


_DESCRIPTIONS = {
    "ui_dev": "Delegate interface visuals, responsive layout, accessibility, and pixel-polish work.",
    "client_dev": "Delegate frontend state, hooks, interactions, and client-side data-flow work.",
    "backend_dev": "Delegate APIs, data layers, migrations, and backend contract work.",
    "reverse_engineer": "Delegate static-only binary, APK, and protocol analysis with evidence-backed findings.",
}


def _schema(lane_name: str) -> dict:
    return {
        "name": f"delegate_{lane_name}",
        "description": _DESCRIPTIONS[lane_name] + " Uses the configured model chain and returns a lane receipt.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Self-contained, verifiable task brief."},
                "workdir": {"type": "string", "description": "Existing absolute workspace directory."},
                "context": {"type": "string", "description": "Optional constraints, evidence, and background."},
                "timeout_seconds": {
                    "type": "integer", "default": 0,
                    "description": "Optional per-model-attempt wall-clock limit; 0 means no limit.",
                },
            },
            "required": ["task", "workdir"],
        },
    }


for _lane_name in LANE_NAMES:
    registry.register(
        name=f"delegate_{_lane_name}", toolset="delegation", schema=_schema(_lane_name),
        handler=partial(_handle_registered_lane, _lane_name), check_fn=check_lane_requirements,
        emoji="🔀", max_result_size_chars=100_000,
    )

__all__ = ["LANE_TOOL_NAMES", "check_lane_requirements", "delegate_lane"]
