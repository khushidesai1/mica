"""Run locally once: python scripts/gmail_authorize.py /path/to/client_secret.json.

The refresh token is sensitive. Store it in Secret Manager; never commit the output.
"""
from __future__ import annotations

import argparse

from google_auth_oauthlib.flow import InstalledAppFlow

from mica.connectors.gmail_client import SCOPES


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_secrets", help="Downloaded desktop OAuth client JSON")
    args = parser.parse_args()
    flow = InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not credentials.refresh_token:
        raise SystemExit("No refresh token returned; grant offline consent again.")
    print(credentials.refresh_token)


if __name__ == "__main__":
    main()
