"""Deterministic triage-first chat routing for the gateway.

This module intentionally keeps the first implementation boring and testable:
keyword/regex classification, explicit board/category selection, and Kanban DB
writes through ``hermes_cli.kanban_db``.  The gateway calls it after slash and
quick-command bypasses have had their chance, but before starting an agent turn.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, cast

from agent.redact import redact_sensitive_text
from gateway.config import GatewayConfig
from gateway.platforms.base import MessageEvent
from hermes_cli import kanban_db

_ALLOWED_MODES = {"off", "normal", "triage_first", "force_triage", "inherit"}
_DEFAULT_COMMAND_PREFIXES = ["/"]
_DEFAULT_IMMEDIATE_KEYWORDS = ["now:", "execute:", "run:", "do now:"]
_DEFAULT_TRIAGE_KEYWORDS = [
    "queue",
    "track",
    "make a task",
    "add to kanban",
    "put on the board",
]
_GENERIC_PROJECTS = {"task", "todo", "stuff", "misc", "thing", "things", "work"}
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
_EXPLICIT_BOARD_RE = re.compile(r"\b(?:board|project)\s*:\s*([^:\n]+?)(?=\s*:\s*|$)", re.IGNORECASE)
_FOR_PROJECT_RE = re.compile(
    r"\bfor\s+([A-Z][A-Za-z0-9]*(?:[\s-]+[A-Z][A-Za-z0-9]*){0,5})(?=\s*:\s*|\s+-\s+|$)",
    re.IGNORECASE,
)
_TASK_INTENT_RE = re.compile(
    r"\b(?:fix|build|add|update|create|make|implement|investigate|triage|debug|prepare|write|track|queue)\b",
    re.IGNORECASE,
)
_URGENT_RE = re.compile(r"\b(?:urgent|asap|p0|critical|immediately)\b", re.IGNORECASE)
_LOW_PRIORITY_RE = re.compile(r"\b(?:someday|backlog|low priority|nice to have)\b", re.IGNORECASE)
_QUESTION_RE = re.compile(r"\?\s*$")
_CONVERSATION_RE = re.compile(
    r"^\s*(?:hi|hello|hey|thanks|thank you|what do you think|can you explain|how do i|why|what is)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TriageRoutingResult:
    created: bool
    task_id: str | None = None
    board_slug: str | None = None
    ack: str | None = None
    bypass_rule: str | None = None
    metadata: dict[str, Any] | None = None


def _triage_config(config: GatewayConfig | Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(config, Mapping):
        config_map = cast(Mapping[str, Any], config)
        gateway_map = config_map.get("gateway", {})
        raw = config_map.get("kanban_triage")
        if raw is None and isinstance(gateway_map, Mapping):
            raw = cast(Mapping[str, Any], gateway_map).get("kanban_triage")
        raw = raw or {}
    else:
        raw = getattr(config, "kanban_triage", {}) if config is not None else {}
    if not isinstance(raw, Mapping):
        raw = {}
    cfg = dict(raw)
    cfg.setdefault("enabled", False)
    cfg.setdefault("default_mode", "normal")
    cfg.setdefault("triage_assignee", "paul")
    cfg.setdefault("fallback_board", "inbox")
    cfg.setdefault("min_confidence", 0.70)
    cfg.setdefault("create_missing_boards", True)
    cfg.setdefault("board_create_policy", "explicit_project_only")
    cfg.setdefault("acknowledge", True)
    cfg.setdefault("ack_template", "Queued for triage: board={board_slug} task={task_id}")
    cfg.setdefault("immediate_execution_keywords", list(_DEFAULT_IMMEDIATE_KEYWORDS))
    cfg.setdefault("triage_keywords", list(_DEFAULT_TRIAGE_KEYWORDS))
    cfg.setdefault("command_prefixes", list(_DEFAULT_COMMAND_PREFIXES))
    cfg.setdefault("boards", {})
    cfg.setdefault("categories", {})
    cfg.setdefault("platforms", {})
    cfg.setdefault("users", {})
    mode = str(cfg.get("default_mode") or "normal").strip().lower()
    cfg["default_mode"] = mode if mode in _ALLOWED_MODES else "normal"
    return cfg


def _source_key(event: MessageEvent) -> tuple[str | None, str | None, str | None, str | None]:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    return (
        str(platform) if platform is not None else None,
        str(getattr(source, "chat_id", "") or "") or None,
        str(getattr(source, "thread_id", "") or "") or None,
        str(getattr(source, "user_id", "") or "") or None,
    )


def _resolve_mode(cfg: Mapping[str, Any], event: MessageEvent) -> str:
    platform, chat_id, thread_id, user_id = _source_key(event)
    mode = str(cfg.get("default_mode") or "normal").lower()

    platforms_raw = cfg.get("platforms")
    platforms = cast(Mapping[str, Any], platforms_raw) if isinstance(platforms_raw, Mapping) else {}
    platform_cfg = platforms.get(platform) if platform else None
    if isinstance(platform_cfg, Mapping):
        p_mode = str(platform_cfg.get("default_mode") or "inherit").lower()
        if p_mode != "inherit" and p_mode in _ALLOWED_MODES:
            mode = p_mode
        for key in ("chats", "channels"):
            scoped = platform_cfg.get(key)
            if isinstance(scoped, Mapping):
                for candidate in (chat_id, thread_id):
                    override = scoped.get(candidate) if candidate is not None else None
                    if isinstance(override, Mapping):
                        c_mode = str(override.get("mode") or "inherit").lower()
                        if c_mode != "inherit" and c_mode in _ALLOWED_MODES:
                            mode = c_mode

    users = cfg.get("users") if isinstance(cfg.get("users"), Mapping) else {}
    if platform and user_id and isinstance(users, Mapping):
        override = users.get(f"{platform}:{user_id}")
        if isinstance(override, Mapping):
            u_mode = str(override.get("mode") or "inherit").lower()
            if u_mode != "inherit" and u_mode in _ALLOWED_MODES:
                mode = u_mode
    return mode


def _normalize_new_board_slug(raw: str | None) -> str | None:
    if not raw:
        return None
    slug = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")[:64].strip("-")
    if not slug or slug in _GENERIC_PROJECTS:
        return None
    return slug


def _contains_any(text: str, needles: list[str]) -> str | None:
    lowered = text.lower()
    squashed = re.sub(r"\bthis\b\s*", "", lowered)
    for needle in needles:
        n = str(needle or "").strip().lower()
        if n and (n in lowered or n in squashed):
            return n
    return None


def _extract_explicit_project(text: str) -> tuple[str | None, str | None]:
    match = _EXPLICIT_BOARD_RE.search(text)
    if match:
        return match.group(1).strip(), "explicit_field"
    match = _FOR_PROJECT_RE.search(text)
    if match:
        candidate = match.group(1).strip()
        # Avoid treating generic lowercase phrases as projects.
        if _normalize_new_board_slug(candidate):
            return candidate, "for_project"
    return None, None


def _classify(text: str, cfg: Mapping[str, Any]) -> dict[str, Any]:
    stripped = text.strip()
    signals: list[str] = []
    triage_hit = _contains_any(stripped, list(cfg.get("triage_keywords") or []))
    immediate_hit = _contains_any(stripped, list(cfg.get("immediate_execution_keywords") or []))
    project_hint, project_source = _extract_explicit_project(stripped)

    if immediate_hit:
        return {
            "intent": "immediate_execution",
            "category": "general",
            "project_hint": project_hint,
            "confidence": 0.99,
            "signals": [f"immediate_keyword:{immediate_hit}"],
        }

    if triage_hit:
        signals.append(f"triage_keyword:{triage_hit}")
    if project_hint:
        signals.append(f"project_hint:{project_source}")
    if _TASK_INTENT_RE.search(stripped):
        signals.append("task_verb")

    boards_raw = cfg.get("boards")
    boards = cast(Mapping[str, Any], boards_raw) if isinstance(boards_raw, Mapping) else {}
    alias_category: str | None = None
    alias_project: str | None = None
    lowered = stripped.lower()
    for slug, info in boards.items():
        if not isinstance(info, Mapping):
            continue
        aliases = [str(slug)] + [str(alias) for alias in info.get("aliases", [])]
        for alias in aliases:
            if alias and re.search(rf"\b{re.escape(alias.lower())}\b", lowered):
                alias_project = str(slug)
                alias_category = str(info.get("default_category") or "general")
                signals.append(f"alias:{alias}")
                break
        if alias_project:
            break

    category = alias_category or "general"
    categories_raw = cfg.get("categories")
    categories = cast(Mapping[str, Any], categories_raw) if isinstance(categories_raw, Mapping) else {}
    if isinstance(categories, Mapping):
        for cat, info in categories.items():
            if isinstance(info, Mapping) and cat != "general" and re.search(rf"\b{re.escape(str(cat).lower())}\b", lowered):
                category = str(cat)
                signals.append(f"category:{cat}")
                break

    if alias_project and not project_hint:
        project_hint = alias_project

    if triage_hit:
        intent = "task_request"
        confidence = 0.91 if (project_hint or _TASK_INTENT_RE.search(stripped)) else 0.78
    elif project_hint and _TASK_INTENT_RE.search(stripped):
        intent = "task_request"
        confidence = 0.83
    elif _TASK_INTENT_RE.search(stripped) and not _QUESTION_RE.search(stripped):
        intent = "task_request"
        confidence = 0.74
    elif _CONVERSATION_RE.search(stripped) or _QUESTION_RE.search(stripped):
        intent = "conversation"
        confidence = 0.95
        signals.append("conversation_pattern")
    else:
        intent = "unknown"
        confidence = 0.35

    return {
        "intent": intent,
        "category": category,
        "project_hint": project_hint,
        "confidence": confidence,
        "signals": signals,
    }


def _should_create(mode: str, classification: Mapping[str, Any], cfg: Mapping[str, Any]) -> tuple[bool, str | None]:
    intent = str(classification.get("intent") or "unknown")
    if mode == "off":
        return False, "mode_off"
    if intent == "immediate_execution":
        return False, "immediate_execution"
    if intent == "conversation":
        return False, "conversation"
    if mode == "force_triage":
        return True, None
    confidence = float(classification.get("confidence") or 0.0)
    min_conf = float(cfg.get("min_confidence") or 0.70)
    explicit_triage = any(str(signal).startswith("triage_keyword:") for signal in classification.get("signals", []))
    if mode == "normal":
        return (True, None) if explicit_triage else (False, "normal_without_triage_keyword")
    if mode == "triage_first":
        if intent in {"task_request", "bug_report", "project_update", "content_request", "website_request", "research_request"} and confidence >= min_conf:
            return True, None
        return False, "low_confidence"
    return False, "mode_off"


def _select_board(text: str, classification: Mapping[str, Any], cfg: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    fallback = _normalize_new_board_slug(str(cfg.get("fallback_board") or "inbox")) or "inbox"
    project_hint = classification.get("project_hint")
    boards_raw = cfg.get("boards")
    boards = cast(Mapping[str, Any], boards_raw) if isinstance(boards_raw, Mapping) else {}
    categories_raw = cfg.get("categories")
    categories = cast(Mapping[str, Any], categories_raw) if isinstance(categories_raw, Mapping) else {}

    explicit_project, explicit_source = _extract_explicit_project(text)
    if explicit_project:
        selected = _normalize_new_board_slug(explicit_project)
        selected_source = "explicit_project"
        reason = f"explicit {explicit_source} '{explicit_project}'"
    else:
        selected = None
        selected_source = None
        reason = "no explicit project"

    lowered = text.lower()
    if not selected:
        for slug, info in boards.items():
            if not isinstance(info, Mapping):
                continue
            aliases = [str(slug)] + [str(alias) for alias in info.get("aliases", [])]
            for alias in aliases:
                if alias and re.search(rf"\b{re.escape(alias.lower())}\b", lowered):
                    selected = _normalize_new_board_slug(str(slug))
                    selected_source = "alias_match"
                    reason = f"matched configured alias '{alias}'"
                    break
            if selected:
                break

    if not selected:
        category = str(classification.get("category") or "general")
        cat_info = categories.get(category) if isinstance(categories, Mapping) else None
        if isinstance(cat_info, Mapping):
            selected = _normalize_new_board_slug(str(cat_info.get("board") or ""))
            if selected:
                selected_source = "category_default"
                reason = f"category default '{category}'"

    if not selected:
        selected = fallback
        selected_source = "fallback"
        reason = "fallback board"

    created_board = False
    fallback_used = False
    unresolved_hint = None
    exists = kanban_db.board_exists(selected)
    if not exists:
        create_missing = bool(cfg.get("create_missing_boards", True))
        policy = str(cfg.get("board_create_policy") or "explicit_project_only")
        confidence = float(classification.get("confidence") or 0.0)
        explicit = selected_source in {"explicit_project"}
        should_create = create_missing and policy != "never" and (
            policy == "always"
            or (policy == "explicit_project_only" and explicit)
            or (policy == "confident_project" and (explicit or confidence >= 0.85))
        )
        if should_create:
            kanban_db.create_board(selected)
            created_board = True
        else:
            unresolved_hint = selected
            selected = fallback
            fallback_used = True
            selected_source = "fallback"
            reason = "missing_or_uncertain_board"
            if not kanban_db.board_exists(selected):
                kanban_db.create_board(selected)
                created_board = selected != fallback

    return selected, {
        "board_slug": selected,
        "source": selected_source,
        "created_board": created_board,
        "fallback_used": fallback_used,
        "reason": reason,
        "unresolved_board_hint": unresolved_hint,
        "project_hint": project_hint,
    }


def _priority(text: str) -> int:
    if _URGENT_RE.search(text):
        return 10
    if _LOW_PRIORITY_RE.search(text):
        return -5
    return 0


def _title(text: str, classification: Mapping[str, Any]) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    cleaned = re.sub(r"^(?:add this to kanban|make a task|track this|queue this)(?:\s+for\s+[^:]+)?\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:board|project)\s*:\s*[^:]+\s*:\s*", "", cleaned, flags=re.IGNORECASE)
    first = re.split(r"(?<=[.!?])\s+", cleaned)[0].strip()
    title = first[:80].rstrip()
    category = str(classification.get("category") or "general")
    if category != "general" and not title.lower().startswith(f"[{category}]"):
        title = f"[{category}] {title}"
    return title or "Triage incoming chat request"


def _body(text: str, event: MessageEvent, classification: Mapping[str, Any], board_decision: Mapping[str, Any]) -> str:
    source = getattr(event, "source", None)
    safe_text = redact_sensitive_text(text.strip())
    links = _URL_RE.findall(safe_text)
    source_lines = []
    if source is not None:
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
        source_lines.append(f"- Platform: {platform or 'unknown'}")
        if getattr(source, "chat_name", None):
            source_lines.append(f"- Chat: {source.chat_name}")
        if getattr(source, "chat_type", None):
            source_lines.append(f"- Chat type: {source.chat_type}")
        if getattr(source, "thread_id", None):
            source_lines.append(f"- Thread: {source.thread_id}")
        if getattr(source, "user_name", None):
            source_lines.append(f"- User: {source.user_name}")
    source_text = "\n".join(source_lines) or "- Source: unknown"
    links_text = "\n".join(f"- {link}" for link in links) if links else "- None detected"
    return (
        "Triage this request, assign the right specialist, and refine acceptance criteria before implementation.\n\n"
        "## Original message\n"
        f"{safe_text}\n\n"
        "## Source\n"
        f"{source_text}\n\n"
        "## Extracted routing\n"
        f"- Intent: {classification.get('intent')}\n"
        f"- Category: {classification.get('category')}\n"
        f"- Project hint: {classification.get('project_hint') or 'none'}\n"
        f"- Confidence: {classification.get('confidence')}\n"
        f"- Board: {board_decision.get('board_slug')} ({board_decision.get('reason')})\n\n"
        "## Acceptance criteria\n"
        "- Clarify the desired outcome during triage.\n"
        "- Assign an implementation specialist only after acceptance criteria are explicit.\n\n"
        "## Links and attachments\n"
        f"{links_text}\n"
        f"- Attachment count: {len(getattr(event, 'media_urls', []) or [])}\n"
    )


def _metadata(event: MessageEvent, text: str, classification: Mapping[str, Any], board_decision: Mapping[str, Any], task_id: str, assignee: str | None, priority: int, ack: str | None) -> dict[str, Any]:
    source = getattr(event, "source", None)
    redacted = redact_sensitive_text(text)
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None)) if source is not None else None
    received_at = getattr(event, "timestamp", None)
    if isinstance(received_at, datetime):
        received = received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    else:
        received = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "chat_routing": {
            "routing_version": 1,
            "source": {
                "platform": platform,
                "chat_id": getattr(source, "chat_id", None) if source is not None else None,
                "channel_id": getattr(source, "parent_chat_id", None) if source is not None else None,
                "thread_id": getattr(source, "thread_id", None) if source is not None else None,
                "message_id": getattr(event, "message_id", None) or (getattr(source, "message_id", None) if source is not None else None),
                "user_id": getattr(source, "user_id", None) if source is not None else None,
                "user_display": getattr(source, "user_name", None) if source is not None else None,
                "received_at": received,
            },
            "message": {
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text_excerpt": redacted[:280],
                "attachment_count": len(getattr(event, "media_urls", []) or []),
                "link_urls": _URL_RE.findall(redacted),
            },
            "classification": dict(classification),
            "board_decision": dict(board_decision),
            "task_decision": {
                "task_id": task_id,
                "assignee": assignee,
                "priority": priority,
                "status": "triage",
            },
            "ack": {
                "sent": bool(ack),
                "format": ack,
                "reply_message_id": None,
            },
            "bypass": {"bypassed": False, "rule": None},
        }
    }


def _idempotency_key(event: MessageEvent, board_slug: str, text: str) -> str:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None)) if source is not None else "unknown"
    parts = [
        "gateway-chat-triage-v1",
        str(board_slug),
        str(platform or ""),
        str(getattr(source, "chat_id", "") if source is not None else ""),
        str(getattr(source, "thread_id", "") if source is not None else ""),
        str(getattr(event, "message_id", "") or (getattr(source, "message_id", "") if source is not None else "")),
    ]
    if not parts[-1]:
        parts.append(hashlib.sha256(text.encode("utf-8")).hexdigest())
    return ":".join(parts)


def route_chat_to_kanban_if_needed(event: MessageEvent, config: GatewayConfig | Mapping[str, Any] | None) -> TriageRoutingResult:
    """Route an inbound message into Kanban triage when configured to do so."""
    cfg = _triage_config(config)
    if not bool(cfg.get("enabled", False)):
        return TriageRoutingResult(created=False, bypass_rule="disabled")

    text = event.text or ""
    command_prefixes = [str(p) for p in (cfg.get("command_prefixes") or _DEFAULT_COMMAND_PREFIXES)]
    if any(text.startswith(prefix) for prefix in command_prefixes if prefix):
        return TriageRoutingResult(created=False, bypass_rule="command_prefix")

    meaningful_tokens = re.findall(r"[A-Za-z0-9]+", text)
    mode = _resolve_mode(cfg, event)
    classification = _classify(text, cfg)
    if len(meaningful_tokens) < 3 and mode != "force_triage":
        return TriageRoutingResult(created=False, bypass_rule="too_short", metadata={"classification": classification})

    should_create, bypass_rule = _should_create(mode, classification, cfg)
    if not should_create:
        return TriageRoutingResult(created=False, bypass_rule=bypass_rule, metadata={"classification": classification})

    board_slug, board_decision = _select_board(text, classification, cfg)
    category = str(classification.get("category") or "general")
    categories_raw = cfg.get("categories")
    categories = cast(Mapping[str, Any], categories_raw) if isinstance(categories_raw, Mapping) else {}
    assignee = str(cfg.get("triage_assignee") or "paul")
    if isinstance(categories, Mapping) and isinstance(categories.get(category), Mapping):
        assignee = str(categories[category].get("assignee") or assignee)
    priority = _priority(text)

    ack: str | None = None
    with kanban_db.connect(board=board_slug) as conn:
        task_id = kanban_db.create_task(
            conn,
            title=_title(text, classification),
            body=_body(text, event, classification, board_decision),
            assignee=assignee,
            created_by="gateway-chat-triage",
            priority=priority,
            triage=True,
            idempotency_key=_idempotency_key(event, board_slug, text),
        )
        if bool(cfg.get("acknowledge", True)):
            ack = str(cfg.get("ack_template") or "Queued for triage: board={board_slug} task={task_id}").format(
                board_slug=board_slug,
                task_id=task_id,
            )
            if board_decision.get("created_board"):
                ack += " (created new board)"
            elif board_decision.get("fallback_used") and board_decision.get("unresolved_board_hint"):
                ack += f" (could not confidently match {board_decision['unresolved_board_hint']})"
        metadata = _metadata(event, text, classification, board_decision, task_id, assignee, priority, ack)
        # Store structured routing metadata as a durable JSON comment. The core
        # kanban task schema intentionally has no arbitrary metadata column.
        existing_comments = kanban_db.list_comments(conn, task_id)
        marker = '"chat_routing"'
        if not any(marker in (comment.body or "") for comment in existing_comments):
            kanban_db.add_comment(conn, task_id, "gateway-chat-triage", json.dumps(metadata, sort_keys=True))

    return TriageRoutingResult(created=True, task_id=task_id, board_slug=board_slug, ack=ack, metadata=metadata)
