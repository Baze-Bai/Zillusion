"""Tool permission system — ported from Claude Code's 3-layer permission architecture.

Three-phase decision flow:
  Phase 1: Hooks (fast, local) — pre-configured rules
  Phase 2: Classifier (automated) — ML-based auto-approval
  Phase 3: Interactive dialog — user confirmation

Permission modes:
  - default: prompt user for approval
  - auto: classifier-based auto-approval for safe operations
  - plan: read-only auto-approved, write needs approval
  - bypass: allow all (development only)

ResolveOnce guard prevents race conditions in concurrent permission requests.
"""

from __future__ import annotations

import enum
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PermissionMode(str, enum.Enum):
    DEFAULT = "default"
    AUTO = "auto"
    PLAN = "plan"
    BYPASS = "bypass"


class PermissionDecisionType(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass
class PermissionDecision:
    """Result of a permission check."""
    behavior: PermissionDecisionType
    updated_input: dict | None = None
    message: str = ""
    source: str = ""  # hook / classifier / user / rule
    reason: str = ""
    permanent: bool = False


@dataclass
class PermissionRule:
    """A single permission rule matching tool name + input pattern."""
    tool_pattern: str  # regex matching tool name
    input_pattern: str | None = None  # regex matching serialized input
    decision: PermissionDecisionType = PermissionDecisionType.ALLOW
    reason: str = ""

    def matches(self, tool_name: str, tool_input: dict) -> bool:
        if not re.match(self.tool_pattern, tool_name):
            return False
        if self.input_pattern:
            input_str = str(tool_input)
            if not re.search(self.input_pattern, input_str):
                return False
        return True


class ResolveOnce:
    """Atomic race-condition-safe resolution guard.

    Ported from Claude Code's createResolveOnce<T>.
    Ensures only one caller wins when multiple permission
    checks run concurrently (hooks, classifier, user dialog).
    """

    def __init__(self) -> None:
        self._resolved = False
        self._lock = threading.Lock()
        self._value: PermissionDecision | None = None

    def claim(self) -> bool:
        """Atomic CAS: return True if this caller won the race."""
        with self._lock:
            if self._resolved:
                return False
            self._resolved = True
            return True

    def resolve(self, value: PermissionDecision) -> bool:
        """Try to resolve with a value. Returns True if successful."""
        if self.claim():
            self._value = value
            return True
        return False

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    @property
    def value(self) -> PermissionDecision | None:
        return self._value


class ToolPermissionContext:
    """Immutable permission context for a single tool invocation.

    Ported from Claude Code's PermissionContext factory pattern.
    """

    def __init__(
        self,
        tool_name: str,
        tool_input: dict,
        mode: PermissionMode = PermissionMode.DEFAULT,
        allow_rules: list[PermissionRule] | None = None,
        deny_rules: list[PermissionRule] | None = None,
        hooks: list[Callable] | None = None,
    ) -> None:
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.mode = mode
        self._allow_rules = allow_rules or []
        self._deny_rules = deny_rules or []
        self._hooks = hooks or []
        self._resolve_once = ResolveOnce()

    def build_allow(
        self, updated_input: dict | None = None, reason: str = ""
    ) -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionDecisionType.ALLOW,
            updated_input=updated_input or self.tool_input,
            reason=reason,
        )

    def build_deny(self, message: str, reason: str = "") -> PermissionDecision:
        return PermissionDecision(
            behavior=PermissionDecisionType.DENY,
            message=message,
            reason=reason,
        )


class PermissionManager:
    """Central permission manager handling the 3-phase decision flow.

    Phase 1: Check rules (allow/deny lists)
    Phase 2: Run hooks (custom permission logic)
    Phase 3: Classifier or user dialog (depending on mode)
    """

    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT) -> None:
        self.mode = mode
        self._allow_rules: list[PermissionRule] = []
        self._deny_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []
        self._hooks: list[Callable] = []
        self._decision_log: list[dict] = []
        # Track recent denials to avoid infinite loops
        self._denial_tracker: dict[str, int] = {}

    def add_allow_rule(self, rule: PermissionRule) -> None:
        self._allow_rules.append(rule)

    def add_deny_rule(self, rule: PermissionRule) -> None:
        self._deny_rules.append(rule)

    def add_hook(self, hook: Callable) -> None:
        self._hooks.append(hook)

    async def check_permission(
        self,
        tool_name: str,
        tool_input: dict,
        user_callback: Callable | None = None,
    ) -> PermissionDecision:
        """Main permission check entry point.

        Three-phase decision flow:
        1. Rules (fast, deterministic)
        2. Hooks (extensible, custom logic)
        3. Mode-based fallback (auto/user/bypass)
        """
        start = time.monotonic()

        ctx = ToolPermissionContext(
            tool_name=tool_name,
            tool_input=tool_input,
            mode=self.mode,
            allow_rules=self._allow_rules,
            deny_rules=self._deny_rules,
            hooks=self._hooks,
        )

        # === Phase 0: Bypass mode ===
        if self.mode == PermissionMode.BYPASS:
            decision = ctx.build_allow(reason="bypass_mode")
            self._log_decision(tool_name, decision, "bypass")
            return decision

        # === Phase 1: Check deny rules first (highest priority) ===
        for rule in self._deny_rules:
            if rule.matches(tool_name, tool_input):
                decision = ctx.build_deny(
                    f"Denied by rule: {rule.reason}",
                    reason=f"deny_rule:{rule.reason}",
                )
                self._log_decision(tool_name, decision, "deny_rule")
                return decision

        # === Phase 1b: Check allow rules ===
        for rule in self._allow_rules:
            if rule.matches(tool_name, tool_input):
                decision = ctx.build_allow(reason=f"allow_rule:{rule.reason}")
                self._log_decision(tool_name, decision, "allow_rule")
                return decision

        # === Phase 2: Run hooks ===
        for hook in self._hooks:
            try:
                hook_result = await hook(tool_name, tool_input)
                if hook_result is not None:
                    self._log_decision(tool_name, hook_result, "hook")
                    return hook_result
            except Exception as e:
                logger.warning("Permission hook failed: %s", e)

        # === Phase 3: Mode-based fallback ===

        # Plan mode: read-only tools auto-approved
        if self.mode == PermissionMode.PLAN:
            from src.services.tool_executor import is_tool_concurrent_safe
            if is_tool_concurrent_safe(tool_name):
                decision = ctx.build_allow(reason="plan_mode_readonly")
                self._log_decision(tool_name, decision, "plan_mode")
                return decision

        # Auto mode: safe tools auto-approved via classifier
        if self.mode == PermissionMode.AUTO:
            if self._classify_safe(tool_name, tool_input):
                decision = ctx.build_allow(reason="auto_classifier")
                self._log_decision(tool_name, decision, "classifier")
                return decision

        # Default: require user approval (or auto-approve if no callback)
        if user_callback:
            decision = await user_callback(tool_name, tool_input)
            self._log_decision(tool_name, decision, "user")
            return decision

        # No user callback available (non-interactive mode): auto-approve
        decision = ctx.build_allow(reason="non_interactive_auto")
        self._log_decision(tool_name, decision, "auto_non_interactive")
        return decision

    def _classify_safe(self, tool_name: str, tool_input: dict) -> bool:
        """Simple classifier for auto-mode safety check.

        Returns True if the tool invocation is considered safe.
        Production: replace with ML classifier.
        """
        from src.services.tool_executor import CONCURRENT_SAFE_TOOLS
        # Read-only tools are always safe
        if tool_name in CONCURRENT_SAFE_TOOLS:
            return True

        # Check denial history (avoid loops)
        key = f"{tool_name}:{hash(str(tool_input))}"
        if self._denial_tracker.get(key, 0) >= 3:
            return False

        return False  # Default: not safe, need user approval

    def _log_decision(
        self, tool_name: str, decision: PermissionDecision, source: str
    ) -> None:
        """Log permission decisions for audit trail."""
        self._decision_log.append({
            "tool_name": tool_name,
            "decision": decision.behavior.value,
            "source": source,
            "reason": decision.reason,
            "timestamp": time.monotonic(),
        })
        logger.debug(
            "Permission: %s %s → %s (source=%s, reason=%s)",
            tool_name, decision.behavior.value, source, decision.reason,
        )

    def get_decision_log(self) -> list[dict]:
        return self._decision_log


# Singleton
permission_manager = PermissionManager()
