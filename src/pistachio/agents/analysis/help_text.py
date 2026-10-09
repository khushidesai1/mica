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

COMMAND_HELP = """*Projects*
• `init <name> gs://bucket/folder/` — Create and select a project (DM).
• `switch` — List your projects (alias: `projects`).
• `switch <name or ID>` — Select a project for new threads (alias: `use`).
• `rename <name or ID> <new-name>` — Rename your project (owner, DM).
Existing threads keep their project. In channels, add `--force` to replace a selection.

*Sharing — owner, DM*
• `share <project> @user` — Give someone project access.
• `unshare <project> @user` — Remove someone's project access.
• `publish <project> #channel` — Allow replies visible to that channel.
• `unpublish <project> #channel` — Stop replies in that channel.

*Project guide and tasks*
• `brief` — Show this thread's project guide draft.
• `save brief` — Save the draft for new threads.
• `clean brief` — Draft a guide cleanup for review.
• `cancel` — Stop queued or running computation.

*Memory*
• `memory context` — Show the conversation summary for this scope.
• `forget context` — Clear that summary and start fresh context.
• `memory` — Show your private profile, preferences, and notes (DM).
• `remember <text>` — Save a private note (DM).
• `forget <number|profile|preferences|all>` — Clear selected private memory (DM).

*Project knowledge — when enabled, shared with project members*
• `knowledge` — Show the index, file count and last update.
• `knowledge show <path>` — Read an entry.
• `forget knowledge <path>` — Remove an entry, retaining its history.
• `undo knowledge` — Restore the files changed by the most recent save.
• `knowledge github on` / `knowledge github off` — Owner: enable or stop GitHub mirroring.
Shared and published projects mirror automatically when configured, unless the owner turns it off.
Private projects require owner opt-in. Repository readers see all mirrored projects and history.
GitHub edits do not sync back; corrections should be made here. Deletions retain Git history.

*GCS writes — DM*
• `write grant <project> gs://bucket/folder/ [duration]` — Allow new files.
• `write status <project>` — Show the destination and expiry.
• `write revoke <project>` — Stop future uploads.
Use `30m`, `1h`, or `7d`; omit duration for no expiry (until revoked).
Creates new files only; existing files cannot be overwritten or deleted.
You must belong to the project. Use approved folders in that project's linked bucket.
Only designated write admins can grant or revoke. Grants apply to future runs.

*AtoMx transfers — configured projects*
• `transfer <sample> [as <name>] [from <export_root>]` — Preview, then Confirm or Cancel.
• `transfers` — Show markers and the last published source inventory.
• `cancel transfer <sample>` — Stop an active transfer.
• `atomx login status` — Show saved-login details (never the password).
• `replace atomx login` / `forget atomx login` — Owner or login saver only.
Enter credentials only through the login button's form, never in chat.

*Help*
• `help` — Show this command list.
• `help write` — Show write-access examples and saving instructions."""
