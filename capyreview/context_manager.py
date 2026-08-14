"""Token-aware deterministic context construction for PR review agents."""
from dataclasses import asdict, dataclass, field
import json
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple


HUNK_RANGE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class ContextBundle:
    text: str
    compressed: bool
    original_tokens: int
    final_tokens: int
    omitted_files: List[str] = field(default_factory=list)
    omitted_hunks: int = 0
    strategy: str = "full-diff"

    def metadata(self) -> Dict[str, Any]:
        value = asdict(self)
        value.pop("text", None)
        return value


@dataclass(frozen=True)
class ManagedContext:
    """The complete dynamic context presented to one agent-loop iteration."""

    text: str
    system_prompt: str
    estimated_tokens: int
    compressed: bool
    diff: Dict[str, Any]
    manifest: Dict[str, Any] = field(default_factory=dict)
    kept_feedback: int = 0
    kept_memories: int = 0
    kept_observations: int = 0
    dropped_feedback: int = 0
    dropped_memories: int = 0
    dropped_observations: int = 0

    def metadata(self) -> Dict[str, Any]:
        value = asdict(self)
        value.pop("text", None)
        value.pop("system_prompt", None)
        return value


@dataclass
class _Hunk:
    index: int
    path: str
    header: str
    text: str
    new_start: int = 0
    new_count: int = 0

    def contains_new_line(self, line: int) -> bool:
        return self.new_count > 0 and self.new_start <= line < self.new_start + self.new_count

    def changed_lines(self) -> int:
        return sum(
            1 for line in self.text.splitlines()
            if (line.startswith("+") and not line.startswith("+++"))
            or (line.startswith("-") and not line.startswith("---"))
        )

    def byte_cost(self) -> int:
        return len(self.text.encode("utf-8"))


class ContextManager:
    """Build a bounded LLM context while preserving changed-line evidence."""

    def __init__(self, max_tokens: int = 12000, reserved_tokens: int = 2500):
        if max_tokens < 512:
            raise ValueError("context max_tokens must be at least 512")
        if reserved_tokens < 0 or reserved_tokens >= max_tokens:
            raise ValueError("context reserved_tokens must be within the context budget")
        self.max_tokens = max_tokens
        self.reserved_tokens = reserved_tokens

    @staticmethod
    def estimate_tokens(text: str) -> int:
        # A conservative dependency-free estimate for mixed source code and text.
        return max(1, (len(text.encode("utf-8")) + 3) // 4)

    @staticmethod
    def _compact_assignment(assignment: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: assignment.get(key) for key in (
                "agent", "objective", "files", "risk_domains", "round", "reason"
            ) if assignment.get(key) not in (None, "", [])
        }

    @staticmethod
    def _render_line(label: str, value: Any) -> str:
        rendered = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return "%s: %s\n" % (label, rendered.replace("\x00", ""))

    def _contract_entries(
        self, assignment: Dict[str, Any], skills: Sequence[Dict[str, Any]],
        tools: Sequence[Dict[str, Any]],
    ) -> List[Tuple[str, Any, str]]:
        entries = [(
            "ASSIGNMENT", self._compact_assignment(assignment), "assignment"
        )]
        entries.extend((
            "SKILL", {
                "name": skill.get("name"),
                "version": skill.get("version"),
                "instructions": str(skill.get("body", ""))[:6000],
            }, "skills"
        ) for skill in skills)
        entries.extend((
            "TOOL", {
                "name": tool.get("name"),
                "description": str(tool.get("description", ""))[:240],
                "parameters": tool.get("parameters") or {},
            }, "tools"
        ) for tool in tools)
        return entries

    def contract_tokens(
        self, system_prompt: str, assignment: Dict[str, Any],
        skills: Sequence[Dict[str, Any]] = (),
        tools: Sequence[Dict[str, Any]] = (),
    ) -> int:
        """Estimate the immutable model contract before allocating Diff space."""
        rendered = system_prompt + "\n" + "".join(
            self._render_line(label, value)
            for label, value, _section in self._contract_entries(
                assignment, skills, tools
            )
        )
        return self.estimate_tokens(rendered)

    def build(
        self, diff: str, assignment: Dict[str, Any] = None,
        memories: Sequence[Dict[str, Any]] = (),
        fixed_tokens: int = 0,
    ) -> ContextBundle:
        original_tokens = self.estimate_tokens(diff)
        available_tokens = self.max_tokens - self.reserved_tokens - max(
            0, int(fixed_tokens)
        )
        if available_tokens < 32:
            raise ValueError("fixed context leaves no room for diff evidence")
        if original_tokens <= available_tokens:
            return ContextBundle(
                diff, False, original_tokens, original_tokens,
                strategy="full-diff",
            )

        assignment = assignment or {}
        # Memory is injected later by ``compose``. Historical hints must not decide
        # whether current PR code is visible to the reviewer.
        _ = memories
        file_headers, hunks = self._parse(diff)
        byte_budget = max(128, available_tokens * 4)
        selected: List[_Hunk] = []
        selected_ids = set()
        used = 0
        included_headers = set()

        focus_by_path: Dict[str, set] = {}
        for item in assignment.get("focus_lines") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", ""))
            try:
                line = int(item.get("line", 0))
            except (TypeError, ValueError):
                continue
            if path and line > 0:
                focus_by_path.setdefault(path, set()).add(line)

        p0 = [
            hunk for hunk in hunks
            if any(
                hunk.contains_new_line(line)
                for line in focus_by_path.get(hunk.path, set())
            )
        ]
        represented = {hunk.path for hunk in p0}
        remaining = [hunk for hunk in hunks if hunk.index not in {item.index for item in p0}]

        path_order = []
        for path in list(assignment.get("files") or []) + list(file_headers):
            if path in file_headers and path not in path_order:
                path_order.append(path)
        p1 = []
        for path in path_order:
            if path in represented:
                continue
            candidates = [hunk for hunk in remaining if hunk.path == path]
            if not candidates:
                continue
            representative = max(
                candidates,
                key=lambda item: (
                    item.changed_lines() / max(1, item.byte_cost()),
                    item.changed_lines(), -item.index,
                ),
            )
            p1.append(representative)
            represented.add(path)

        priority_ids = {item.index for item in p0 + p1}
        p2 = [hunk for hunk in hunks if hunk.index not in priority_ids]

        def add_hunk(hunk: _Hunk, allowance: int, focused: bool = False) -> bool:
            nonlocal used
            if hunk.index in selected_ids:
                return False
            file_header = file_headers.get(hunk.path, "")
            header_cost = (
                len(file_header.encode("utf-8"))
                if hunk.path not in included_headers else 0
            )
            remaining_bytes = byte_budget - used - header_cost
            content_budget = min(remaining_bytes, max(0, allowance - header_cost))
            if content_budget < 48:
                return False
            content = hunk.text
            if hunk.byte_cost() > content_budget:
                content = self._compact_hunk(
                    hunk, content_budget,
                    focus_by_path.get(hunk.path, set()) if focused else set(),
                )
            if not content:
                return False
            selected.append(_Hunk(
                hunk.index, hunk.path, hunk.header, content,
                hunk.new_start, hunk.new_count,
            ))
            selected_ids.add(hunk.index)
            used += header_cost + len(content.encode("utf-8"))
            included_headers.add(hunk.path)
            return True

        def add_tier(items: Sequence[_Hunk], focused: bool = False) -> None:
            for position, hunk in enumerate(items):
                available = byte_budget - used
                if available < 48:
                    break
                remaining_items = max(1, len(items) - position)
                allowance = max(128, available // remaining_items)
                add_hunk(hunk, allowance, focused)

        add_tier(p0, focused=True)
        add_tier(p1)
        for hunk in p2:
            if byte_budget - used < 48:
                break
            add_hunk(hunk, byte_budget - used)

        if not selected and hunks:
            first = (p0 or p1 or hunks)[0]
            selected = [_Hunk(
                first.index, first.path, first.header,
                self._compact_hunk(
                    first, byte_budget, focus_by_path.get(first.path, set())
                ) or first.text[:byte_budget],
                first.new_start, first.new_count,
            )]
            included_headers.add(first.path)

        pieces = []
        current_path = None
        for hunk in sorted(selected, key=lambda item: item.index):
            if hunk.path != current_path:
                pieces.append(file_headers.get(hunk.path, ""))
                current_path = hunk.path
            pieces.append(hunk.text)
        compressed = "".join(pieces).strip() + "\n"
        all_paths = list(file_headers)
        omitted_files = [path for path in all_paths if path not in included_headers]
        omitted_hunks = max(0, len(hunks) - len(selected))
        final_tokens = self.estimate_tokens(compressed)
        return ContextBundle(
            compressed, True, original_tokens, final_tokens,
            omitted_files=omitted_files, omitted_hunks=omitted_hunks,
            strategy="priority-tier-hunk-compression",
        )

    def compose(
        self, diff_bundle: ContextBundle, assignment: Dict[str, Any],
        feedback: Sequence[Any] = (), inbox: Sequence[Dict[str, Any]] = (),
        memories: Sequence[Dict[str, Any]] = (),
        observations: Sequence[Dict[str, Any]] = (),
        tools: Sequence[Dict[str, Any]] = (),
        skills: Sequence[Dict[str, Any]] = (),
        system_prompt: str = "",
    ) -> ManagedContext:
        """Fit all changing loop state into one deterministic token budget.

        The diff owns ``max_tokens - reserved_tokens``. Assignment, tool schemas,
        critique, recalled memories and tool observations share the reserved
        portion. Lower-priority records are dropped instead of silently growing
        the model request on every loop iteration.
        """
        optional_bytes = max(0, self.reserved_tokens * 4)
        parts: List[str] = []
        optional_used = 0
        was_truncated = False
        section_bytes = {
            "system": len(system_prompt.encode("utf-8")),
            "assignment": 0, "skills": 0, "tools": 0,
            "collaboration": 0, "observations": 0,
            "feedback": 0, "memories": 0,
            "diff": len(diff_bundle.text.encode("utf-8")),
        }

        def append(
            label: str, value: Any, section: str, required: bool = False,
        ) -> bool:
            nonlocal optional_used, was_truncated
            line = self._render_line(label, value)
            encoded = line.encode("utf-8")
            if required:
                parts.append(line)
                section_bytes[section] += len(encoded)
                return True
            remaining = optional_bytes - optional_used
            if len(encoded) <= remaining:
                parts.append(line)
                optional_used += len(encoded)
                section_bytes[section] += len(encoded)
                return True
            was_truncated = True
            return False

        contract_entries = self._contract_entries(assignment, skills, tools)
        kept_skills = 0
        kept_tools = 0
        for label, value, section in contract_entries:
            append(label, value, section, required=True)
            kept_skills += int(section == "skills")
            kept_tools += int(section == "tools")

        # Keep only mailbox routing metadata. Message bodies are represented by
        # critique and observations, avoiding duplicated prompt content.
        if inbox:
            append("COLLABORATION", {
                "count": len(inbox),
                "kinds": sorted({str(item.get("kind", "")) for item in inbox}),
                "senders": sorted({str(item.get("sender", "")) for item in inbox}),
            }, "collaboration")

        kept_observations = 0
        for item in reversed(observations):
            compact = {
                key: item.get(key)
                for key in ("id", "step", "tool", "ok", "result", "error")
                if item.get(key) is not None
            }
            if append("OBSERVATION", compact, "observations"):
                kept_observations += 1

        kept_feedback = 0
        for item in feedback:
            if append("CRITIC_FEEDBACK", str(item)[:1200], "feedback"):
                kept_feedback += 1

        kept_memories = 0
        for item in memories:
            compact = {
                "scope": item.get("scope"), "kind": item.get("kind"),
                "content": str(item.get("content", ""))[:1200],
                "score": item.get("recall_score"),
            }
            if append("MEMORY", compact, "memories"):
                kept_memories += 1

        runtime_text = "".join(parts)
        text = runtime_text + "DIFF_CONTEXT:\n" + diff_bundle.text
        # ``build`` receives the fixed system cost and ``compose`` owns the
        # reserved runtime portion. Never repair an overflow by clipping the
        # contract: callers must rebuild the diff with the correct fixed cost.
        maximum_bytes = self.max_tokens * 4
        encoded = (system_prompt + "\n" + text).encode("utf-8")
        if len(encoded) > maximum_bytes:
            raise ValueError(
                "managed model context exceeds the complete input budget; "
                "rebuild diff context with the fixed system cost"
            )
        final_tokens = self.estimate_tokens(system_prompt + "\n" + text)
        to_tokens = lambda value: (value + 3) // 4 if value else 0
        manifest = {
            "budget_tokens": self.max_tokens,
            "total_input_tokens": final_tokens,
            "sections": {
                key: to_tokens(value) for key, value in section_bytes.items()
            },
            "included": {
                "skills": kept_skills, "tools": kept_tools,
                "observations": kept_observations,
                "feedback": kept_feedback, "memories": kept_memories,
            },
            "dropped": {
                "skills": 0, "tools": 0,
                "observations": max(0, len(observations) - kept_observations),
                "feedback": max(0, len(feedback) - kept_feedback),
                "memories": max(0, len(memories) - kept_memories),
            },
        }
        return ManagedContext(
            text=text, system_prompt=system_prompt,
            estimated_tokens=final_tokens,
            compressed=bool(diff_bundle.compressed or was_truncated),
            diff=diff_bundle.metadata(), manifest=manifest,
            kept_feedback=kept_feedback, kept_memories=kept_memories,
            kept_observations=kept_observations,
            dropped_feedback=max(0, len(feedback) - kept_feedback),
            dropped_memories=max(0, len(memories) - kept_memories),
            dropped_observations=max(0, len(observations) - kept_observations),
        )

    @staticmethod
    def _truncate_utf8(value: bytes, limit: int) -> bytes:
        if limit <= 0:
            return b""
        clipped = value[:limit]
        while clipped:
            try:
                clipped.decode("utf-8")
                return clipped
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return b""

    def _parse(self, diff: str) -> Tuple[Dict[str, str], List[_Hunk]]:
        lines = diff.splitlines(True)
        files: List[Tuple[str, List[str]]] = []
        current: List[str] = []
        current_path = "unknown"
        for line in lines:
            if line.startswith("--- ") and current:
                files.append((current_path, current))
                current = []
                current_path = "unknown"
            current.append(line)
            if line.startswith("+++ "):
                raw = line[4:].strip()
                current_path = raw[2:] if raw.startswith("b/") else raw
        if current:
            files.append((current_path, current))

        headers: Dict[str, str] = {}
        hunks: List[_Hunk] = []
        index = 0
        for path, block in files:
            positions = [i for i, line in enumerate(block) if line.startswith("@@")]
            if not positions:
                headers[path] = "".join(block[:2])
                text = "".join(block)
                hunks.append(_Hunk(index, path, "", text))
                index += 1
                continue
            headers[path] = "".join(block[:positions[0]])
            for pos_index, start in enumerate(positions):
                end = positions[pos_index + 1] if pos_index + 1 < len(positions) else len(block)
                text = "".join(block[start:end])
                match = HUNK_RANGE.match(block[start])
                hunks.append(_Hunk(
                    index, path, block[start].rstrip(), text,
                    int(match.group(1)) if match else 0,
                    int(match.group(2) or 1) if match else 0,
                ))
                index += 1
        return headers, hunks

    @staticmethod
    def _compact_hunk(hunk: _Hunk, byte_budget: int, focus_lines: set) -> str:
        if byte_budget < 128:
            return ""
        lines = hunk.text.splitlines(True)
        if not lines:
            return ""
        added_indices = []
        focused_indices = []
        new_line = hunk.new_start
        for index, line in enumerate(lines):
            if index == 0 and line.startswith("@@"):
                continue
            added = line.startswith("+") and not line.startswith("+++")
            removed = line.startswith("-") and not line.startswith("---")
            if added:
                added_indices.append(index)
                if new_line in focus_lines:
                    focused_indices.append(index)
                new_line += 1
            elif not removed and not line.startswith("\\"):
                new_line += 1

        mandatory = [0]
        for index in focused_indices:
            mandatory.extend([
                index, max(0, index - 1), min(len(lines) - 1, index + 1)
            ])
        if not focused_indices and added_indices:
            mandatory.extend([added_indices[0], added_indices[-1]])

        selected = set()
        estimated = 0
        for index in mandatory:
            if index in selected:
                continue
            cost = len(lines[index].encode("utf-8"))
            if estimated + cost > byte_budget:
                continue
            selected.add(index)
            estimated += cost
        # Leave room for omission markers and the mandatory lines when results are
        # rendered back in source order. Optional additions may never crowd out a
        # Router focus line near the end of a large hunk.
        optional_limit = int(byte_budget * (0.75 if focused_indices else 0.9))
        for index in added_indices:
            if index in selected:
                continue
            cost = len(lines[index].encode("utf-8"))
            if estimated + cost > optional_limit:
                break
            selected.add(index)
            estimated += cost
        output = []
        previous = -2
        used = 0
        for index in sorted(selected):
            if index - previous > 1 and output:
                marker = " ... [unchanged context omitted by CapyReview] ...\n"
                marker_cost = len(marker.encode("utf-8"))
                line_cost = len(lines[index].encode("utf-8"))
                if used + marker_cost + line_cost <= byte_budget:
                    output.append(marker)
                    used += marker_cost
            encoded = lines[index].encode("utf-8")
            if used + len(encoded) > byte_budget:
                continue
            output.append(lines[index])
            used += len(encoded)
            previous = index
        return "".join(output)


def render_memories(memories: Iterable[Dict[str, Any]], max_chars: int = 5000) -> str:
    """Render recalled memory as untrusted, compact runtime context."""
    lines = []
    used = 0
    for item in memories:
        line = "[%s/%s] %s" % (
            item.get("scope", "memory"), item.get("kind", "note"),
            str(item.get("content", "")).replace("\n", " ")[:1000],
        )
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
