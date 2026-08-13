"""Small, review-scoped adapter for the remote GitHub MCP server.

The model sees five high-level read-only tools. Repository identity, pull request
number and head commit are supplied by the trusted review task, never by model
arguments.
"""
import asyncio
from dataclasses import dataclass
import json
import re
from typing import Any, Protocol, Tuple

from .runtime import AgentTool, ToolRegistry


class McpClient(Protocol):
    def call_tool(self, name: str, arguments: dict) -> Any:
        ...


class GitHubMcpToolError(RuntimeError):
    """The remote MCP server completed the call with a tool-level error."""


class GitHubMcpTransportError(RuntimeError):
    """The MCP connection failed before a usable tool result was received."""


class GitHubMcpClient:
    """Synchronous application adapter over the official async MCP SDK."""

    endpoint = "https://api.githubcopilot.com/mcp/"
    allowed_tools = (
        "get_file_contents", "search_code", "list_commits", "actions_list",
        "get_job_logs", "list_code_scanning_alerts",
    )

    def __init__(self, token: str, timeout_seconds: int = 20):
        self.token = token.strip()
        self.timeout_seconds = max(1, int(timeout_seconds))

    def call_tool(self, name: str, arguments: dict) -> Any:
        if not self.token:
            raise ValueError("GitHub token is required for remote MCP tools")
        if name not in self.allowed_tools:
            raise ValueError("GitHub MCP tool is not in the read-only allowlist")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("GitHub MCP tools must run outside an active asyncio loop")
        last_error = None
        for _attempt in range(2):
            try:
                return asyncio.run(self._call_tool(name, arguments))
            except GitHubMcpToolError:
                raise
            except Exception as exc:
                last_error = exc
        raise GitHubMcpTransportError(
            "GitHub MCP transport failed after one retry: %s" % last_error
        ) from last_error

    async def _call_tool(self, name: str, arguments: dict) -> Any:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        headers = {
            "Authorization": "Bearer %s" % self.token,
            "X-MCP-Tools": ",".join(self.allowed_tools),
        }
        async with httpx2.AsyncClient(
            headers=headers, timeout=self.timeout_seconds,
        ) as http_client:
            transport = streamable_http_client(
                self.endpoint, http_client=http_client,
            )
            async with Client(
                transport, read_timeout_seconds=self.timeout_seconds,
            ) as client:
                # MCP 2026 mirrors selected arguments into Mcp-Param-* headers.
                # The official SDK derives that map from tools/list.
                await client.list_tools()
                result = await client.call_tool(
                    name, dict(arguments),
                    read_timeout_seconds=self.timeout_seconds,
                )
        return self._normalize_result(name, result)

    @staticmethod
    def _normalize_result(name: str, result: Any) -> Any:
        text = "\n".join(
            str(block.text) for block in (getattr(result, "content", None) or [])
            if isinstance(getattr(block, "text", None), str)
        ).strip()
        if bool(getattr(result, "is_error", False)):
            raise GitHubMcpToolError(
                "GitHub MCP tool %s failed: %s" % (name, text or "unknown error")
            )
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            return structured
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


@dataclass(frozen=True)
class ReviewToolContext:
    repository: str
    head_commit: str
    pull_request: int | None
    files: Tuple[str, ...]
    domains: Tuple[str, ...]


class GitHubMcpToolProvider:
    """Build the least-privilege tool catalog for one reviewer assignment."""

    def __init__(self, client: McpClient):
        self.client = client

    def registry(self, context: ReviewToolContext) -> ToolRegistry:
        tools = [
            AgentTool(
                "read_code_context",
                "Read a small source window from an assigned file at the PR head commit.",
                self._schema({
                    "path": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                }, ("path", "line")),
                lambda path, line: self._read_code_context(context, path, line),
            ),
            AgentTool(
                "search_repository",
                "Search code in the current repository when the diff lacks context.",
                self._schema({
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                }, ("query",)),
                lambda query, path="": self._search_repository(context, query, path),
            ),
            AgentTool(
                "read_file_history",
                "Read recent commits for an assigned file to understand change intent.",
                self._schema({
                    "path": {"type": "string"},
                }, ("path",)),
                lambda path: self._read_file_history(context, path),
            ),
        ]
        domains = {item.lower() for item in context.domains}
        if "security" in domains:
            tools.append(AgentTool(
                "read_code_scanning_findings",
                "Read open code-scanning alerts for the PR head commit.",
                self._schema({
                    "severity": {"type": "string"},
                }),
                lambda severity="": self._read_code_scanning_findings(
                    context, severity
                ),
            ))
        else:
            tools.append(AgentTool(
                "read_ci_failure",
                "Read failed CI checks and their bounded failure logs for this PR head.",
                self._schema({
                    "check_name": {"type": "string"},
                }),
                lambda check_name="": self._read_ci_failure(context, check_name),
            ))
        return ToolRegistry(tools)

    @staticmethod
    def _schema(properties: dict, required: tuple = ()) -> dict:
        return {
            "type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False,
        }

    def _read_code_context(
        self, context: ReviewToolContext, path: str, line: int,
    ) -> Any:
        owner, repo = self._repository(context)
        path = self._assigned_path(context, path)
        result = self.client.call_tool("get_file_contents", {
            "owner": owner, "repo": repo, "path": path,
            "sha": self._head_commit(context),
        })
        content = self._file_content(result)
        lines = content.splitlines()
        center = int(line)
        if center > len(lines):
            raise ValueError("line is outside the file returned by GitHub")
        start = max(1, center - 20)
        end = min(len(lines), center + 20)
        numbered = "\n".join(
            "%d: %s" % (index, lines[index - 1])
            for index in range(start, end + 1)
        )
        return {
            "path": path, "start_line": start, "end_line": end,
            "content": numbered,
        }

    def _search_repository(
        self, context: ReviewToolContext, query: str, path: str,
    ) -> Any:
        owner, repo = self._repository(context)
        query = str(query).strip()
        if not query:
            raise ValueError("search query is required")
        if len(query) > 160:
            raise ValueError("search query is too long")
        if re.search(r"(?i)(?:^|\s)(?:repo|org|user|path):", query):
            raise ValueError("search scope qualifiers must use trusted tool fields")
        scoped = "%s repo:%s/%s" % (query, owner, repo)
        path = str(path).strip()
        if path:
            path = self._safe_path(path)
            scoped += " path:%s" % path
        return self.client.call_tool("search_code", {
            "query": scoped, "perPage": 10,
            "fields": ["path", "sha", "html_url"],
        })

    def _read_file_history(
        self, context: ReviewToolContext, path: str,
    ) -> Any:
        owner, repo = self._repository(context)
        path = self._assigned_path(context, path)
        return self.client.call_tool("list_commits", {
            "owner": owner, "repo": repo, "path": path,
            "sha": self._head_commit(context), "perPage": 5,
        })

    def _read_ci_failure(
        self, context: ReviewToolContext, check_name: str,
    ) -> Any:
        owner, repo = self._repository(context)
        runs_result = self.client.call_tool("actions_list", {
            "method": "list_workflow_runs", "owner": owner, "repo": repo,
            "per_page": 10,
            "workflow_runs_filter": {
                "head_sha": self._head_commit(context), "status": "failure",
            },
        })
        runs = self._items(runs_result, "workflow_runs")
        requested = str(check_name).strip().lower()
        selected = next((
            run for run in runs
            if not requested or requested in str(
                run.get("name") or run.get("display_title") or ""
            ).lower()
        ), None)
        if selected is None:
            return {"run": None, "logs": "No matching failed CI run at the PR head."}
        run_id = selected.get("id")
        if not isinstance(run_id, int):
            raise ValueError("GitHub returned a failed CI run without a numeric id")
        logs = self.client.call_tool("get_job_logs", {
            "owner": owner, "repo": repo, "run_id": run_id,
            "failed_only": True, "return_content": True, "tail_lines": 80,
        })
        return {
            "run": {
                key: selected[key] for key in (
                    "id", "name", "display_title", "conclusion", "html_url"
                ) if key in selected
            },
            "logs": logs,
        }

    def _read_code_scanning_findings(
        self, context: ReviewToolContext, severity: str,
    ) -> Any:
        owner, repo = self._repository(context)
        severity = str(severity).strip().lower()
        allowed = {"", "critical", "high", "medium", "low", "warning", "note", "error"}
        if severity not in allowed:
            raise ValueError("unsupported code-scanning severity")
        arguments = {
            "owner": owner, "repo": repo, "state": "open", "perPage": 10,
        }
        if severity:
            arguments["severity"] = severity
        result = self.client.call_tool("list_code_scanning_alerts", arguments)
        alerts = self._items(result, "alerts")
        head = self._head_commit(context)
        return {"alerts": [
            alert for alert in alerts
            if str(
                (alert.get("most_recent_instance") or {}).get("commit_sha")
                or alert.get("commit_sha") or ""
            ) == head
        ]}

    @staticmethod
    def _repository(context: ReviewToolContext) -> tuple[str, str]:
        parts = context.repository.strip().split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repository must be owner/repo")
        return parts[0], parts[1]

    @staticmethod
    def _head_commit(context: ReviewToolContext) -> str:
        value = context.head_commit.strip()
        if not value:
            raise ValueError("review head commit is required for GitHub tools")
        return value

    @staticmethod
    def _safe_path(path: str) -> str:
        value = str(path).strip().replace("\\", "/")
        if (
            not value or value.startswith("/") or value.endswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("path must be a safe repository-relative path")
        return value

    def _assigned_path(self, context: ReviewToolContext, path: str) -> str:
        value = self._safe_path(path)
        if value not in context.files:
            raise ValueError("path must be an assigned file for this reviewer")
        return value

    @staticmethod
    def _file_content(result: Any) -> str:
        if isinstance(result, str):
            return result
        if isinstance(result, dict) and isinstance(result.get("content"), str):
            return result["content"]
        raise ValueError("GitHub did not return text file content")

    @staticmethod
    def _items(result: Any, key: str) -> list[dict]:
        value = result if isinstance(result, list) else (
            result.get(key, []) if isinstance(result, dict) else []
        )
        return [item for item in value if isinstance(item, dict)]
