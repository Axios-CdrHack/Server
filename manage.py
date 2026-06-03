#!/usr/bin/env python3
import os
import sys
from pathlib import Path


RUNSERVER_ADDRPORT = "0.0.0.0:8001"


def load_env_file(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env_key = key.strip()
        env_value = value.strip().strip('"').strip("'")
        if not os.environ.get(env_key):
            os.environ[env_key] = env_value


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "axios_django.settings")
    base_dir = Path(__file__).resolve().parent
    load_env_file(base_dir / ".env")
    load_env_file(base_dir.parent / ".env")
    if len(sys.argv) >= 2 and sys.argv[1] == "runserver":
        has_addrport = any(not arg.startswith("-") for arg in sys.argv[2:])
        if not has_addrport:
            sys.argv.insert(2, RUNSERVER_ADDRPORT)
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
