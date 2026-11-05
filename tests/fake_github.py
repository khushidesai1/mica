"""In-memory GitHub GraphQL/Contents server; no network or credentials."""

from __future__ import annotations

import base64
import hashlib
import json

import httpx


def blob_oid(text: str) -> str:
    data = text.encode() if isinstance(text, str) else text
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class FakeGitHub:
    def __init__(self):
        self.head = "0" * 40
        self.trees = {self.head: {}}
        self.commits = []
        self.requests = []
        self.fail = 0
        self.timeout = False
        self.truncated = set()
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def seed(self, changes, headline="External edit"):
        files = dict(self.trees[self.head])
        for path, text in changes.items():
            if text is None:
                files.pop(path, None)
            else:
                files[path] = text
        parent = self.head
        self.head = hashlib.sha1(
            (
                self.head + json.dumps(changes, default=lambda raw: base64.b64encode(raw).decode())
            ).encode()
        ).hexdigest()
        self.trees[self.head] = files
        self.commits.insert(
            0,
            {
                "oid": self.head,
                "messageHeadline": headline,
                "parents": {"nodes": [{"oid": parent}]},
            },
        )
        return self.head

    def tree(self, files, prefix):
        entries = []
        names = sorted({p[len(prefix) :].split("/")[0] for p in files if p.startswith(prefix)})
        for name in names:
            path = prefix + name
            if path in files:
                text = files[path]
                obj = {
                    "oid": blob_oid(text),
                    "text": (text if isinstance(text, str) else None)
                    if path not in self.truncated
                    else "",
                    "byteSize": len(text.encode() if isinstance(text, str) else text),
                    "isTruncated": path in self.truncated,
                }
                kind = "blob"
            else:
                obj, kind = self.tree(files, path + "/"), "tree"
            entries.append({"name": name, "type": kind, "object": obj})
        return {"entries": entries}

    def handle(self, request):
        self.requests.append(request)
        if self.timeout:
            raise httpx.ReadTimeout("unavailable", request=request)
        if self.fail:
            return httpx.Response(self.fail)
        if request.method == "GET":
            if "/git/blobs/" in request.url.path:
                oid = request.url.path.rsplit("/", 1)[1]
                text = next(
                    t for files in self.trees.values() for t in files.values() if blob_oid(t) == oid
                )
            else:
                path = request.url.path.split("/contents/", 1)[1]
                text = self.trees[request.url.params["ref"]][path]
            return httpx.Response(
                200,
                json={
                    "content": base64.b64encode(
                        text.encode() if isinstance(text, str) else text
                    ).decode(),
                    "encoding": "base64",
                    "sha": blob_oid(text),
                },
            )
        payload = json.loads(request.content)
        variables, query = payload["variables"], payload["query"]
        if "createCommitOnBranch" in query:
            params = variables["input"]
            if params["expectedHeadOid"] != self.head:
                return httpx.Response(200, json={"errors": [{"type": "STALE_DATA"}]})
            changes = {}
            for item in params["fileChanges"].get("additions", []):
                raw = base64.b64decode(item["contents"])
                try:
                    changes[item["path"]] = raw.decode()
                except UnicodeDecodeError:
                    changes[item["path"]] = raw
            changes.update(
                {item["path"]: None for item in params["fileChanges"].get("deletions", [])}
            )
            sha = self.seed(changes, params["message"]["headline"])
            return httpx.Response(
                200, json={"data": {"createCommitOnBranch": {"commit": {"oid": sha}}}}
            )
        head = variables.get("head") or self.head
        files = self.trees[head]
        if "MemoryBlobs" in query:
            selected = {}
            for key, path in variables.items():
                if key.startswith("p") and key[1:].isdigit():
                    value = files.get(path)
                    selected["f" + key[1:]] = (
                        {
                            "object": {
                                "oid": blob_oid(value),
                                "text": value,
                                "byteSize": len(
                                    value.encode() if isinstance(value, str) else value
                                ),
                                "isTruncated": path in self.truncated,
                            }
                        }
                        if value is not None
                        else None
                    )
            return httpx.Response(200, json={"data": {"repository": {"object": selected}}})
        prefix = variables.get("path", "") + "/"
        names = files.get(".mica/projects.json")
        node = {
            "names": {
                "object": {"text": names, "byteSize": len(names.encode()), "isTruncated": False}
            }
            if names is not None
            else None,
            "oid": head,
            "file": {"object": self.tree(files, prefix)}
            if any(p.startswith(prefix) for p in files)
            else None,
            "history": {
                "totalCount": len(self.commits),
                "nodes": self.commits,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        }
        repository = {"isPrivate": True, "ref": {"target": node}, "object": node}
        result = {"data": {"repository": repository}}
        if "MemoryRead" in query and node["file"] is None:
            result["errors"] = [
                {
                    "type": "NOT_FOUND",
                    "path": ["repository", "ref", "target", "file"],
                }
            ]
        return httpx.Response(200, json=result)
