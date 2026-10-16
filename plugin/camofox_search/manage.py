"""Agent-facing tool: ``camofox-search-manage``.

CRUD over the plugin's SQLite rule store (:mod:`rules`):

- ``rule_type="search"``  — engine domain → SERP URL template
  (``{query}``/``{limit}``) + extraction JS (``@LIMIT@``). Drives ``web_search``
  engine selection and can add engines with no built-in support.
- ``rule_type="content"`` — page domain → extraction JS. Overrides the generic
  full-page text extraction in ``web_extract`` for that domain/subdomains.

Rule-authoring guidance ships as the bundled plugin skill
``web-camofox-search:camofox-search-rule`` (load it with ``skill_view`` before
writing rule JS).

Handler contract: returns a JSON *string* (the tool registry only accepts
strings or the multimodal envelope). Envelope:
``{"success": true, ...}`` or ``{"success": false, "error": "..."}``.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from . import rules

TOOL_NAME = "camofox-search-manage"

_RULE_HINT = ("Rule authoring guidance: load the skill "
              "'web-camofox-search:camofox-search-rule' (skill_view) first.")

MANAGE_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Create, update, delete, and inspect camofox-search rules (SQLite at "
        "HERMES_HOME/camofox-search/camofox-search.db, WAL). Search rules map a "
        "search-engine domain (e.g. 'bing.com') to a SERP URL template with "
        "{query}/{limit} placeholders plus page-context extraction JS returning "
        "JSON.stringify({ok:true,results:[{title,url,description}]}); they drive "
        "web_search engine selection and let you add engines with no built-in "
        "support (then add its id or domain to CAMOFOX_SEARCH_ENGINE). Content rules "
        "map a content-page domain to extraction JS returning "
        "JSON.stringify({ok:true,title,url,text}) or a plain string, overriding "
        "generic web_extract output for that domain and its subdomains. Actions: "
        "list | get | create | update | delete. "
        + _RULE_HINT
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "get", "create", "update", "delete"],
                "description": "Operation to run. 'update' merges only the fields you pass.",
            },
            "rule_type": {
                "type": "string",
                "enum": ["search", "content"],
                "description": "Which rule table to operate on.",
            },
            "domain": {
                "type": "string",
                "description": (
                    "Rule key: the search-engine domain (search rules) or content-page "
                    "domain (content rules). Accepts a bare domain, a hostname or a full "
                    "URL ('https://www.example.com/page' → 'example.com'). Required for "
                    "get/create/update/delete; omit for list."
                ),
            },
            "url_template": {
                "type": "string",
                "description": (
                    "Search rules only. SERP URL with {query} (URL-encoded query) and "
                    "optional {limit} (result count) placeholders, e.g. "
                    "'https://www.bing.com/search?q={query}&count={limit}'. Required on "
                    "create; pass to update to change it."
                ),
            },
            "extract_js": {
                "type": "string",
                "description": (
                    "Single JS expression evaluated in the page context by the Camofox "
                    "browser. Search rules: return "
                    "JSON.stringify({ok:true,results:[{title,url,description}]}); '@LIMIT@' "
                    "is replaced with the requested count. Content rules: return "
                    "JSON.stringify({ok:true,title,url,text}) or a plain string "
                    "(@LIMIT@ is not substituted). Required on create."
                ),
            },
            "notes": {
                "type": "string",
                "description": "Optional free-text note stored with the rule (why it exists).",
            },
        },
        "required": ["action", "rule_type"],
        "additionalProperties": False,
    },
}


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _err(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)


_STORE_API: dict[str, dict[str, Callable[..., Any]]] = {
    "search": {
        "list": rules.list_search_rules,
        "get": rules.get_search_rule,
        "create": rules.create_search_rule,
        "update": rules.update_search_rule,
        "delete": rules.delete_search_rule,
    },
    "content": {
        "list": rules.list_content_rules,
        "get": rules.get_content_rule,
        "create": rules.create_content_rule,
        "update": rules.update_content_rule,
        "delete": rules.delete_content_rule,
    },
}

_ACTIONS = ("list", "get", "create", "update", "delete")


def _string_arg(args: dict[str, Any], name: str) -> Optional[str]:
    if name not in args or args.get(name) is None:
        return None
    value = args.get(name)
    if not isinstance(value, str):
        raise rules.RuleValidationError(f"{name} must be a string")
    return value


def handle_manage(args: dict[str, Any], **_kwargs: Any) -> str:
    """Tool handler: dispatch one manage action; always returns a JSON string."""
    args = args if isinstance(args, dict) else {}
    try:
        return _ok(**_dispatch(args))
    except (rules.RuleValidationError, rules.RuleNotFoundError,
            rules.RuleExistsError, rules.RuleStoreError) as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001 - tool handlers must never raise
        return _err(f"{type(exc).__name__}: {exc}")


def _dispatch(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    rule_type = str(args.get("rule_type") or "").strip().lower()
    if action not in _ACTIONS:
        raise rules.RuleValidationError(
            f"invalid action {action!r}; expected one of: {', '.join(_ACTIONS)}")
    if rule_type not in _STORE_API:
        raise rules.RuleValidationError(
            f"invalid rule_type {rule_type!r}; expected 'search' or 'content'")

    api = _STORE_API[rule_type]
    domain = _string_arg(args, "domain")
    url_template = _string_arg(args, "url_template")
    extract_js = _string_arg(args, "extract_js")
    notes = _string_arg(args, "notes")

    if action == "list":
        items = api["list"]()
        return {"action": action, "rule_type": rule_type,
                "count": len(items), "rules": [item.to_dict() for item in items]}

    if not domain or not domain.strip():
        raise rules.RuleValidationError(f"domain is required for action='{action}'")

    if action == "get":
        rule = api["get"](domain)
        if rule is None:
            raise rules.RuleNotFoundError(
                f"no {rule_type} rule for {domain!r}")
        return {"action": action, "rule_type": rule_type, "rule": rule.to_dict()}

    if action == "delete":
        rule = api["delete"](domain)
        return {"action": action, "rule_type": rule_type,
                "deleted": True, "rule": rule.to_dict()}

    if action == "create":
        if rule_type == "search":
            if url_template is None:
                raise rules.RuleValidationError(
                    "url_template is required to create a search rule")
            rule = api["create"](domain, url_template, extract_js or "", notes or "")
        else:
            if url_template is not None:
                raise rules.RuleValidationError(
                    "url_template applies to search rules only")
            rule = api["create"](domain, extract_js or "", notes or "")
        return {"action": action, "rule_type": rule_type, "rule": rule.to_dict()}

    # update (merge semantics: only the provided fields change)
    if rule_type == "search":
        rule = api["update"](domain, url_template=url_template,
                             extract_js=extract_js, notes=notes)
    else:
        if url_template is not None:
            raise rules.RuleValidationError(
                "url_template applies to search rules only")
        rule = api["update"](domain, extract_js=extract_js, notes=notes)
    return {"action": action, "rule_type": rule_type, "rule": rule.to_dict()}
