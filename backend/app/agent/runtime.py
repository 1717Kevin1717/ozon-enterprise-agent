from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.agent.intent_engine import IntentPolicy


def normalized_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str))


@dataclass
class AgentRunState:
    policy: IntentPolicy
    cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    executions: list[dict[str, Any]] = field(default_factory=list)

    def _key(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return f"{tool_name}:{json.dumps(normalized_arguments(arguments), ensure_ascii=False, sort_keys=True)}"

    def record_rejection(self, tool_name: str, arguments: dict[str, Any], result: dict[str, Any], status: str) -> None:
        """Audit a rejected model request without counting it as an executed backend tool."""

        self.executions.append({
            "tool_name": tool_name,
            "normalized_args": normalized_arguments(arguments),
            "result": json.loads(json.dumps(result, ensure_ascii=False, default=str)),
            "status": status,
        })

    async def execute(self, tool_name: str, arguments: dict[str, Any], runner: Callable[[], Awaitable[dict[str, Any]]]) -> tuple[dict[str, Any], bool]:
        normalized = normalized_arguments(arguments)
        key = self._key(tool_name, normalized)
        if key in self.cache:
            return self.cache[key], True
        if tool_name not in self.policy.allowed_tools or tool_name in self.policy.forbidden_tools:
            result = {"tool": tool_name, "success": False, "error_code": "INTENT_TOOL_FORBIDDEN", "message": "当前业务意图不允许执行该工具。"}
            self.executions.append({"tool_name": tool_name, "normalized_args": normalized, "result": result, "status": "forbidden"})
            return result, False
        completed = sum(1 for item in self.executions if item["status"] == "success")
        if completed >= self.policy.max_tool_calls:
            result = {"tool": tool_name, "success": False, "error_code": "TOOL_BUDGET_EXCEEDED", "message": "本次任务已达到工具调用预算。"}
            self.executions.append({"tool_name": tool_name, "normalized_args": normalized, "result": result, "status": "budget_exceeded"})
            return result, False
        result = await runner()
        status = "success" if result.get("success") else "failed"
        audit_result = json.loads(json.dumps(result, ensure_ascii=False, default=str))
        self.executions.append({"tool_name": tool_name, "normalized_args": normalized, "result": audit_result, "status": status})
        if status == "success":
            self.cache[key] = result
        return result, False

    @property
    def actual_tool_calls(self) -> int:
        return sum(1 for item in self.executions if item["status"] in {"success", "failed"})

    @property
    def duplicate_tool_execution(self) -> int:
        keys = [self._key(item["tool_name"], item["normalized_args"]) for item in self.executions if item["status"] == "success"]
        return len(keys) - len(set(keys))
