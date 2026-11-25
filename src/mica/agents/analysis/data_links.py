"""Normalize supported Google Cloud Storage links, including Slack's link markup."""

import re
from urllib.parse import unquote, urlsplit


def message_text(text: str) -> str:
    text = re.sub(r"<@[A-Z0-9]+>", "", text)
    return re.sub(r"<((?:gs|https)://[^>|]+)(?:\|[^>]+)?>", r"\1", text).strip()


def data_uri(link: str) -> str:
    link = message_text(link).strip("`")
    uri = urlsplit(link)
    if uri.scheme == "gs":
        bucket, path = uri.netloc, uri.path
        if uri.query or uri.fragment:
            raise ValueError("Use a bucket or folder link without query parameters.")
    elif uri.scheme == "https" and uri.hostname in {
        "storage.googleapis.com",
        "storage.cloud.google.com",
        "console.cloud.google.com",
    }:
        path = uri.path
        if uri.hostname == "console.cloud.google.com":
            prefix = "/storage/browser/"
            if not path.startswith(prefix):
                raise ValueError("Use a Google Cloud Storage bucket or folder link.")
            path = path.removeprefix(prefix).removeprefix("_details/")
        bucket, _, path = path.lstrip("/").partition("/")
        path = "/" + unquote(path)
    else:
        raise ValueError("Use a gs:// link or a Google Cloud Storage bucket/folder URL.")
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket)
        or any(x in {".", ".."} for x in path.split("/"))
        or any(x in path for x in ("%", "\\", "\x00"))
    ):
        raise ValueError("Invalid bucket or folder path.")
    return f"gs://{bucket}{path or '/'}"
