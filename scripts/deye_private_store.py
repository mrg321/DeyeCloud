"""GitHub private Git database store. No checkout, local data files or raw logs."""

import base64
import hashlib
import json
import re

import requests


APPROVAL_WORKFLOW_PATH = ".github/workflows/deye-approval.yml"


class PrivateStoreError(RuntimeError):
    """Only fixed codes, never response messages or exceptions."""


class ApprovalWorkflowMissing(PrivateStoreError):
    """The private branch snapshot lacks an executable workflow file."""


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def blob_sha(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def valid_data_path(path):
    return bool(re.fullmatch(
        r"(?:config/(?:installation|recommendation|night_soc)\.json|state/collection\.json|parameters/latest\.json|"
        r"originals/[a-f0-9]{64}\.json|(?:daily|intraday|consumption|evening|recommendations|approvals/current)/\d{4}-\d{2}-\d{2}\.json|"
        r"approvals/(?:proposals|decisions)/\d{4}-\d{2}-\d{2}/[a-f0-9]{64}\.json)", path))


def valid_workbook_path(path):
    return bool(re.fullmatch(r"reports/(?:closed|evening|combined)/\d{4}-\d{2}-\d{2}\.xlsx", path))


class GitHubPrivateStore:
    """Publish one whole run using a non-forced Git ref update (fast-forward only)."""

    def __init__(self, repository, token, *, public_repository, session=None):
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+/DeyeCloud-data", repository or "")
                or repository.casefold() == (public_repository or "").casefold() or not token):
            raise PrivateStoreError("PRIVATE_DESTINATION_INVALID")
        self._url = "https://api.github.com/repos/" + repository
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._session.headers.update({"Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        self.calls = 0
        self._cache = {}
        try:
            metadata = self._request("GET", "")
            if metadata.get("private") is not True or metadata.get("visibility") != "private":
                raise PrivateStoreError("DESTINATION_NOT_PRIVATE")
            self.head = self._request("GET", "/git/ref/heads/main")["object"]["sha"]
            commit = self._request("GET", "/git/commits/" + self.head)
            self.tree = commit["tree"]["sha"]
            response = self._request("GET", "/git/trees/" + self.tree + "?recursive=1")
            if response.get("truncated"):
                raise PrivateStoreError("PRIVATE_TREE_TOO_LARGE")
            self.entries = {entry["path"]: entry for entry in response["tree"]}
        except Exception:
            self.close()
            raise PrivateStoreError("PRIVATE_STORE_INIT_FAILED") from None

    def close(self):
        self._session.headers.pop("Authorization", None)
        self._session.close()

    def _request(self, method, path, payload=None, *, missing_ok=False):
        if self.calls >= 128:
            raise PrivateStoreError("GITHUB_REQUEST_BUDGET")
        self.calls += 1
        try:
            base = "https://api.github.com" if method == "GET" and path.startswith("/users/") else self._url
            with self._session.request(method, base + path, json=payload,
                    timeout=(5, 30), verify=True, allow_redirects=False, stream=True) as response:
                if response.status_code == 204 and path == "/dispatches":
                    return {}
                if response.status_code == 201 and method == "POST" and path == "/actions/variables":
                    return {}
                if response.status_code == 404 and missing_ok and method == "GET":
                    return None
                if response.status_code not in {200, 201}:
                    raise PrivateStoreError("PRIVATE_STORE_REQUEST_FAILED")
                content = bytearray()
                for chunk in response.iter_content(8192):
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise PrivateStoreError("PRIVATE_STORE_RESPONSE_LIMIT")
                result = json.loads(content)
                if type(result) is not dict and not (path.startswith("/issues?") and type(result) is list):
                    raise PrivateStoreError("PRIVATE_STORE_INVALID_RESPONSE")
                return result
        except Exception:
            raise PrivateStoreError("PRIVATE_STORE_REQUEST_FAILED") from None

    def verify_private(self):
        metadata = self._request("GET", "")
        if metadata.get("private") is not True or metadata.get("visibility") != "private":
            raise PrivateStoreError("DESTINATION_NOT_PRIVATE")

    def dispatch_approval(self, day):
        workflow = self.entries.get(APPROVAL_WORKFLOW_PATH, {})
        if workflow.get("type") != "blob" or workflow.get("mode") != "100644":
            raise ApprovalWorkflowMissing("APPROVAL_WORKFLOW_MISSING")
        self.verify_private()
        self._request("POST", "/dispatches", {
            "event_type": "deye-recommendation", "client_payload": {"date": day}})

    def install_approval_workflow(self, template):
        if APPROVAL_WORKFLOW_PATH in self.entries:
            return
        metadata = self._request("GET", "")
        if (metadata.get("private") is not True or metadata.get("visibility") != "private"
                or metadata.get("default_branch") != "main"):
            raise PrivateStoreError("APPROVAL_WORKFLOW_DESTINATION_INVALID")
        # The Contents API requires a SHA to replace an existing file. A concurrent
        # installation therefore cannot be overwritten by this create request.
        self._request("PUT", "/contents/" + APPROVAL_WORKFLOW_PATH, {
            "branch": "main", "message": "Install private Deye approval workflow",
            "content": base64.b64encode(template).decode("ascii")})

    def ensure_approval_variables(self, values):
        for name, value in values.items():
            path = "/actions/variables/" + name
            if self._request("GET", path, missing_ok=True) is None:
                self.verify_private()
                self._request("POST", "/actions/variables", {"name": name, "value": value})

    def read(self, path):
        if not valid_data_path(path):
            raise PrivateStoreError("PRIVATE_PATH_INVALID")
        if path in self._cache:
            return self._cache[path]
        content = self._read_blob(path)
        if content is None:
            return None
        try:
            result = json.loads(content)
            if type(result) is not dict:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise PrivateStoreError("PRIVATE_FILE_INVALID") from None
        self._cache[path] = result
        return result

    def read_workbook(self, path):
        if not valid_workbook_path(path):
            raise PrivateStoreError("PRIVATE_PATH_INVALID")
        return self._read_blob(path)

    def _read_blob(self, path):
        entry = self.entries.get(path)
        if entry is None:
            return None
        if entry.get("type") != "blob" or entry.get("mode") != "100644":
            raise PrivateStoreError("PRIVATE_FILE_INVALID")
        try:
            blob = self._request("GET", "/git/blobs/" + entry["sha"])
            if blob["encoding"] != "base64":
                raise ValueError()
            return base64.b64decode(blob["content"], validate=False)
        except Exception:
            raise PrivateStoreError("PRIVATE_FILE_INVALID") from None

    def publish(self, changes):
        pending = []
        for path, value in sorted(changes.items()):
            if valid_workbook_path(path) and isinstance(value, bytes):
                content = value
            elif valid_data_path(path) and not path.startswith("config/"):
                content = canonical(value)
            else:
                raise PrivateStoreError("PRIVATE_PATH_INVALID")
            if self.entries.get(path, {}).get("sha") == blob_sha(content):
                continue
            pending.append((path, content))
        if not pending:
            return False
        # Visibility can have changed while collecting. Recheck before sending data.
        metadata = self._request("GET", "")
        if metadata.get("private") is not True or metadata.get("visibility") != "private":
            raise PrivateStoreError("DESTINATION_NOT_PRIVATE")
        updates = []
        for path, content in pending:
            blob = self._request("POST", "/git/blobs", {
                "content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
            updates.append({"path": path, "type": "blob", "mode": "100644", "sha": blob["sha"]})
        tree = self._request("POST", "/git/trees", {"base_tree": self.tree, "tree": updates})
        commit = self._request("POST", "/git/commits", {
            "message": "Update private Deye history", "tree": tree["sha"], "parents": [self.head]})
        metadata = self._request("GET", "")
        if metadata.get("private") is not True or metadata.get("visibility") != "private":
            raise PrivateStoreError("DESTINATION_NOT_PRIVATE")
        # No force, no rebase and no blind retry. Concurrent changes fail safely.
        self._request("PATCH", "/git/refs/heads/main", {"sha": commit["sha"], "force": False})
        return True
