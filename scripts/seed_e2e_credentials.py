#!/usr/bin/env python3
"""Seed E2E credentials from environment variables into Credential Vault."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from app.core.credential_vault import create_credential, list_credentials
from app.core.db_pool import close_pool, init_pool


ROLE_PREFIXES = ("ADMIN", "WHOLESALE", "RETAIL")


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _login_steps(login_url: str) -> list[dict[str, str | int]]:
    return [
        {"action": "navigate", "url": login_url},
        {
            "action": "fill",
            "selector": (
                "input[name='login'], input#login, input[name='userid'], "
                "input[name='id'], input[name='username'], input[type='text']"
            ),
            "value": "{{username}}",
        },
        {
            "action": "fill",
            "selector": "input[name='password'], input#password, input[type='password']",
            "value": "{{password}}",
        },
        {
            "action": "click",
            "selector": (
                "button[type='submit'], input[type='submit'], "
                "button:has-text('로그인'), a:has-text('로그인')"
            ),
        },
        {"action": "wait", "ms": 2000},
    ]


def _credential_specs() -> list[dict[str, str]]:
    project = os.getenv("NEWTALK_V1_PROJECT", "NTV2")
    specs: list[dict[str, str]] = []
    for role in ROLE_PREFIXES:
        prefix = f"NEWTALK_V1_{role}"
        specs.append(
            {
                "role": role.lower(),
                "project": project,
                "service": os.getenv(f"{prefix}_SERVICE", f"newtalk-v1-{role.lower()}"),
                "label": os.getenv(f"{prefix}_LABEL", role.lower()),
                "login_url": os.getenv(f"{prefix}_LOGIN_URL", ""),
                "username": os.getenv(f"{prefix}_USERNAME", ""),
                "password": os.getenv(f"{prefix}_PASSWORD", ""),
            }
        )
    return specs


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env-file",
        default=".env.e2e.local",
        help="Path to local E2E env file. Use empty string to skip file loading.",
    )
    args = parser.parse_args()

    if args.env_file:
        _load_env_file(Path(args.env_file))

    await init_pool()
    try:
        saved = []
        for spec in _credential_specs():
            missing = [name for name in ("login_url", "username", "password") if not spec[name]]
            if missing:
                raise SystemExit(
                    f"Missing required env for {spec['service']}: {', '.join(missing)}"
                )
            item = await create_credential(
                service=spec["service"],
                project=spec["project"],
                label=spec["label"],
                login_url=spec["login_url"],
                username=spec["username"],
                password=spec["password"],
                extra_fields={"source": "env", "system": "newtalk-v1", "role": spec["role"]},
                login_steps=_login_steps(spec["login_url"]),
            )
            saved.append(item)

        active = await list_credentials(project=os.getenv("NEWTALK_V1_PROJECT", "NTV2"))
        print(f"saved={len(saved)} active_project_credentials={len(active)}")
        for item in saved:
            print(
                "credential "
                f"project={item.get('project')} service={item.get('service')} "
                f"label={item.get('label')} username={item.get('username')} "
                "password=<stored>"
            )
    finally:
        await close_pool()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
