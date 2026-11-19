"""GitHub private Git database store. No checkout, local data files or raw logs."""

import base64
import hashlib
import json
import re

import requests


class PrivateStoreError(RuntimeError):
    """Only fixed codes, never response messages or exceptions."""


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def blob_sha(content):
    return hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()


def valid_data_path(path):
    return bool(re.fullmatch(
        r"(?:config/installation\.json|state/collection\.json|parameters/latest\.json|"
        r"originals/[a-f0-9]{64}\.json|(?:daily|intraday|consumption|evening)/\d{4}-\d{2}-\d{2}\.json)", path))


def valid_workbook_path(path):
    return bool(re.fullmatch(r"reports/(?:closed|evening)/\d{4}-\d{2}-\d{2}\.xlsx", path))


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

    def _request(self, method, path, payload=None):
        if self.calls >= 128:
            raise PrivateStoreError("GITHUB_REQUEST_BUDGET")
        self.calls += 1
        try:
            with self._session.request(method, self._url + path, json=payload,
                    timeout=(5, 30), verify=True, allow_redirects=False, stream=True) as response:
                if response.status_code not in {200, 201}:
                    raise PrivateStoreError("PRIVATE_STORE_REQUEST_FAILED")
                content = bytearray()
                for chunk in response.iter_content(8192):
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise PrivateStoreError("PRIVATE_STORE_RESPONSE_LIMIT")
                result = json.loads(content)
                if type(result) is not dict:
                    raise PrivateStoreError("PRIVATE_STORE_INVALID_RESPONSE")
                return result
        except Exception:
            raise PrivateStoreError("PRIVATE_STORE_REQUEST_FAILED") from None

    def read(self, path):
        if not valid_data_path(path):
            raise PrivateStoreError("PRIVATE_PATH_INVALID")
        if path in self._cache:
            return self._cache[path]
        entry = self.entries.get(path)
        if entry is None:
            return None
        if entry.get("type") != "blob" or entry.get("mode") != "100644":
            raise PrivateStoreError("PRIVATE_FILE_INVALID")
        try:
            blob = self._request("GET", "/git/blobs/" + entry["sha"])
            if blob["encoding"] != "base64":
                raise ValueError()
            result = json.loads(base64.b64decode(blob["content"], validate=False))
            if type(result) is not dict:
                raise ValueError()
            self._cache[path] = result
            return result
        except Exception:
            raise PrivateStoreError("PRIVATE_FILE_INVALID") from None

    def publish(self, changes):
        pending = []
        for path, value in sorted(changes.items()):
            if valid_workbook_path(path) and isinstance(value, bytes):
                content = value
            elif valid_data_path(path) and path != "config/installation.json":
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
