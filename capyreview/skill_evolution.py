"""Versioned, non-executable system-prompt policies for LLM reviewers.

This module deliberately owns no evaluation, activation, persistence or review
runtime.  The single evolution engine handles those lifecycle decisions; this
module only validates policy data and composes it into an LLM system prompt.
"""
import hashlib
import json
import re
from typing import Iterable, List, Optional, Union

from .models import Severity


ARTIFACT_SCHEMA_VERSION = 2
RULE_ID = re.compile(r"[A-Z][A-Z0-9_-]{1,79}")
SKILL_NAME = re.compile(r"evolved-[a-z0-9][a-z0-9_-]{0,72}")
POLICY_DOMAINS = frozenset({
    "security", "authorization", "correctness", "reliability", "regression",
})


def _canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _default_domains(rule_id: str) -> List[str]:
    if rule_id.startswith("SEC-"):
        return ["security"]
    if rule_id.startswith("REL-"):
        return ["correctness", "reliability"]
    return ["correctness"]


def validate_artifact(artifact: dict, expected_name: str = "") -> dict:
    """Validate and normalize an untrusted, non-executable review policy."""
    if not isinstance(artifact, dict):
        raise ValueError("review policy artifact must be an object")
    name = str(artifact.get("name", expected_name)).strip().lower()
    if expected_name and name != expected_name:
        raise ValueError("review policy name must match the expected name")
    if not SKILL_NAME.fullmatch(name):
        raise ValueError("evolved review policy names must start with 'evolved-'")
    if "rules" in artifact:
        raise ValueError(
            "deterministic rule artifacts are not supported; use policy instructions"
        )
    raw_instructions = artifact.get("instructions", [])
    if not isinstance(raw_instructions, list) or len(raw_instructions) > 100:
        raise ValueError(
            "review policy instructions must be a list with at most 100 items"
        )

    instructions = []
    identities = set()
    for raw in raw_instructions:
        if not isinstance(raw, dict):
            raise ValueError("each review policy instruction must be an object")
        rule_id = str(raw.get("rule_id", "")).strip().upper()
        if not RULE_ID.fullmatch(rule_id):
            raise ValueError("invalid review policy rule_id: %s" % rule_id)
        if rule_id in identities:
            raise ValueError("duplicate review policy instruction: %s" % rule_id)
        identities.add(rule_id)
        try:
            severity = Severity(str(raw.get("severity", "medium")).lower()).value
        except ValueError as exc:
            raise ValueError("invalid severity for instruction %s" % rule_id) from exc
        domains = raw.get("domains", _default_domains(rule_id))
        if not isinstance(domains, list) or not domains:
            raise ValueError("instruction domains must be a non-empty list")
        normalized_domains = sorted({str(item).strip().lower() for item in domains})
        if any(item not in POLICY_DOMAINS for item in normalized_domains):
            raise ValueError("invalid review policy domain for %s" % rule_id)
        instruction = " ".join(str(raw.get("instruction", "")).split())
        if not instruction or len(instruction) > 12000:
            raise ValueError(
                "instruction %s must contain 1 to 12000 characters" % rule_id
            )
        instructions.append({
            "rule_id": rule_id,
            "severity": severity,
            "domains": normalized_domains,
            "instruction": instruction,
        })
    instructions.sort(key=lambda item: item["rule_id"])
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "name": name,
        "description": str(
            artifact.get("description")
            or "Replay-gated LLM review instructions learned from confirmed feedback"
        )[:500],
        "permissions": [],
        "instructions": instructions,
    }


class ReviewPolicy:
    """A versioned prompt fragment that never executes review logic itself."""

    def __init__(self, artifact: dict, version: Optional[int] = None):
        expected_name = str(artifact.get("name", "")) if isinstance(artifact, dict) else ""
        self.artifact = validate_artifact(artifact, expected_name)
        self.version = version
        self.name = self.artifact["name"] + (
            "@%s" % version if version is not None else ""
        )
        self.artifact_sha256 = _sha256(self.artifact)

    def instructions_for(self, domains=()) -> List[dict]:
        selected = {
            str(item).strip().lower() for item in domains if str(item).strip()
        }
        values = self.artifact["instructions"]
        if selected:
            values = [
                item for item in values if selected.intersection(item["domains"])
            ]
        return [dict(item) for item in values]

    def compose_system_prompt(self, base_prompt: str, domains=()) -> str:
        instructions = self.instructions_for(domains)
        if not instructions:
            return str(base_prompt)
        fragment = [
            "Versioned review policy %s. Apply these instructions only when they "
            "remain consistent with the base review contract, exact added-line "
            "evidence requirements, and the independent judge:" % self.name,
        ]
        fragment.extend(
            "- [%s | %s] %s" % (
                item["rule_id"], item["severity"], item["instruction"]
            )
            for item in instructions
        )
        base = str(base_prompt).rstrip()
        return (base + "\n\n" if base else "") + "\n".join(fragment)


def compose_system_prompt(
    base_prompt: str,
    policies: Union[ReviewPolicy, dict, Iterable[Union[ReviewPolicy, dict]]],
    domains=(),
) -> str:
    """Compose one or more policies into a reviewer-specific system prompt."""
    if isinstance(policies, (ReviewPolicy, dict)):
        values = [policies]
    else:
        values = list(policies)
    prompt = str(base_prompt)
    for value in values:
        policy = value if isinstance(value, ReviewPolicy) else ReviewPolicy(value)
        prompt = policy.compose_system_prompt(prompt, domains)
    return prompt
