"""Token-aware deterministic context construction for PR review agents."""
from dataclasses import asdict, dataclass, field
import json
import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple


HUNK_RANGE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


@dataclass(frozen=True)
class ContextBundle:
    text: str
    compressed: bool
    original_tokens: int
    final_tokens: int
    omitted_files: List[str] = field(default_factory=list)
    omitted_hunks: int = 0
    strategy: str = "full-diff"
    batch_index: int = 1
    batch_count: int = 1

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
    old_start: int = 0
    old_count: int = 0
    new_start: int = 0
    new_count: int = 0

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
                "description": str(skill.get("description", ""))[:1024],
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

    def runtime_tokens(
        self, feedback: Sequence[Any] = (),
        inbox: Sequence[Dict[str, Any]] = (),
        memories: Sequence[Dict[str, Any]] = (),
        observations: Sequence[Dict[str, Any]] = (),
    ) -> int:
        """Estimate the dynamic material already present for one loop call."""
        parts: List[str] = []
        if inbox:
            parts.append(self._render_line("COLLABORATION", {
                "count": len(inbox),
                "kinds": sorted({str(item.get("kind", "")) for item in inbox}),
                "senders": sorted({str(item.get("sender", "")) for item in inbox}),
            }))
        for item in reversed(observations):
            parts.append(self._render_line("OBSERVATION", {
                key: item.get(key)
                for key in ("id", "step", "tool", "ok", "result", "error")
                if item.get(key) is not None
            }))
        for item in feedback:
            parts.append(self._render_line(
                "CRITIC_FEEDBACK", str(item)[:1200]
            ))
        for item in memories:
            parts.append(self._render_line("MEMORY", {
                "scope": item.get("scope"), "kind": item.get("kind"),
                "content": str(item.get("content", ""))[:1200],
                "score": item.get("recall_score"),
            }))
        return self.estimate_tokens("".join(parts)) if parts else 0

    def build(
        self, diff: str, assignment: Dict[str, Any] = None,
        memories: Sequence[Dict[str, Any]] = (),
        fixed_tokens: int = 0,
    ) -> ContextBundle:
        # Compatibility wrapper for callers that do not yet consume a batch plan.
        # Multi-batch execution is handled by ``build_batches`` users.
        return self.build_batches(
            diff, assignment, memories, fixed_tokens=fixed_tokens
        )[0]

    def build_batches(
        self, diff: str, assignment: Dict[str, Any] = None,
        memories: Sequence[Dict[str, Any]] = (), fixed_tokens: int = 0,
    ) -> List[ContextBundle]:
        """Return full Diff, a compact change view, or bounded Hunk batches.

        The method does not rank code by risk. Compression is activated only when
        the complete Diff cannot fit after the caller's fixed context. It removes
        unchanged Unified Diff context while preserving every addition, deletion,
        file header and Hunk header. If that lossless change view still cannot fit,
        it is split in source order without dropping a Hunk.
        """
        del assignment, memories
        original_tokens = self.estimate_tokens(diff)
        available_tokens = self.max_tokens - max(0, int(fixed_tokens))
        if available_tokens < 32:
            raise ValueError("fixed context leaves no room for diff evidence")
        if original_tokens <= available_tokens:
            return [ContextBundle(
                diff, False, original_tokens, original_tokens,
                strategy="full-diff",
            )]

        file_headers, hunks = self._parse(diff)
        compact_hunks = [
            _Hunk(
                hunk.index, hunk.path, hunk.header,
                self._compact_change_view(hunk),
                hunk.old_start, hunk.old_count,
                hunk.new_start, hunk.new_count,
            )
            for hunk in hunks
        ]
        compact = self._render_hunks(file_headers, compact_hunks)
        compact_tokens = self.estimate_tokens(compact)
        if compact_tokens <= available_tokens:
            return [ContextBundle(
                compact, True, original_tokens, compact_tokens,
                strategy="compact-change-view",
            )]

        batches = self._batch_hunks(
            file_headers, compact_hunks, available_tokens * 4
        )
        count = len(batches)
        was_compacted = compact_tokens < original_tokens
        return [
            ContextBundle(
                text, was_compacted, original_tokens,
                self.estimate_tokens(text),
                strategy=(
                    "compact-hunk-batch" if was_compacted else "hunk-batch"
                ),
                batch_index=index, batch_count=count,
            )
            for index, text in enumerate(batches, 1)
        ]

    def compose(
        self, diff_bundle: ContextBundle, assignment: Dict[str, Any],
        feedback: Sequence[Any] = (), inbox: Sequence[Dict[str, Any]] = (),
        memories: Sequence[Dict[str, Any]] = (),
        observations: Sequence[Dict[str, Any]] = (),
        tools: Sequence[Dict[str, Any]] = (),
        skills: Sequence[Dict[str, Any]] = (),
        system_prompt: str = "",
    ) -> ManagedContext:
        """Assemble full loop material, reducing optional history only on overflow."""
        complete_tokens = (
            self.contract_tokens(system_prompt, assignment, skills, tools)
            + self.runtime_tokens(feedback, inbox, memories, observations)
            + self.estimate_tokens("DIFF_CONTEXT:\n" + diff_bundle.text)
        )
        complete_fits = complete_tokens <= self.max_tokens
        optional_bytes = (
            self.max_tokens * 4
            if complete_fits else max(0, self.reserved_tokens * 4)
        )
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
                    int(match.group(3)) if match else 0,
                    int(match.group(4) or 1) if match else 0,
                ))
                index += 1
        return headers, hunks

    @staticmethod
    def _compact_change_view(hunk: _Hunk) -> str:
        """Remove unchanged Hunk context without dropping change evidence."""
        lines = hunk.text.splitlines(True)
        if not lines:
            return ""
        output = [lines[0]] if lines[0].startswith("@@") else []
        body = lines[1:] if output else lines
        omitted = 0
        changed = False
        old_line = hunk.old_start
        new_line = hunk.new_start

        def flush_omitted() -> None:
            nonlocal omitted
            if omitted:
                output.append(
                    "... [%d unchanged lines omitted; next old=%d, new=%d] ...\n"
                    % (omitted, old_line, new_line)
                )
                omitted = 0

        for line in body:
            is_change = (
                (line.startswith("+") and not line.startswith("+++"))
                or (line.startswith("-") and not line.startswith("---"))
            )
            if is_change:
                flush_omitted()
                output.append(line)
                changed = True
                if line.startswith("+"):
                    new_line += 1
                else:
                    old_line += 1
            elif line.startswith("\\"):
                flush_omitted()
                output.append(line)
            else:
                omitted += 1
                old_line += 1
                new_line += 1
        flush_omitted()
        # Metadata-only changes (rename, mode, binary marker) have no Hunk changes.
        return "".join(output) if changed else hunk.text

    @staticmethod
    def _render_hunks(
        file_headers: Dict[str, str], hunks: Sequence[_Hunk],
    ) -> str:
        pieces: List[str] = []
        current_path = None
        for hunk in hunks:
            if hunk.path != current_path:
                pieces.append(file_headers.get(hunk.path, ""))
                current_path = hunk.path
            pieces.append(hunk.text)
        rendered = "".join(pieces)
        return rendered if not rendered or rendered.endswith("\n") else rendered + "\n"

    def _split_compact_hunk(
        self, hunk: _Hunk, content_budget: int,
    ) -> List[_Hunk]:
        """Split one oversized compact Hunk at line boundaries."""
        lines = hunk.text.splitlines(True)
        if not lines:
            return []
        header = lines[0] if lines[0].startswith("@@") else ""
        body = lines[1:] if header else lines
        header_cost = len(header.encode("utf-8"))
        if content_budget <= header_cost:
            raise ValueError("context budget cannot fit a Hunk header")
        parts: List[_Hunk] = []
        current: List[str] = []
        used = header_cost
        for line in body:
            cost = len(line.encode("utf-8"))
            if cost + header_cost > content_budget:
                raise ValueError(
                    "a single changed Diff line exceeds the context budget"
                )
            if current and used + cost > content_budget:
                text = header + "".join(current)
                parts.append(_Hunk(
                    hunk.index, hunk.path, hunk.header, text,
                    hunk.old_start, hunk.old_count,
                    hunk.new_start, hunk.new_count,
                ))
                current = []
                used = header_cost
            current.append(line)
            used += cost
        if current:
            parts.append(_Hunk(
                hunk.index, hunk.path, hunk.header,
                header + "".join(current),
                hunk.old_start, hunk.old_count,
                hunk.new_start, hunk.new_count,
            ))
        return parts

    def _batch_hunks(
        self, file_headers: Dict[str, str], hunks: Sequence[_Hunk],
        byte_budget: int,
    ) -> List[str]:
        expanded: List[_Hunk] = []
        for hunk in hunks:
            header_cost = len(file_headers.get(hunk.path, "").encode("utf-8"))
            if header_cost + hunk.byte_cost() <= byte_budget:
                expanded.append(hunk)
                continue
            expanded.extend(self._split_compact_hunk(
                hunk, byte_budget - header_cost
            ))

        batches: List[str] = []
        current: List[_Hunk] = []
        for hunk in expanded:
            candidate = current + [hunk]
            rendered = self._render_hunks(file_headers, candidate)
            if current and len(rendered.encode("utf-8")) > byte_budget:
                batches.append(self._render_hunks(file_headers, current))
                current = [hunk]
            else:
                current = candidate
        if current:
            batches.append(self._render_hunks(file_headers, current))
        if not batches:
            raise ValueError("Diff contains no reviewable content")
        if any(len(item.encode("utf-8")) > byte_budget for item in batches):
            raise ValueError("a compact Diff batch exceeds the context budget")
        return batches

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
