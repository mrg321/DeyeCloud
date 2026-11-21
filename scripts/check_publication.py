"""Offline public-file and reachable Git-history check. Findings never echo values."""

import json
from pathlib import Path
import re
import subprocess


PUBLIC_FILES = frozenset({
    ".gitignore", "README.md", "requirements.txt", "requirements-actions.txt",
    ".vscode/launch.json", ".vscode/settings.json",
    ".github/workflows/deye-daily.yml", "config/deye_local.example.toml", "config/data.example.json",
    "docs/PUBLICATION.md", "docs/deye_local.md", "docs/automation.md",
    "scripts/setup_credentials.py", "scripts/deye_client.py", "scripts/deye_read_contracts.py",
    "scripts/deye_mcp_server.py", "scripts/deye_history_quality.py", "scripts/debug_deye_client.py",
    "scripts/audit_deye_readonly.py", "scripts/audit_deye_history.py", "scripts/audit_deye_history_followup.py",
    "scripts/report_deye_history_quality.py", "scripts/deye_private_store.py", "scripts/collect_deye_daily.py",
    "scripts/check_publication.py", "scripts/Invoke-DeyeDaily.ps1",
    "scripts/deye_workbook.py",
    "config/recommendation.example.json", "scripts/recommend_deye.py",
    "tests/test_recommend_deye.py", "docs/recommendation-readonly.md",
    "scripts/deye_tou.py", "tests/test_deye_tou.py",
    "config/deye-approval.workflow.example.json", "scripts/approve_deye.py",
    "tests/test_approve_deye.py", "docs/approval.md", "scripts/Invoke-DeyeWorkflowSimulation.ps1",
    "scripts/Install-DeyeApprovalWorkflow.ps1",
    "tests/test_deye_local.py", "tests/test_deye_audit.py",
    "tests/test_deye_history.py", "tests/test_daily_collection.py", "tests/test_publication.py",
})
PATTERNS = {
    "PRIVATE_KEY": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "GITHUB_TOKEN": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})",
    "JWT": r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}",
    "AWS_KEY": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "CREDENTIAL_URL": r"https?://[^\s/@:]+:[^\s/@]+@",
    "PERSONAL_PATH": r"(?i)[A-Z]:[\\/]+Users[\\/]+(?!Public\b)[^\\/\s`]+",
}


def content_findings(content, identifiers=()):
    findings = {category for category, pattern in PATTERNS.items() if re.search(pattern, content)}
    if any(value and value in content for value in identifiers):
        findings.add("INSTALLATION_IDENTIFIER")
    # Recognize hardcoded credential-shaped values, excluding explicit templates/test fixtures.
    for match in re.finditer(r'''(?i)["'](?:appSecret|password|accessToken|refreshToken)["']\s*:\s*["']([^"']+)["']''', content):
        value = match.group(1)
        if (not value.lower().startswith(("fake", "test", "example", "replace", "${{", "<"))
                and re.fullmatch(r"DEYE_[A-Z_]+", value) is None):
            findings.add("CREDENTIAL_LITERAL")
    return sorted(findings)


def private_identifiers(root):
    identifiers = set()
    def visit(value):
        if type(value) is dict:
            for key, item in value.items():
                if key in {"stationId", "deviceId", "parentId", "deviceSn", "id", "name"}:
                    if isinstance(item, (str, int)) and len(str(item)) >= 5:
                        identifiers.add(str(item))
                visit(item)
        elif type(value) is list:
            for item in value:
                visit(item)
    for path in (root / "docs").glob("deye-*.json"):
        visit(json.loads(path.read_text(encoding="utf-8")))
    return identifiers


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)


def audit(root):
    identifiers = private_identifiers(root)
    findings, checked = [], set()
    names = set(git(root, "ls-files", "-z").decode().strip("\0").split("\0"))
    names.update(git(root, "ls-files", "--others", "--exclude-standard", "-z").decode().strip("\0").split("\0"))
    for name in sorted(names - {""}):
        if name not in PUBLIC_FILES:
            findings.append({"scope": "tree", "code": "UNREVIEWED_PUBLIC_PATH"})
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            findings.append({"scope": "tree", "code": "INVALID_PUBLIC_FILE"})
            continue
        for code in content_findings(path.read_text(encoding="utf-8"), identifiers):
            findings.append({"scope": "tree", "file": name, "code": code})
    commits = git(root, "rev-list", "--all", "--reflog").decode().splitlines()
    for commit in set(commits):
        commit_text = git(root, "cat-file", "commit", commit).decode("utf-8", errors="replace")
        for identity in re.findall(r"^(?:author|committer) .* <([^>]+)>", commit_text, re.MULTILINE):
            if not identity.lower().endswith(("@users.noreply.github.com", "@example.invalid")):
                findings.append({"scope": "history", "commit": commit[:12], "code": "GIT_IDENTITY_REVIEW"})
        for code in content_findings(commit_text, identifiers):
            findings.append({"scope": "history", "commit": commit[:12], "code": code})
        entries = git(root, "ls-tree", "-r", "-z", commit).split(b"\0")
        for entry in filter(None, entries):
            metadata, raw_name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            name = raw_name.decode("utf-8", errors="replace")
            if name not in PUBLIC_FILES or kind != "blob" or mode != "100644":
                findings.append({"scope": "history", "commit": commit[:12], "code": "UNREVIEWED_HISTORICAL_PATH"})
            if kind != "blob" or oid in checked:
                continue
            checked.add(oid)
            text = git(root, "cat-file", "blob", oid).decode("utf-8", errors="replace")
            for code in content_findings(text, identifiers):
                findings.append({"scope": "history", "commit": commit[:12], "code": code})
    # Index is checked independently, so an old staged copy cannot escape a tree scan.
    for name in filter(None, git(root, "diff", "--cached", "--name-only", "-z").decode().split("\0")):
        if name not in PUBLIC_FILES:
            findings.append({"scope": "index", "code": "UNREVIEWED_STAGED_PATH"})
        else:
            for code in content_findings(git(root, "show", ":" + name).decode("utf-8"), identifiers):
                findings.append({"scope": "index", "file": name, "code": code})
    return {"commits_checked": len(set(commits)), "historical_blobs_checked": len(checked),
            "files_checked": len(names - {""}), "findings": findings,
            "scope": "local reachable refs and reflogs, index and nonignored worktree"}


def main():
    try:
        report = audit(Path(__file__).resolve().parents[1])
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return int(any(
            finding["code"] != "GIT_IDENTITY_REVIEW"
            for finding in report["findings"]
        ))
    except Exception:
        print("Publication check failed; sensitive details suppressed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
