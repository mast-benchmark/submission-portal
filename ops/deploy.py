#!/usr/bin/env python3
"""Create the private submissions dataset and the Space, set secrets, push the code.

Private inputs (HF_TOKEN in .env, teams.csv) are read from MAST_PRIVATE_DIR, by default the ``ops``
directory next to this checkout, so they never sit inside the repository.

    python ops/deploy.py --namespace mast-benchmark            # full deploy
    python ops/deploy.py --namespace sahel-sh --dry-run        # show what would happen
    python ops/deploy.py --sync-only                           # just refresh the vendored validator

Never prints the token.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import os

REPO_ROOT = Path(__file__).resolve().parents[1]                 # the submission-portal checkout
PRIVATE_DIR = Path(os.environ.get("MAST_PRIVATE_DIR") or REPO_ROOT.parent / "ops")   # .env, exports, roster: never inside a repository
SPACE_DIR = REPO_ROOT
VALIDATOR_SRC = Path(os.environ.get("MAST_VALIDATE_SRC") or REPO_ROOT.parent / "mast-validate" / "src" / "mast_validate")
ENV_FILE = PRIVATE_DIR / ".env"


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def sync_validator() -> None:
    dst = SPACE_DIR / "mast_validate"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(VALIDATOR_SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    print(f"vendored {VALIDATOR_SRC.name} -> {dst}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", default="mast-benchmark", help="owner of the private dataset")
    ap.add_argument("--space-namespace", default=None,
                    help="owner of the Space (default: --namespace). Orgs need a paid plan to host Gradio "
                         "Spaces; a personal account can host one free.")
    ap.add_argument("--space-name", default="submit")
    ap.add_argument("--dataset-name", default="mast-2026-submissions")
    ap.add_argument("--deadline", default=None, help="MAST_DEADLINE_UTC variable, ISO-8601")
    ap.add_argument("--sync-only", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sync_validator()
    if args.sync_only:
        return 0
    env = load_env()
    token = env.get("HF_TOKEN")
    if not token:
        print("ops/.env has no HF_TOKEN", file=sys.stderr)
        return 1
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    who = api.whoami()
    orgs = {o["name"] for o in who.get("orgs", [])}
    space_ns = args.space_namespace or args.namespace
    for ns in {args.namespace, space_ns}:
        if ns != who["name"] and ns not in orgs:
            print(f"token user {who['name']!r} is not a member of {ns!r} (orgs: {sorted(orgs)})", file=sys.stderr)
            return 1
    dataset_id = f"{args.namespace}/{args.dataset_name}"
    space_id = f"{space_ns}/{args.space_name}"
    print(f"dataset (private): {dataset_id}\nspace (public):    {space_id}")
    if args.dry_run:
        return 0

    api.create_repo(dataset_id, repo_type="dataset", private=True, exist_ok=True)
    if not api.file_exists(dataset_id, "teams.csv", repo_type="dataset"):
        teams = PRIVATE_DIR / "teams.csv"
        content = teams.read_bytes() if teams.is_file() else b"team_name,contact_email,member_emails,tracks,aliases,registered_at\n"
        api.upload_file(path_or_fileobj=content, path_in_repo="teams.csv", repo_id=dataset_id,
                        repo_type="dataset", commit_message="initial roster")
        print("uploaded teams.csv" + ("" if teams.is_file() else " (empty header only; run import_roster.py --push)"))
    api.create_repo(space_id, repo_type="space", space_sdk="gradio", private=False, exist_ok=True)
    api.add_space_secret(space_id, "HF_TOKEN", token)
    api.add_space_variable(space_id, "SUBMISSIONS_REPO", dataset_id)
    api.add_space_variable(space_id, "STORAGE_BACKEND", "hf")
    if args.deadline:
        api.add_space_variable(space_id, "MAST_DEADLINE_UTC", args.deadline)
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        if env.get(k):
            api.add_space_secret(space_id, k, env[k])
    api.upload_folder(folder_path=str(SPACE_DIR), repo_id=space_id, repo_type="space",
                      commit_message="deploy portal",
                      ignore_patterns=["local-store/*", "tests/*", "__pycache__/*", "*.pyc", ".pytest_cache/*", "*.log"],
                      delete_patterns=["*"])
    print(f"deployed: https://huggingface.co/spaces/{space_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
