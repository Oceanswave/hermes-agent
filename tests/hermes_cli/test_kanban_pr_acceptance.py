"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    "headRefOid": sha, "baseRefName": "main", "state": state.get("pr_state", "OPEN"),
                    "mergeCommit": {"oid": "c" * 40}, "mergedAt": "2026-09-11T20:55:18Z",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [] if state.get("legacy") else [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[{"type": "required_status_checks", "ruleset_id": 7, "parameters": {
                    "required_status_checks": [{"context": "required", "integration_id": 1}]}}]] if state.get("legacy") else [[]]
            elif "/rulesets/rule-suites/" in self.path:
                value = {"id": 8, "after_sha": "c" * 40, "ref": "refs/heads/main",
                         "pushed_at": "2026-09-11T14:55:18-06:00", "result": state.get("suite_result", "pass"),
                         "rule_evaluations": [{"rule_source": {"type": "ruleset", "id": state.get("source_id", 7)},
                             "enforcement": state.get("enforcement", "active"), "result": state.get("rule_result", "pass"),
                             "rule_type": "required_status_checks"}]}
            elif "/rulesets/rule-suites?" in self.path:
                value = [[{"id": 8, "after_sha": state.get("suite_sha", "c" * 40), "ref": "refs/heads/main"}]]
            elif "/rulesets/7" in self.path:
                value = {"id": 7, "enforcement": "active", "updated_at": state.get("policy_updated", "2026-09-01T00:00:00Z")}
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") or state.get("legacy") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[{"id": 99, "context": "required", "state": state["conclusion"], "creator": None,
                           "target_url": "https://example.com/deployment"}]] if state.get("legacy") else [[]]
            elif "/pulls/" in self.path:
                merged = state.get("pr_state") == "MERGED"
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "closed" if merged else "open",
                         "merged": merged, "merge_commit_sha": state.get("reread_merge_sha", "c" * 40)}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")


@pytest.mark.linux_only
def test_merged_legacy_status_requires_unchanged_enforcing_rule_proof(github):
    cases = [
        ({}, True),
        ({"pr_state": "OPEN"}, False),
        ({"suite_result": "bypass"}, False),
        ({"suite_result": "fail"}, False),
        ({"suite_sha": "d" * 40}, False),
        ({"source_id": 9}, False),
        ({"enforcement": "evaluate"}, False),
        ({"rule_result": "fail"}, False),
        ({"policy_updated": "2026-09-12T00:00:00Z"}, False),
        ({"conclusion": "failure"}, False),
        ({"conclusion": "pending"}, False),
        ({"reread_merge_sha": "d" * 40}, False),
    ]
    with connect() as conn:
        for overrides, expected in cases:
            github.update(legacy=True, pr_state="MERGED", conclusion="success", suite_result="pass",
                          suite_sha="c" * 40, source_id=7, enforcement="active", rule_result="pass",
                          policy_updated="2026-09-01T00:00:00Z", reread_merge_sha="c" * 40)
            github.update(overrides)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            assert kb.complete_task(conn, tid, metadata={"published_pr": "https://github.com/acme/repo/pull/7"}) is expected, overrides
            assert (kb.get_task(conn, tid).status == "done") is expected
            receipt = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)
            ).fetchone()[0])
            if expected:
                assert receipt["checks"][0]["id"] == 99
                assert receipt["merged_rule_evidence"]["merge_sha"] == "c" * 40
                assert receipt["merged_rule_evidence"]["rulesets"] == [7]
