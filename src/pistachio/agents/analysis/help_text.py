"""Short project controls shared by onboarding and chat commands."""

PROJECT_HELP = (
    "Projects: `init <name> gs://bucket/folder/` to create & select · `switch` to list · "
    "`switch <name or ID>` to select · `rename <name or ID> <new-name>` in DM.\n"
    "Switching applies to new threads; existing threads keep their project."
)

WRITE_HELP = (
    "GCS writes (DM): `write grant <project> gs://bucket/folder/ [duration]` · "
    "`write status <project>` · `write revoke <project>`.\n"
    "Use `30m`, `1h`, or `7d`; omit duration for no expiry (until revoked). "
    "Grants create new files only, within approved folders in that project's linked bucket. "
    "You must belong to the project. Only designated write admins can grant or revoke; "
    "project members can check status."
)

WRITE_DETAILS = (
    "Example: `write grant liver gs://bucket/liver/outputs/` lasts until revoked; "
    "add `1h` to expire after an hour.\n"
    "Each project has one destination; a new grant replaces it. Grants apply to future runs. "
    "After granting, ask Pistachio to save the desired results to that GCS folder. "
    "Existing files cannot be overwritten or deleted. Revoking blocks further uploads; "
    "an upload already completing may finish."
)
