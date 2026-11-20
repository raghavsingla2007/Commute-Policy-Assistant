"""CLI mock JWT issuer: prints a signed token to stdout.

    python -m scripts.issue_token --company kestrel --role admin [--ttl 900] [--subject alice]

Uses the same JWT_SECRET / JWT_ISSUER / JWT_AUDIENCE as the server (read from the environment),
so run it inside the container (`docker compose run --rm api python -m scripts.issue_token ...`)
or with the same .env. It refuses the dev-default secret unless DEV_MODE=true, like the server.
"""

import argparse
import sys

from app.auth import MAX_TTL_S, ROLES, mint_token
from app.config import KNOWN_COMPANIES, load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mint a JWT for the Commute Policy Assistant")
    parser.add_argument("--company", required=True, choices=sorted(KNOWN_COMPANIES))
    parser.add_argument("--role", default="employee", choices=sorted(ROLES))
    parser.add_argument("--ttl", type=int, default=None, help=f"seconds, 1..{MAX_TTL_S} (default JWT_TTL_S)")
    parser.add_argument("--subject", default="demo-user")
    args = parser.parse_args(argv)

    settings = load_settings()
    try:
        settings.validate()
        token = mint_token(settings, args.company, args.role, args.subject, args.ttl)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
