"""Paginated metadata using the real GCS iterator, with no network or credentials."""

from google.auth.credentials import AnonymousCredentials
from google.cloud import storage


def fake_gcs(monkeypatch, folders, *, fail_page=None):
    client = storage.Client(project="test", credentials=AnonymousCredentials())
    requests = []

    def api_request(**kwargs):
        requests.append(kwargs)
        if fail_page == len(requests):
            raise RuntimeError("private error details")
        query = kwargs["query_params"]
        prefix = query["prefix"]
        children = folders.get(prefix, [])
        offset = int(query.get("pageToken", 0))
        page = children[offset : offset + 100]
        response = {
            "prefixes": [prefix + child for child in page if child.endswith("/")],
            "items": [
                {"name": prefix + child, "size": "12"} for child in page if not child.endswith("/")
            ],
        }
        if offset + 100 < len(children):
            response["nextPageToken"] = str(offset + 100)
        return response

    monkeypatch.setattr(client._connection, "api_request", api_request)
    monkeypatch.setattr(storage, "Client", lambda: client)
    return requests
