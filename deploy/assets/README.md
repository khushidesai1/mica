# Slack loading circle

Upload `pistachio-loading.gif` as the workspace emoji `pistachio-loading`, then
set `AGENT_LOADING_EMOJI=:pistachio-loading:` on the existing Cloud Run service.
The API forwards this setting to new Slack workers.

The GIF has 24 frames, a transparent background, and a 128×128 canvas. It is
used for startup and compact fallback statuses. During computation, Slack's
native expandable plan supplies its own circular progress indicator.

The emoji is installed in the azizilab workspace. Other workspaces must upload
it before using this setting; the generic application default remains available.
