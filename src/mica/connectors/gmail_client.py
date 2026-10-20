"""Mailbox OAuth and cryptographic sender verification; never trust verdict headers."""

from __future__ import annotations

import re
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any

import dkim
import dns.exception
import dns.resolver
import google_auth_httplib2
import httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from mica.config import Settings

SCOPES = [f"https://www.googleapis.com/auth/gmail.{s}" for s in ("readonly", "send", "modify")]
MAX_MESSAGE_BYTES = 35_000_000


def enabled(settings: Settings) -> bool:
    return bool(
        settings.gmail_account
        and settings.gmail_label
        and settings.gmail_oauth_client_id
        and settings.gmail_oauth_client_secret.get_secret_value()
        and settings.gmail_refresh_token.get_secret_value()
    )


def service(settings: Settings) -> Any:
    if not enabled(settings):
        raise ValueError("Gmail OAuth and label configuration is incomplete")
    credentials = Credentials(  # type: ignore[no-untyped-call]
        token=None,
        refresh_token=settings.gmail_refresh_token.get_secret_value(),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.gmail_oauth_client_id,
        client_secret=settings.gmail_oauth_client_secret.get_secret_value(),
        scopes=SCOPES,
    )
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=20))
    return build("gmail", "v1", http=http, cache_discovery=False)


def mailbox(settings: Settings) -> str:
    # Consumer Gmail notifications name the base account, never a plus alias.
    email = settings.gmail_account.strip().lower()
    local, domain = email.rsplit("@", 1)
    if domain in {"gmail.com", "googlemail.com"}:
        return local.split("+", 1)[0].replace(".", "") + "@gmail.com"
    return email


def own_addresses(settings: Settings) -> set[str]:
    return {
        settings.gmail_account.strip().lower(),
        mailbox(settings),
        *(a.strip().lower() for a in settings.gmail_aliases),
    }


def is_own(address: str, settings: Settings) -> bool:
    address = address.lower()
    if address in own_addresses(settings):
        return True
    local, _, domain = address.rpartition("@")
    return domain in {"gmail.com", "googlemail.com"} and (
        local.split("+", 1)[0].replace(".", "") + "@gmail.com" == mailbox(settings)
    )


def dns_txt(name: bytes, timeout: int = 5) -> bytes | None:
    try:
        records = dns.resolver.resolve(name.decode("ascii"), "TXT", lifetime=timeout, search=False)
        values = list(records)
        return b"".join(values[0].strings) if len(values) == 1 else None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return None
    except dns.exception.DNSException as exc:
        # dkimpy turns its own DNS timeout into a failed signature. Transient DNS failure
        # must instead leave the mailbox cursor untouched so the message is retried.
        raise RuntimeError("DKIM DNS verification is temporarily unavailable") from exc


def verified_message(raw: bytes) -> tuple[str, EmailMessage] | None:
    if len(raw) > MAX_MESSAGE_BYTES:
        return None
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not isinstance(message, EmailMessage) or message.defects:
        return None
    for name in ("From", "Subject", "Message-ID", "To", "Cc", "Reply-To"):
        values = message.get_all(name, [])
        if len(values) > 1 or any(getattr(v, "defects", ()) for v in values):
            return None
    from_header = message["From"]
    addresses: Any = getattr(from_header, "addresses", ())
    if len(addresses) != 1:
        return None
    sender = addresses[0].addr_spec.lower()
    if not re.fullmatch(r"[^\s@<>]+@[A-Za-z0-9.-]+", sender):
        return None
    if not re.fullmatch(r"<[^\s<>]+@[^\s<>]+>", str(message["Message-ID"] or "")):
        return None
    domain = sender.rsplit("@", 1)[1].encode("ascii")
    signatures = message.get_all("DKIM-Signature", [])
    if len(signatures) > 5 or len(str(message["Subject"] or "")) > 998:
        return None
    if sum(len(getattr(message[h], "addresses", ())) for h in ("To", "Cc")) > 100:
        return None
    try:
        verifier = dkim.DKIM(raw, timeout=5)
    except dkim.DKIMException:
        return None
    # Verify the original bytes against DNS. Injected Authentication-Results has no authority.
    for index, value in enumerate(signatures):
        try:
            tags = dkim.util.parse_tag_value(str(value).encode("ascii"))
            if tags.get(b"d", b"").lower() != domain or b"l" in tags:
                continue
            signed = {s.strip().lower() for s in tags.get(b"h", b"").split(b":")}
            required = {
                h.encode()
                for h in ("from", "to", "subject", "message-id", "cc")
                if message[h] is not None
            }
            if not required <= signed or tags.get(b"a") not in {b"rsa-sha256", b"ed25519-sha256"}:
                continue
            if verifier.verify(idx=index, dnsfunc=dns_txt):
                return sender, message
        except (dkim.DKIMException, ValueError, UnicodeError):
            continue
    return None


def label_id(api: Any, configured: str) -> str:
    labels = api.users().labels().list(userId="me").execute().get("labels", [])
    matches = [item["id"] for item in labels if configured in {item["id"], item["name"]}]
    if len(set(matches)) != 1:
        raise ValueError("Configure one existing Mica Gmail label")
    return str(matches[0])
