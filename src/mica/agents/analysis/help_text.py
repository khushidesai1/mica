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
    "Grants allow new files and confirmed overwrites within approved folders "
    "in that project's linked bucket. "
    "You must belong to the project. Only designated write admins can grant or revoke; "
    "project members can check status."
)

WRITE_DETAILS = (
    "Example: `write grant liver gs://bucket/liver/outputs/` lasts until revoked; "
    "add `1h` to expire after an hour.\n"
    "Each project has one destination; a new grant replaces it. Grants apply to future runs. "
    "After granting, ask Mica to save the desired results to that GCS folder. "
    "Confirm overwrites before each run; other files stay unchanged. "
    "Revoking blocks further uploads; "
    "an upload already completing may finish."
)

COMMAND_HELP = """You can use ordinary language in Slack or email: "What projects can I access?",
"Use my-project", "Create project my-project at gs://bucket/folder/", "Show the project guide",
"Include the scripts", or "Stop this analysis". Project creation needs an explicit data URI.
Access changes require a private Slack confirmation; email offers that handoff.
Exact commands still work:

*Projects*
• `init <name> gs://bucket/folder/` — Create and select a project (DM).
• `switch` — List your projects (alias: `projects`).
• `switch <name or ID>` — Select a project for new threads (alias: `use`).
• `rename <name or ID> <new-name>` — Rename your project (owner, DM).
• `data-root <project> gs://bucket/folder/` — Change its default data location (owner, Slack DM,
confirmation required). You can also say "Set this project's default data folder to gs://bucket/folder/".
Email requests for this setting direct you to private Slack.
Existing threads keep their project. In channels, add `--force` to replace a selection.

*Sharing — owner, DM*
• `share <project> @user-or-email` — Give someone project access.
Email recipients need no Slack account or channel publication; they email from the shared address
and ask to list/select the project. This does not send an invitation email.
• `unshare <project> @user` — Remove someone's project access.
• `publish <project> #channel` — Allow replies visible to that channel.
• `unpublish <project> #channel` — Stop replies in that channel.

*Project guide and tasks*
• `brief` — Show the current shared project guide.
Guide corrections save automatically when you describe them in ordinary chat.
• `cancel` — Stop queued or running computation.

*Script attachments*
• `scripts` — Show your setting (off by default).
• `scripts on` / `scripts off` — Attach scripts you run, or stop attaching them (DM or channel).

*Project memory*
• `memory` — Show the project index and current work.
• `memory <file>` — Read an entry, such as `memory conventions.md`.
• `brief` — Show `PROJECT.md`.
Mica remembers and tidies project memory automatically. Tell it corrections in plain language.
Anything said about a project, in a DM or a channel, may be remembered for everyone using that
project, and repository readers can see it.

*GCS writes — DM*
• `write grant <project> gs://bucket/folder/ [duration]` — Allow writing files.
• `write status <project>` — Show the destination and expiry.
• `write revoke <project>` — Stop future uploads.
Use `30m`, `1h`, or `7d`; omit duration for no expiry (until revoked).
New files need no extra confirmation. Confirm overwrites before each run; no separate grant.
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
