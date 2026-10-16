"""Explicit legacy migration: stream/verify a lean checkpoint and retain its original."""

import argparse
import json

from pistachio.agents.analysis.checkpoints import migrate_thread
from pistachio.agents.analysis.storage import GCSStorage

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--thread", required=True)
    args = parser.parse_args()
    print(json.dumps(migrate_thread(GCSStorage(args.bucket), args.thread), indent=2))
