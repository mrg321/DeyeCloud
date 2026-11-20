from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import approve_deye as approval
import recommend_deye as recommendation
from deye_private_store import ApprovalWorkflowMissing, GitHubPrivateStore


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)
DAY = "2026-11-21"
REVIEWERS = {123: "reviewer"}


class Store:
    def __init__(self):
        self.entries = {f"recommendations/{DAY}.json": {
            "date": DAY, "forecast": {"sunshine_hours": 3, "pv_kwh": 9},
            "recommendation": {"decision": "recommend", "confidence": "medium",
                               "current_targets": [40, 50], "recommended_soc": 60}}}
        self.issues = {}
        self.comment = None
        self.verify_private = Mock()
        self.publish = Mock(side_effect=self.entries.update)
        self._request = Mock(side_effect=self.request)

    def read(self, path):
        return self.entries.get(path)

    def request(self, method, path, data=None):
        if path.startswith("/issues?"):
            return list(self.issues.values())
        if path == "/users/reviewer":
            return {"login": "reviewer", "id": 123, "type": "User"}
        if (method, path) == ("POST", "/issues"):
            number = len(self.issues) + 1
            issue = {**data, "number": number, "state": "open",
                     "user": {"login": "github-actions[bot]", "type": "Bot"}, "assignees": [{"id": 123}]}
            self.issues[number] = issue
            return issue
        if path == "/issues/comments/42":
            return self.comment
        issue = self.issues[int(path.rsplit("/", 1)[1])]
        if method == "PATCH":
            issue.update(data)
        return issue

    def event(self, command="approve", actor_id=123):
        identity = self.entries[f"approvals/current/{DAY}.json"]["proposal_id"]
        number = self.entries[approval.proposal_path(DAY, identity)]["issue"]
        actor = {"id": actor_id, "login": "reviewer", "type": "User"}
        self.comment = {"id": 42, "user": actor, "body": f"/deye {command} {DAY} {identity}",
                        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
                        "issue_url": f"https://api.github.com/repos/test/DeyeCloud-data/issues/{number}"}
        return identity, {"action": "created", "sender": actor, "issue": {"number": number},
                          "comment": self.comment.copy()}


class ApprovalTests(unittest.TestCase):
    def test_dispatch_requires_private_workflow_file(self):
        store = object.__new__(GitHubPrivateStore)
        store.verify_private = Mock()
        store._request = Mock()
        store.entries = {}
        with self.assertRaises(ApprovalWorkflowMissing):
            store.dispatch_approval(DAY)
        store._request.assert_not_called()
        name = ".github/workflows/deye-approval.yml"
        store.entries[name] = {"type": "blob", "mode": "120000"}
        with self.assertRaises(ApprovalWorkflowMissing):
            store.dispatch_approval(DAY)
        store.entries[name]["mode"] = "100644"
        store.dispatch_approval(DAY)
        store.verify_private.assert_called_once()
        store._request.assert_called_once_with("POST", "/dispatches", {
            "event_type": "deye-recommendation", "client_payload": {"date": DAY}})

    def test_first_dispatch_installs_then_retries_with_data_token(self):
        installer, refreshed = Mock(), Mock()
        with patch.dict("os.environ", {"DEYE_APPROVAL_WORKFLOW_TOKEN": "installer",
                                      "DEYE_APPROVER_IDS": "[123]",
                                      "DEYE_APPROVER_LOGINS": '{"123":"reviewer"}'}, clear=True), \
                patch.object(recommendation, "GitHubPrivateStore", side_effect=[installer, refreshed]) as store:
            recommendation.dispatch_approval("test/DeyeCloud-data", "data", "test/DeyeCloud", DAY)
        self.assertEqual([call.args[1] for call in store.call_args_list], ["installer", "data"])
        installer.install_approval_workflow.assert_called_once_with(
            (ROOT / "config/deye-approval.workflow.example.json").read_bytes())
        installer.ensure_approval_variables.assert_called_once_with({
            "DEYE_APPROVER_IDS": "[123]", "DEYE_APPROVER_LOGINS": '{"123":"reviewer"}',
            "DEYE_APPROVAL_ENABLED": "true"})
        refreshed.dispatch_approval.assert_called_once_with(DAY)
        installer.close.assert_called_once()
        refreshed.close.assert_called_once()

    def test_dispatch_copies_explicit_cutoff(self):
        installer, refreshed = Mock(), Mock()
        with patch.dict("os.environ", {"DEYE_APPROVAL_WORKFLOW_TOKEN": "installer",
                                      "DEYE_APPROVER_IDS": "[123]", "DEYE_APPROVAL_CUTOFF": "22:30",
                                      "DEYE_APPROVER_LOGINS": '{"123":"reviewer"}'}, clear=True), \
                patch.object(recommendation, "GitHubPrivateStore", side_effect=[installer, refreshed]):
            recommendation.dispatch_approval("test/DeyeCloud-data", "data", "test/DeyeCloud", DAY)
        self.assertEqual(installer.ensure_approval_variables.call_args.args[0]["DEYE_APPROVAL_CUTOFF"], "22:30")

    def test_variable_sync_creates_only_missing_values(self):
        store = object.__new__(GitHubPrivateStore)
        store.verify_private = Mock()
        store._request = Mock(side_effect=[{"value": "[456]"}, None, {}])
        store.ensure_approval_variables({"DEYE_APPROVER_IDS": "[123]", "DEYE_APPROVAL_CUTOFF": "23:00"})
        self.assertEqual(store._request.call_args.args, ("POST", "/actions/variables",
                         {"name": "DEYE_APPROVAL_CUTOFF", "value": "23:00"}))
        store.verify_private.assert_called_once()

    def test_installer_creates_only_in_private_main_and_preserves_existing_file(self):
        store = object.__new__(GitHubPrivateStore)
        store.entries = {}
        store._request = Mock(return_value={"private": True, "visibility": "private", "default_branch": "main"})
        store.install_approval_workflow(b"{}")
        method, path, body = store._request.call_args.args
        self.assertEqual((method, path), ("PUT", "/contents/.github/workflows/deye-approval.yml"))
        self.assertEqual(body["branch"], "main")
        self.assertNotIn("sha", body)
        store._request.reset_mock()
        store.entries[".github/workflows/deye-approval.yml"] = {"type": "blob"}
        store.install_approval_workflow(b"{}")
        store._request.assert_not_called()

    def notify(self, store):
        return approval.notify(store, DAY, "23:00", NOW, REVIEWERS)

    def test_workflow_entry_point_from_dispatch_through_mobile_comment(self):
        store = Store()
        store.close = Mock()
        repository = {"full_name": "test/DeyeCloud-data", "private": True}
        with TemporaryDirectory() as directory:
            event_file = Path(directory) / "event.json"
            env = {"GITHUB_REPOSITORY": repository["full_name"], "GITHUB_REF": "refs/heads/main",
                   "GITHUB_EVENT_PATH": str(event_file), "GITHUB_EVENT_NAME": "repository_dispatch",
                   "GH_TOKEN": "fixture", "DEYE_APPROVAL_CODE_REPOSITORY": "test/DeyeCloud",
                   "DEYE_APPROVER_IDS": "[123]", "DEYE_APPROVER_LOGINS": '{"123":"reviewer"}'}
            with patch.dict("os.environ", env, clear=True), patch.object(approval, "datetime", wraps=datetime) as clock, \
                    patch.object(approval, "GitHubPrivateStore", return_value=store), patch("builtins.print"):
                clock.now.return_value = NOW
                event_file.write_text(json.dumps({"repository": repository, "action": "deye-recommendation",
                                                  "client_payload": {"date": DAY}}), encoding="utf-8")
                self.assertEqual(approval.main(), 0)
                identity, comment = store.event()
                event_file.write_text(json.dumps({**comment, "repository": repository}), encoding="utf-8")
                with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "issue_comment"}):
                    self.assertEqual(approval.main(), 0)
            self.assertEqual(store.entries[approval.decision_path(DAY, identity)]["status"], "approved")

    def test_decisions_are_audited_once_and_close_the_issue(self):
        for command, expected in approval.DECISIONS.items():
            with self.subTest(command=command):
                store = Store()
                self.assertEqual(self.notify(store), "NOTIFIED")
                self.assertEqual(self.notify(store), "ALREADY_NOTIFIED")
                identity, event = store.event(command)
                self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), expected.upper())
                path = approval.decision_path(DAY, identity)
                self.assertEqual(store.entries[path]["actor_id"], 123)
                self.assertEqual(store.entries[path]["status"], expected)
                self.assertEqual(store.issues[1]["state"], "closed")
                self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), "ALREADY_DECIDED")
                self.assertEqual(store.publish.call_count, 2)

    def test_unauthorized_and_edited_comments_never_record_approval(self):
        store = Store()
        self.notify(store)
        identity, event = store.event(actor_id=456)
        self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), "IGNORED")
        _, event = store.event()
        store.comment["updated_at"] = (NOW + timedelta(seconds=1)).isoformat()
        self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), "COMMENT_CHANGED")
        _, event = store.event()
        store.issues[1]["body"] += "\nEdit"
        self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), "ISSUE_CHANGED_OR_CLOSED")
        self.assertNotIn(approval.decision_path(DAY, identity), store.entries)

    def test_report_changes_and_new_proposal_invalidate_old_commands(self):
        store = Store()
        self.notify(store)
        identity, event = store.event()
        store.entries[f"recommendations/{DAY}.json"]["recommendation"]["recommended_soc"] = 70
        self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), "REPORT_CHANGED")
        self.assertEqual(self.notify(store), "NOTIFIED")
        self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), "SUPERSEDED")
        self.assertEqual(store.entries[approval.decision_path(DAY, identity)]["status"], "superseded")
        self.assertEqual(store.issues[1]["state"], "closed")

    def test_expiry_blocks_late_processing_and_reconciles_without_polling(self):
        for elapsed in (timedelta(hours=4), timedelta(days=1)):
            with self.subTest(elapsed=elapsed):
                store = Store()
                self.notify(store)
                identity, event = store.event()
                if elapsed.days:
                    self.assertTrue(approval.reconcile_expiry(store, NOW + elapsed))
                else:
                    self.assertEqual(approval.decide(store, event, REVIEWERS, NOW + elapsed), "EXPIRED")
                self.assertEqual(store.entries[approval.decision_path(DAY, identity)]["status"], "expired")
        self.assertEqual(approval.expiry("2026-03-30", "23:00").utcoffset(), timedelta(hours=2))
        with self.assertRaisesRegex(ValueError, "AMBIGUOUS"):
            approval.expiry("2026-10-26", "02:30")

    def test_retry_binds_existing_issue_after_interrupted_publication(self):
        store = Store()
        with patch.object(store, "publish", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                self.notify(store)
        self.assertEqual(len(store.issues), 1)
        self.assertEqual(self.notify(store), "NOTIFIED")
        self.assertEqual(len(store.issues), 1)

    def test_hold_creates_information_without_approval_commands(self):
        store = Store()
        store.entries[f"recommendations/{DAY}.json"]["recommendation"]["decision"] = "hold"
        self.notify(store)
        identity, event = store.event()
        self.assertNotIn("/deye approve", store.issues[1]["body"])
        self.assertEqual(approval.decide(store, event, REVIEWERS, NOW), "INVALID_PROPOSAL")
        self.assertEqual(store.entries[approval.decision_path(DAY, identity)]["status"], "no_change")

    def test_dry_run_cannot_dispatch_an_approval(self):
        with patch.dict("os.environ", {"DEYE_APPROVAL_ENABLED": "true", "DEYE_RECOMMENDATION_ENABLED": "true"},
                        clear=True), patch.object(recommendation, "GitHubPrivateStore") as store, \
                patch.object(recommendation, "run", return_value="DRY_RUN_COMPLETED"), patch("builtins.print"):
            self.assertEqual(recommendation.main(["--dry-run"]), 0)
        store.return_value.dispatch_approval.assert_not_called()

    def test_workflow_has_only_event_triggers_and_narrow_permissions(self):
        workflow = json.loads((ROOT / "config/deye-approval.workflow.example.json").read_text())
        self.assertEqual(workflow["on"], {"repository_dispatch": {"types": ["deye-recommendation"]},
                                          "issue_comment": {"types": ["created"]}})
        job = workflow["jobs"]["approval"]
        self.assertEqual(job["permissions"], {"contents": "write", "issues": "write"})
        self.assertLessEqual(job["timeout-minutes"], 3)
        self.assertIn("github.event.repository.private", job["if"])
        self.assertIn("DEYE_APPROVER_IDS", job["if"])
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"], {"repository": "mrg321/DeyeCloud",
                                            "ref": "refs/heads/main", "persist-credentials": False})
        self.assertNotIn("secrets.", json.dumps(workflow))
        self.assertNotIn("sleep", json.dumps(workflow))


if __name__ == "__main__":
    unittest.main()
