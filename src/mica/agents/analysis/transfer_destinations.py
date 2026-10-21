"""Resolve request-specific import folders from explicit instructions or project layout."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .data_links import message_text
from .registry import RegistryDoc
from .transfer_config import TransferProject, TransferRequest
from .write_access import scope

URI = re.compile(r"gs://[^\s<>`\"')\]]+")


def guide_paths(guide: str, data_uri: str) -> set[str]:
    explicit = {scope(p.rstrip(".,;")) for p in URI.findall(guide)}
    # Resolve relative layout entries only against an explicitly documented bucket root.
    relative = set(
        re.findall(
            r"(?<![\w/])(?:raw_outputs/raw|raw_outputs/additional_files|raw|additional_files)(?:/)?(?=[`\s<]|$)",
            guide,
        )
    )
    roots = {
        p
        for p in explicit
        if not any(
            s in urlsplit(p).path.split("/")
            for s in ("raw", "raw_outputs", "additional_files", "processed")
        )
    }
    result = set(explicit)
    for root in roots:
        result.update(scope(root + part.rstrip("/") + "/") for part in relative)
    return result


def resolve(
    request: TransferRequest, guide: str, text: str, doc: RegistryDoc, project: str, owner: str
) -> TransferProject:
    if not doc.can_use(project, owner):
        raise ValueError("Project membership is required.")
    linked = doc.projects[project].data_uri
    bucket = urlsplit(linked).netloc
    requested = {scope(p.rstrip(".,;")) for p in URI.findall(message_text(text))}
    documented = guide_paths(guide, linked)
    raw = {p for p in documented if urlsplit(p).path.rstrip("/").split("/")[-1] == "raw"}
    chosen = scope(request.destination) if request.destination else ""
    if chosen:
        if chosen not in requested | documented:
            raise ValueError(
                "What exact raw import folder should I use? Include its gs:// "
                "path, or add it to the project guide."
            )
    elif len(requested) == 1:
        chosen = next(iter(requested))
    elif len(raw) == 1:
        chosen = next(iter(raw))
    else:
        raise ValueError(
            "Which raw import folder should I use for this transfer? The "
            "project guide does not identify one unambiguous raw base. Include"
            " the gs:// path."
        )
    extra = scope(request.additional_destination) if request.additional_destination else ""
    additional = {
        p for p in documented if urlsplit(p).path.rstrip("/").split("/")[-1] == "additional_files"
    }
    if extra and extra not in requested | documented:
        raise ValueError(
            "Please specify the additional-files destination explicitly or "
            "document it in the project guide."
        )
    if not extra:
        sibling = chosen.rstrip("/").rsplit("/", 1)[0] + "/additional_files/"
        if sibling in additional:
            extra = sibling
        elif chosen not in requested and len(additional) == 1:
            extra = next(iter(additional))
        else:
            # Keep additional data in its own child folder under the approved raw base.
            extra = chosen + ".additional_files/"
    if any(urlsplit(p).netloc != bucket for p in (chosen, extra)):
        raise ValueError("Choose import folders in this project's linked bucket.")
    if chosen == extra:
        raise ValueError("Raw and additional files require separate folders.")
    target = request.as_sample or request.sample
    sample_paths = (chosen + target + "/", extra + target + "/")
    if sample_paths[0].startswith(sample_paths[1]) or sample_paths[1].startswith(sample_paths[0]):
        raise ValueError("The two sample destination folders must not overlap.")
    # A project owner can approve an import elsewhere in the linked bucket. A collaborator
    # is limited to the linked project prefix, unless an operator configured a narrower policy.
    if not doc.owns(project, owner) and any(
        not p.startswith(scope(linked)) for p in (chosen, extra)
    ):
        raise ValueError(
            "These import folders are outside this project's linked data "
            "prefix. Ask a project owner to approve this import or configure "
            "the project root."
        )
    return TransferProject(
        bucket=bucket,
        raw_prefix=urlsplit(chosen).path.lstrip("/"),
        additional_prefix=urlsplit(extra).path.lstrip("/"),
        status_prefix=urlsplit(chosen).path.lstrip("/") + ".mica-transfers/",
        layout_source="request" if chosen in requested else "project_guide",
    )
