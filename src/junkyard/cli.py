"""Local administration. Credentials are accepted only via hidden TTY prompts."""

import argparse
import getpass
from pathlib import Path
import sys
import warnings

from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
import uvicorn

from junkyard.app import create_app
from junkyard.config import ConfigError, load_settings
from junkyard.database import Database, StoreError
from junkyard.security import PASSWORD_HASHER, PasswordError, hash_password, validate_password


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Never echo an accidentally supplied password from an unknown argument.
        super().error("invalid command-line arguments; use --help")


def prompt_password(label: str) -> str:
    if not sys.stdin.isatty():
        raise PasswordError("password commands require an interactive terminal; stdin and argument passwords are not accepted")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass(f"New {label} password: ")
            confirmation = getpass.getpass(f"Confirm {label} password: ")
    except getpass.GetPassWarning:
        raise PasswordError("cannot disable terminal echo; password entry refused") from None
    if password != confirmation:
        raise PasswordError("passwords do not match")
    validate_password(password)
    return password


def main(argv: list[str] | None = None) -> int:
    parser = Parser(description="Local administration for the Junkyard file server")
    parser.add_argument("--config", type=Path, default=Path("junkyard.toml"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize the database and both passwords")
    reset = commands.add_parser("passwd", help="reset one password without knowing its old value")
    reset.add_argument("kind", choices=("cli", "browser"))
    commands.add_parser("serve", help="serve on the configured loopback address")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        database = Database(settings.data_dir)
        if args.command == "init":
            if database.path.exists() or database.path.is_symlink():
                raise StoreError("database already exists; use junkyard passwd to reset a credential")
            cli_password = prompt_password("CLI")
            browser_password = prompt_password("browser")
            if cli_password == browser_password:
                raise PasswordError("CLI and browser passwords must be different")
            database.initialize(hash_password(cli_password), hash_password(browser_password))
            print("Database and both credentials initialized.")
        elif args.command == "passwd":
            database.check_initialized()
            password = prompt_password(args.kind)
            # Maintain credential separation even during local recovery.
            other_kind = "browser" if args.kind == "cli" else "cli"
            try:
                matches_other = PASSWORD_HASHER.verify(database.password_hash(other_kind), password)
            except VerifyMismatchError:
                matches_other = False
            except (VerificationError, InvalidHashError):
                # Recovery of one damaged credential must remain possible.
                matches_other = False
            if matches_other:
                raise PasswordError("CLI and browser passwords must be different")
            database.replace_password(args.kind, hash_password(password))
            print(f"{args.kind.capitalize()} password changed.")
        else:
            database.check_initialized()
            uvicorn.run(
                create_app(settings),
                host=settings.bind_host,
                port=settings.bind_port,
                workers=1,
                proxy_headers=False,
                access_log=False,
                server_header=False,
            )
    except (ConfigError, StoreError, PasswordError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 1
    return 0
