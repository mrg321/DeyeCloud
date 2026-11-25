"""Refresh the private Actions configuration from the public workflow environment."""

import os
from pathlib import Path
import subprocess

from deye_private_store import GitHubPrivateStore, PrivateStoreError


SECRET_NAMES = ("DEYE_APP_ID", "DEYE_APP_SECRET", "DEYE_EMAIL", "DEYE_PASSWORD")


def sync(repository, public_repository, token, environment):
    approval = environment.get("DEYE_APPROVAL_ENABLED") == "true"
    execution = approval and environment.get("DEYE_TOU_WRITE_ENABLED") == "true"
    reviewers = {name: environment.get(name) for name in ("DEYE_APPROVER_IDS", "DEYE_APPROVER_LOGINS")}
    secrets = {name: environment.get(name) for name in SECRET_NAMES}
    if (not token or not all(value and value.strip() for value in secrets.values())
            or (approval and not all(reviewers.values()))):
        raise PrivateStoreError("SOURCE_ACTIONS_CONFIGURATION_MISSING")
    variables = {
        "DEYE_APPROVER_IDS": reviewers["DEYE_APPROVER_IDS"] or "[]",
        "DEYE_APPROVER_LOGINS": reviewers["DEYE_APPROVER_LOGINS"] or "{}",
        "DEYE_APPROVAL_CUTOFF": environment.get("DEYE_APPROVAL_CUTOFF") or "23:00",
    }
    flags = {"DEYE_TOU_WRITE_ENABLED": str(execution).lower(),
             "DEYE_APPROVAL_ENABLED": str(approval).lower()}
    template = (Path(__file__).resolve().parents[1] / "config/deye-approval.workflow.example.json").read_bytes()
    store = GitHubPrivateStore(repository, token, public_repository=public_repository)
    try:
        # Propagate disabling before publishing anything that could enable a writer.
        store.sync_approval_variables({name: value for name, value in flags.items() if value == "false"})
        for name, value in secrets.items():
            store.verify_private()
            result = subprocess.run(
                ["gh", "secret", "set", name, "--repo", repository],
                input=value.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**{key: val for key, val in os.environ.items() if not key.startswith("DEYE_")},
                     "GH_TOKEN": token, "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1"},
                timeout=30, check=False,
            )
            if result.returncode:
                raise PrivateStoreError(f"SECRET_SYNC_FAILED_{name}")
        store.sync_approval_workflow(template)
        store.sync_approval_variables(variables)
        store.sync_approval_variables({name: value for name, value in flags.items() if value == "true"})
    finally:
        store.close()


def main():
    try:
        sync(os.environ.get("DEYE_DATA_REPOSITORY"), os.environ.get("GITHUB_REPOSITORY"),
             os.environ.get("DEYE_APPROVAL_WORKFLOW_TOKEN"), os.environ)
    except PrivateStoreError as exc:
        print("Private Actions sync failed:", exc)
        return 1
    except (OSError, subprocess.SubprocessError, ValueError):
        print("Private Actions sync failed. Verify GitHub CLI and the configuration template.")
        return 1
    print("Private Actions configuration synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
