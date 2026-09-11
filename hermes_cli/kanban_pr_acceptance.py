"""Exact-head GitHub acceptance for explicitly declared PR tasks.

Network work happens outside SQLite transactions. The lifecycle owner persists
receipts only after rechecking the captured run/status/contract under its lock.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from urllib.parse import quote

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)")


def validate_contract(value: str | None) -> str:
    if value is None or value == "local-only":
        return "local-only"
    if not isinstance(value, str) or not (_REPO.fullmatch(value) or _PR.fullmatch(value)):
        raise ValueError("completion_contract must be local-only, OWNER/REPO, or an exact GitHub PR URL")
    return value


def _api(endpoint: str, *, query: str | None = None, paginate: bool = False):
    command = ["gh", "api", endpoint, "--hostname", "github.com"]
    if query is not None:
        command += ["-f", "query=" + query]
    if paginate:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=True)
    value = json.loads(result.stdout)
    if isinstance(value, dict) and value.get("errors"):
        raise ValueError("GitHub returned incomplete GraphQL evidence")
    return value


def collect_acceptance(contract: str, published_pr: str | None) -> dict:
    receipt = {"ok": False, "classification": "missing", "head_sha": None,
               "pr_url": published_pr, "checks": [],
               "recovery": "Fix required failures, rerun infrastructure checks or wait, then retry completion. "
                           "Use kanban_block if human input is needed; receipts remain on the task event log."}
    try:
        declared = _PR.fullmatch(contract)
        url = contract if declared else published_pr
        match = _PR.fullmatch(url or "")
        if not match or (not declared and match[1] != contract) or (declared and published_pr and published_pr != contract):
            receipt["detail"] = "Supply metadata.published_pr matching the persisted completion contract."
            return receipt
        repo, number = match[1], int(match[2])
        receipt["pr_url"] = url
        owner, name = repo.split("/")
        query = '''{repository(owner:%s,name:%s){pullRequest(number:%d){headRefOid baseRefName state mergedAt mergeCommit{oid}
            baseRef{branchProtectionRule{requiredStatusChecks{context app{databaseId}}}}}}}''' % (
                json.dumps(owner), json.dumps(name), number)
        pr = _api("graphql", query=query)["data"]["repository"]["pullRequest"]
        sha, branch = pr["headRefOid"], pr["baseRefName"]
        receipt["head_sha"] = sha
        receipt["pr_state"] = pr["state"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha) or pr["state"] not in {"OPEN", "MERGED"}:
            raise ValueError("PR is closed or current head is unavailable")
        protection = (pr.get("baseRef") or {}).get("branchProtectionRule") or {}
        required = {(r["context"], (r.get("app") or {}).get("databaseId")) for r in protection.get("requiredStatusChecks", [])}
        sources = {requirement: {None} for requirement in required}
        rules = _api(f"repos/{repo}/rules/branches/{quote(branch, safe='')}?per_page=100", paginate=True)
        for page in rules:
            for rule in page:
                if rule["type"] == "required_status_checks":
                    for check in rule["parameters"]["required_status_checks"]:
                        requirement = (check["context"], check.get("integration_id"))
                        required.add(requirement)
                        sources.setdefault(requirement, set()).add(rule["ruleset_id"])
        receipt["required"] = [{"context": c, "app_id": a} for c, a in sorted(required, key=str)]
        if not required:
            receipt["detail"] = "No repository-required checks are configured; explicitly use a local-only contract for non-CI tasks."
            return receipt
        pages = _api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100&filter=latest", paginate=True)
        runs = [run for page in pages for run in page["check_runs"]]
        if len({r["id"] for r in runs}) != pages[0]["total_count"]:
            raise ValueError("Incomplete check-run pagination")
        statuses = [{**s, "sha": sha} for page in _api(f"repos/{repo}/commits/{sha}/statuses?per_page=100", paginate=True) for s in page]
        merged_proof = None
        outcomes = []
        for context, app_id in sorted(required, key=str):
            matching = [r for r in runs if r["name"] == context and
                        (app_id in (None, -1) or r["app"]["id"] == app_id)]
            legacy = [s for s in statuses if s["context"] == context]
            if legacy:
                legacy = [max(legacy, key=lambda s: s["id"])]
            # Legacy statuses omit the integration ID. For an already merged PR,
            # GitHub's enforcing rule evaluation can attest to that provenance.
            if app_id not in (None, -1):
                proof_sources = sources[(context, app_id)]
                eligible = (not matching and legacy and legacy[0]["state"] == "success"
                            and pr["state"] == "MERGED" and None not in proof_sources)
                if eligible and merged_proof is None:
                    merged_proof = _merged_status_proof(repo, branch, pr, sources)
                if eligible and proof_sources <= set((merged_proof or {}).get("rulesets", [])):
                    receipt["merged_rule_evidence"] = merged_proof
                else:
                    legacy = []
            selected = matching + ([max(legacy, key=lambda s: s["id"])] if legacy else [])
            if not selected:
                outcomes.append("missing")
                receipt["checks"].append({"name": context, "classification": "missing", "head_sha": sha})
            for check in selected:
                is_run = "conclusion" in check
                outcome = check.get("conclusion") if is_run else check["state"]
                classification = _classify(check, sha, outcome, is_run)
                outcomes.append(classification)
                receipt["checks"].append({"name": context, "id": check["id"],
                    "url": check.get("html_url") or check.get("target_url"),
                    "head_sha": check.get("head_sha", check.get("sha")),
                    "classification": classification, "conclusion": outcome})
        # Re-read after all pages: old-head successes are never transferable.
        current = _api(f"repos/{repo}/pulls/{number}")
        if (current["head"]["sha"] != sha or current["base"]["ref"] != branch
                or (current["state"] == "closed" and not current.get("merged"))
                or (merged_proof and (not current.get("merged")
                    or current.get("merge_commit_sha") != merged_proof["merge_sha"]))):
            receipt.update(classification="stale", detail="PR head/base changed while collecting evidence; retry.")
            return receipt
        receipt["classification"] = next((x for x in outcomes if x != "success"), "missing" if not outcomes else "success")
        receipt["ok"] = receipt["classification"] == "success"
        return receipt
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError):
        # Never persist gh stderr (credentials/host details); the failed phase is actionable.
        receipt.update(classification="infra", detail="GitHub acceptance evidence unavailable or incomplete; check gh authentication/API access and retry.")
        return receipt


def _merged_status_proof(repo: str, branch: str, pr: dict, sources: dict) -> dict:
    """Accept only non-bypassed merge evaluations of unchanged active rulesets."""
    merge_sha = (pr.get("mergeCommit") or {}).get("oid")
    merged_at = pr.get("mergedAt")
    if not merge_sha or not merged_at:
        return {}
    ref = f"refs/heads/{branch}"
    pages = _api(f"repos/{repo}/rulesets/rule-suites?ref={quote(ref, safe='')}"
                 "&time_period=month&rule_suite_result=pass&per_page=100", paginate=True)
    for page in pages:
        for candidate in page:
            if candidate.get("after_sha") != merge_sha or candidate.get("ref") != ref:
                continue
            suite = _api(f"repos/{repo}/rulesets/rule-suites/{candidate['id']}")
            if (suite.get("result") != "pass" or suite.get("after_sha") != merge_sha
                    or suite.get("ref") != ref or not suite.get("pushed_at")
                    or abs((datetime.fromisoformat(suite["pushed_at"])
                            - datetime.fromisoformat(merged_at)).total_seconds()) > 5):
                continue
            active = [r for r in suite.get("rule_evaluations", []) if r["enforcement"] == "active"]
            if not active or any(r["result"] != "pass" for r in active):
                continue
            passed = {r["rule_source"]["id"] for r in active
                      if r["rule_type"] == "required_status_checks" and r["rule_source"]["type"] == "ruleset"}
            verified = []
            for ruleset_id in sorted(passed & {s for values in sources.values() for s in values if s is not None}):
                policy = _api(f"repos/{repo}/rulesets/{ruleset_id}")
                if (policy["enforcement"] == "active"
                        and datetime.fromisoformat(policy["updated_at"]) <= datetime.fromisoformat(merged_at)):
                    verified.append(ruleset_id)
            return {"suite_id": suite["id"], "merge_sha": merge_sha,
                    "merged_at": merged_at, "ref": ref, "rulesets": verified}
    return {}


def _classify(check: dict, sha: str, outcome: str | None, is_run: bool) -> str:
    if check.get("head_sha", check.get("sha")) != sha:
        return "stale"
    if is_run and check.get("status") != "completed":
        return "pending"
    return {"success": "success", "failure": "failure", "error": "infra", "pending": "pending"}.get(outcome, "infra")
