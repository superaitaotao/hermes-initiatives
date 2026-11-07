"""CLI registration for ``hermes initiatives setup|status|doctor``."""
from __future__ import annotations

import argparse
import json
import subprocess

from . import autonomy_setup as setup


def register_cli(parser) -> None:
    commands = parser.add_subparsers(dest="initiatives_command", required=True)
    install = commands.add_parser("setup", help="Install profile-local autonomous review/recovery jobs")
    install.add_argument("--profile", required=True)
    install.add_argument("--project", required=True)
    install.add_argument("--recovery", action=argparse.BooleanOptionalAction, default=True)
    install.add_argument("--merge-gate", action=argparse.BooleanOptionalAction, default=True)
    install.add_argument("--schedule-review", default="15,45 * * * *")
    install.add_argument("--schedule-recovery", default="*/30 * * * *")
    install.add_argument("--dry-run", action="store_true")
    status = commands.add_parser("status", help="List plugin jobs and wrapper presence")
    status.add_argument("--profile", action="append", help="Repeat to inspect multiple profiles; default: all profiles")
    doctor = commands.add_parser("doctor", help="Check settings and autonomy prerequisites")
    doctor.add_argument("--project")


def command(args) -> int:
    try:
        if args.initiatives_command == "setup":
            settings, error = setup.read_settings()
            if error:
                raise ValueError(error)
            plans = setup.build_plan(settings, args.project, args.profile,
                                     recovery=args.recovery, merge_gate=args.merge_gate,
                                     schedule_review=args.schedule_review, schedule_recovery=args.schedule_recovery)
            setup.execute_plan(plans, dry_run=args.dry_run)
            return 0
        if args.initiatives_command == "status":
            profiles = args.profile
            if not profiles:
                root = setup.hermes_root()
                profiles = ["default", *sorted(p.name for p in (root / "profiles").glob("*") if p.is_dir())]
            print(json.dumps([row for profile in profiles for row in setup.status(profile)], indent=2))
            return 0
        errors = setup.doctor(args.project)
        print("\n".join(errors) if errors else "Autonomy prerequisites OK")
        return 1 if errors else 0
    except subprocess.CalledProcessError as exc:
        print(f"initiatives: cron command failed; inspect paused jobs: {exc.stderr or exc.stdout or exc}")
        return 2
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"initiatives: {exc}")
        return 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="hermes initiatives")
    register_cli(parser)
    raise SystemExit(command(parser.parse_args()))
