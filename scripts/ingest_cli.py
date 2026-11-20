"""CLI to ingest a policy markdown file as a new version (company and version come from the file header).

Usage:  python -m scripts.ingest_cli data/policies/kestrel_v2.md
        python -m scripts.ingest_cli --seed          # ingest the v1 seed policies if missing

Writes directly to DATA_DIR. Run it while the API server is STOPPED: Chroma's embedded client is
single-process. To upgrade a policy on a running server, use the admin API (added in P6).
"""

import argparse
import sys
from pathlib import Path



from app.config import load_settings
from app.embeddings import MiniLMEmbedder
from app.ingest import IngestFailed, PolicyParseError, ingest_policy, parse_policy, seed_if_empty
from app.store import Store, VersionConflict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?", type=Path, help="policy markdown file")
    parser.add_argument("--seed", action="store_true", help="ingest seed v1 policies if missing")
    args = parser.parse_args()
    if not args.file and not args.seed:
        parser.error("give a policy file or --seed")

    settings = load_settings()
    store = Store(settings.data_dir, MiniLMEmbedder())
    store.janitor()
    if args.seed:
        print("seeded:", seed_if_empty(store, settings.policies_dir) or "nothing (all companies active)")
    if args.file:
        text = args.file.read_text(encoding="utf-8")
        try:
            result = ingest_policy(store, parse_policy(text).company_id, text)
        except (PolicyParseError, VersionConflict, IngestFailed) as exc:
            print(f"FAILED ({type(exc).__name__}): {exc}. The active policy is unchanged.", file=sys.stderr)
            return 1
        print(f"active: {result.company_id} v{result.version} ({result.chunks} chunks), "
              f"replaced v{result.previous_version}")
    for row in store.rows():
        print(f"  {row['company_id']:<9} v{row['version']}  {row['status']:<8} {row['collection']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
