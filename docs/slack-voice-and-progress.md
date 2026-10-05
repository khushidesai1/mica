# Slack Agent: Casual Voice and Claude-Style Progress

Written 3 October 2026 for an AI coding assistant (Codex) to implement. It covers three small
changes to the Slack analysis agent in `src/pistachio/agents/analysis/`:

1. **Voice:** replies are relaxed and conversational, and short (a sentence or two plus a few
   bullets) instead of long blocks of text.
2. **Progress:** the status message shows a loading icon and the current step while the agent
   works. Step details stay in small grey text, so the answer is what stands out, as in Claude.
3. **No timers:** remove `Done in 2m 14s` and the `— working (1m 30s)…` heartbeat text.

---

## 0. Instructions for the implementer (read first)

1. **Keep the diff small.** The target is about 150 changed lines including tests. These areas
   don't change: routing (`reply` vs `compute`), access control, sessions, checkpoints,
   provenance, brief drafting/saving, Batch lifecycle, file uploads, and memory.
2. **No new dependencies, scopes, endpoints, or manifest changes.** Use only `chat.postMessage`
   and `chat.update`, which the app already uses, with Block Kit `blocks`. Do not upgrade
   `slack-sdk`. Do not use `chat.startStream` or `assistant.threads.setStatus` (see section 6).
3. **Model-derived text never goes into a `mrkdwn` element.** Step labels come from the model
   (Bash `description`), so render them as `plain_text`. That way `<!channel>`, links and
   formatting in them are never interpreted.
4. Finish with the CI commands passing:
   `ruff check src tests`, `mypy src/pistachio`, `pytest`, `python -m compileall -q src`.

---

## 1. What the user sees (before → after)

**Before (compute request):**

```
Pistachio: Run spatial plotting script — working (2m 30s)…       ← edited in place
           ...later: Done in 3m 2s
Pistachio: Data: `processed/liver_B_qc.h5ad` (sample B, 612,408 cells)
           I loaded the backed AnnData object and extracted the obs columns ... (15 lines)
           Choices: ... (paragraph)  Caveats: ... (paragraph)
           Reply with more details or corrections and I'll revise this draft. Reply `save brief`
           when you want new threads to use it. No Markdown upload needed.
```

**After:**

```
Pistachio: ⏳ Run spatial plotting script                         ← small grey, edited in place
           Reading PROJECT.md · Inspecting liver_B_qc.h5ad       ← smaller grey, recent steps
           ...when finished, this same message becomes one grey line:
           ✓ Inspecting liver_B_qc.h5ad · Writing spatial.py · Run spatial plotting script
Pistachio: Here's sample B by cell type. Hepatocytes dominate the periportal zone and Kupffer
           cells are pretty evenly spread.
           • Used `processed/liver_B_qc.h5ad` (612,408 cells)
           • 0.2 px points, rasterized
📎 spatial_celltypes_B.png
```

For a **reply** request (no compute), the status message shows `⏳ Thinking…` and is then
replaced in place by the short answer. That path already works; only the styling changes.

The spinner is a Slack emoji (see section 4.1). An animated GIF emoji gives a real spinning
loader. There is no percentage progress bar because the agent has no reliable signal for
"percent done". Like Claude, it shows a spinner and the current action instead.

---

## 2. Change 1: Casual, concise voice

Only prompt text and a few fixed strings change. Keep every safety and scientific rule, such as
exact numbers, never inventing results, stating result-changing choices, and never claiming a
shared save. They should just be written more briefly.

### 2.1 `prompts.py` (compute agent system prompt, `SYSTEM`)

- **Opening line:** replace `You are the lab's data analysis assistant, working in a Slack
  thread for the project described in the project brief below.` with:
  `You are Pistachio, the lab's friendly data analysis helper, working in a Slack thread for the
  project described in the project brief below.`
- **"Choosing data", last bullet:** replace the `Start analysis replies with the data you used,
  for example: "Data: ..."` bullet with:
  ```
  - Mention the data you used in one short line, for example:
    "Used `processed/liver_B_qc.h5ad` (sample B, 612,408 cells)".
  ```
- **"Maintaining the project brief", the bullet starting `Use the revised draft…`:** replace with:
  ```
  - Use the revised draft for this thread's subsequent work. Summarize what changed in a few
    short bullets and name the biggest open question, if any. Don't explain how to save the
    brief; the app adds that note itself. Only the application's save command publishes the
    shared brief; do not claim it was saved globally by writing the draft.
  ```
  (The app already posts a save-brief nudge after a draft changes, `slack_app.py:177-182`, so
  the model repeating it doubled the text.)
- **"Figures and scientific choices", the `State every choice…` bullet:** keep it, and add
  this sentence at the end: `List these compactly in one bullet (comma-separated), not a
  paragraph each.`
- **Replace the whole `## Replying in Slack` section** with:
  ```
  ## Replying in Slack
  - Sound like a friendly, relaxed labmate, not a report. Plain words and contractions are
    fine. Skip filler like "Certainly!", "Great question", or "I hope this helps".
  - Lead with the answer, or what the figure shows, in one or two sentences.
  - Then add at most 3–5 short bullets for what matters: data used, key numbers, choices that
    change the result, caveats. Leave out anything the user doesn't need.
  - Aim for about 8 lines or fewer. Go longer only when the user asks for detail.
  - Don't narrate your process or list the steps you took; Slack already shows them.
  - Use Slack formatting: *bold*, bullet lists, `code`. No markdown headings or tables.
  - Report numbers exactly as computed. Never describe a result you didn't compute.
  - If something fails, say what broke in a sentence and what you'd try next.
  - If you need something from the user, ask one short question.
  ```
- Leave `INIT` and `build_init` alone. They are currently unused.

### 2.2 `direct.py` (conversation/route system prompt, `SYSTEM`)

- Line 25: `You are Pistachio, a lab assistant in Slack.` →
  `You are Pistachio, a friendly, laid-back lab assistant in Slack.`
- Lines 44-46: replace `Tell users they can keep replying with details or use save brief;
  never claim a shared save occurred.` with `After a brief edit, end with one short line such
  as "Keep the details coming, or say save brief to share it." Never claim a shared save
  occurred.`
- Replace `Keep reply concise, Slack-formatted, under 3000 characters. No markdown headings or
  tables.` with:
  ```
  Write reply like a relaxed labmate: lead with the answer in a sentence or two, then a few
  short bullets only if needed. Usually under about 600 characters; never over 3000. Don't
  restate the request, and never paste the whole brief into reply (users can say brief to see
  it); summarize brief edits in 2-4 bullets. Slack formatting; no markdown headings or tables.
  ```
- Leave the `Decision` schema limits (`max_length=3500`) unchanged.

### 2.3 Fixed strings (exact replacements)

| Location | Old | New |
|---|---|---|
| `slack_app.py:178-181` (draft nudge) | `Reply with more details or corrections and I'll revise this draft. Reply \`save brief\` when you want new threads to use it. No Markdown upload needed.` | `Updated this thread's draft brief. Send more tweaks anytime, or say \`save brief\` to share it.` |
| `slack_app.py:76` | `I hit an error while running this. Please retry.` | `Sorry, something broke on my end. Mind trying again?` |
| `slack_app.py:201` | `I hit an error while running this: {result.error}.` | `That run stopped early (\`{result.error}\`). Want me to try again?` |
| `batch.py:543-544` (Spot failure) | `The Spot worker stopped before completing your request. The previous saved session is retained. Send a new message to retry.` | `The worker stopped before finishing. Your earlier progress is safe, so send another message to retry.` |

The status-message strings are listed in section 3.4. Don't change access-denial, command,
memory, or help strings. Tests check them, and they weren't part of the complaint.

---

## 3. Change 2: Claude-style status message

### 3.1 How it works today (for orientation)

One status message is posted per request and edited in place:

| Phase | Who updates it | Where |
|---|---|---|
| received / waiting / starting | control plane, `BatchBridge.notify(event_id, record, phase, text)` | `batch.py:141-167`, callers at `:303`, `:333`, `:445`, `:453`, `:521` |
| replying (reply route) | control plane, `notify(..., "replying", "Working on it…")`, then `finish()` replaces it with the answer (`phase="complete"`) | `direct.py:269`, `:105-110` |
| worker start | Spot worker, `chat_update` | `batch_worker.py:107-113` |
| running steps + heartbeat + final | Spot worker, `SlackAgent.run` | `slack_app.py:92-195` |

Step labels come from `runner.describe_tool()` (`runner.py:187-194`) through the `on_status`
callback. `update()` sends to Slack at most once every 5 s. `heartbeat()` rewrites the message
every 30 s with elapsed time.

### 3.2 New helpers in `slack_format.py`

Add two small functions that return Block Kit lists. Use only `context` blocks, which Slack
renders as small grey text. That keeps the status visually below the answer.

```python
def status_blocks(emoji: str, headline: str, steps: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Spinner + current action; earlier steps in grey. Labels are plain_text (never parsed)."""
    current = steps[-1] if steps else headline
    blocks: list[dict[str, Any]] = [{"type": "context", "elements": [
        {"type": "mrkdwn", "text": emoji},
        {"type": "plain_text", "text": current[:150], "emoji": False},
    ]}]
    if len(steps) > 1:
        blocks.append({"type": "context", "elements": [
            {"type": "plain_text", "text": " · ".join(s[:80] for s in steps[-4:-1]), "emoji": False}
        ]})
    return blocks


def done_blocks(steps: Sequence[str], error: str | None = None) -> list[dict[str, Any]]:
    """The collapsed 'thinking' line left above the answer. No duration."""
    text = f"⚠️ Stopped: {error}" if error else "✓ " + (" · ".join(s[:80] for s in steps[-3:]) or "Finished")
    return [{"type": "context", "elements": [{"type": "plain_text", "text": text[:300], "emoji": True}]}]
```

Notes:
- The `emoji` argument is app-controlled (a setting), so the `mrkdwn` element is safe. The model
  can't reach it.
- Always pass a `text=` fallback along with `blocks` (Slack uses it for notifications and
  accessibility). Use the headline or current step.
- Per the Slack docs for `chat.update`, a text-only update removes existing blocks. So `finish()`
  replacing the status with the answer as plain `text` needs no change. Don't pass
  `blocks=None` explicitly expecting it to "keep" blocks; slack-sdk drops `None` arguments.

### 3.3 Setting

In `config.py` `Settings`, add:

```python
loading_emoji: str = ":hourglass_flowing_sand:"  # AGENT_LOADING_EMOJI; use an animated custom emoji
```

In `batch.py` `job_spec()` `environment`, add `"AGENT_LOADING_EMOJI": settings.loading_emoji`
so the Spot worker uses the same emoji as the control plane.

### 3.4 Wire it in

**`batch.py` `BatchBridge.notify`:** add a module constant
`STATUS_PHASES = {"received", "waiting", "starting", "replying", "cancelling"}`. When `phase` is
in it, send `blocks=status_blocks(self.settings.loading_emoji, text)` along with `text=text`.
Do this for both `chat_update` and `chat_postMessage`. All other phases (`complete`, `failed`,
`cancelled`) stay text-only, so the final answer or error replaces the status cleanly. Keep the
`notice_text` dedup and the `finish()` delivery check exactly as they are; both compare `text`.

Change the status headlines (the `text` passed to `notify`):

| Location | Old | New |
|---|---|---|
| `batch.py:303` received | `Received your message. Queued for processing.` | `On it…` |
| `batch.py:333` waiting | `Received. Waiting for the earlier request in this thread to finish.` | `Got it, I'll start once the earlier request here finishes…` |
| `batch.py:445` waiting | `Received. All workers are busy; your request is queued.` | `All workers are busy right now. You're in the queue…` |
| `batch.py:453` starting | `Starting a Spot worker. This usually takes a few minutes; I’ll update you here.` | `Spinning up a compute worker (usually a few minutes)…` |
| `batch.py:521-522` starting | `Still starting the Spot worker ({minutes} min since receipt). Your request is queued; no need to resend it.` | `Still waiting on a worker ({minutes} min so far). No need to resend…` |
| `direct.py:269` replying | `Working on it…` | `Thinking…` |
| `batch.py:613` cancelling | unchanged text | unchanged, but now shows the spinner (phase `cancelling`) |

The minute count for a slow Spot startup is kept on purpose. It's the one place where elapsed
time reassures users that a queued request isn't stuck. It isn't a "Done in" timer.

**`batch_worker.py:107-113`:** the "worker started" update becomes
`text="Getting set up…", blocks=status_blocks(settings.loading_emoji, "Getting set up…")`.
Leave `Project access is unavailable.` as plain text.

**`slack_app.py` `SlackAgent.run`:**

1. Lines 92-101: the initial status (both the `notice_ts` branch and the `say(...)` fallback)
   uses `status_blocks(emoji, "Working on it…")` with `text="Working on it…"`. For the fallback,
   give `say()` an optional `blocks` argument rather than adding a second posting helper.
2. Replace `latest: str` with `steps: list[str]`, plus `shown = 0` (the number of steps last
   sent to Slack). Add a local `async def flush()` that sends
   `chat_update(..., text=steps[-1], blocks=status_blocks(emoji, "Working on it…", steps))`,
   sets `shown = len(steps)` and `last = time.monotonic()`, and logs and ignores Slack errors
   like today.
3. `update(text)`: keep the authorization check first. Append `text` to `steps` unless it
   equals `steps[-1]`. Call `flush()` only if 5 s have passed since `last` (the same throttle
   as today).
4. `heartbeat()`: keep the loop and its authorization early-return. Each tick, call `flush()`
   only if `len(steps) != shown`. It no longer sends anything time-based. Its only job is
   to show a step the 5 s throttle skipped while a long script runs. Lower `HEARTBEAT_SECONDS`
   to `10`.
5. Lines 185-195 (final status): delete `seconds = int(result.duration)`. Update the status
   with `text="Stopped" if result.error else "Finished"` and
   `blocks=done_blocks(steps, result.error)`.

Leave `TurnResult.duration` and its use in provenance alone. Only the Slack text goes away.

**`runner.py` `describe_tool`:** change the Bash fallback from the raw command to a generic
label, so shell commands never show up in Slack, and cap labels at 80 characters:
`return str(args.get("description") or "Running code")[:80]`.

---

## 4. Change 3: Remove the timers

Sections 3.4 step 4 and step 5 cover this. Afterwards, the only time shown in Slack is
the Spot startup minute count. Check with:
`grep -rn "Done in\|// 60}m\|working (" src/pistachio/agents/analysis` should return nothing.

### 4.1 Loading icon (deployment, no code)

- Default `:hourglass_flowing_sand:` (⏳) works everywhere but doesn't animate.
- For a real spinner, a workspace admin uploads an animated GIF as a custom emoji, for example
  `pistachio-loading` (Slack → *Customize workspace* → *Add emoji*). Then set
  `AGENT_LOADING_EMOJI=:pistachio-loading:` on the control-plane Cloud Run service, in the same
  place as `AGENT_MODEL`. `job_spec` forwards it to Spot workers.

---

## 5. Tests

Update:
- `tests/test_analysis_slack.py::test_receipt_status_reused_and_heartbeat_stops`: remove the
  `"— working"` and `startswith("Done")` assertions. Assert instead that:
  - every update uses `ts == "9.0"`;
  - the final update's `blocks[0]` text starts with `✓`;
  - no update's `text` or blocks matches `r"\d+m \d+s"` or contains `"Done in"`;
  - the heartbeat stops after completion (the existing call-count check).
- `tests/test_analysis_batch.py:441`: assert `"2 min so far"` instead of `"2 min since receipt"`.

Add (keep them short and use the existing fixtures):
- `slack_format`: `status_blocks(":x:", "h", ["a", "<!channel> b"])` puts the step labels only
  in `plain_text` elements, and `done_blocks([], "error_max_turns")` starts with `⚠️ Stopped`.
- `slack_app`: a step that is throttled (two `on_status` calls within 5 s, then a slow finish)
  is eventually sent by the heartbeat (monkeypatch `HEARTBEAT_SECONDS` as the existing test
  does).
- `BatchBridge.notify`: phase `"starting"` sends `blocks`; phase `"complete"` calls
  `chat_update` with no `blocks` argument.
- `describe_tool`: a Bash block without `description` returns `"Running code"`, not the command.

Prompt wording can't be unit-tested well. Check it by hand (section 7).

---

## 6. Deliberately not in this change: Slack's native agent streaming

Slack now has `chat.startStream` / `chat.appendStream` / `chat.stopStream`, with `task_update`
chunks and `task_display_mode: "plan"`. They render the collapsible step list closest to Claude.
`assistant.threads.setStatus` (with `loading_messages`) shows the native "is thinking…" shimmer.
Don't use either here:

- The status message is created by the control plane minutes before the Spot worker exists. A
  stream would have to be started by the worker as a new message, which changes the
  notice/`finish()` delivery logic this plan leaves alone.
- slack-sdk 3.39.0 only types `markdown_text` for streams, so `chunks` would go through
  `**kwargs` without validation. Streaming in channels also needs `recipient_user_id` and
  `recipient_team_id`.
- The docs don't say how long an idle stream lives, and a long script can run silently for many
  minutes.
- `setStatus` applies only to assistant (DM split-view) threads, and the status disappears after
  2 minutes with no message, which is too short for compute runs. It doesn't cover channel threads.

Revisit after this lands if the grey status line still feels too noisy.

---

## 7. Manual check in staging

Run each of these in a DM and in a channel thread (on desktop and mobile Slack):

1. **Reply route:** "what datasets are in this project?" shows `⏳ Thinking…`, which is then
   replaced in place by a casual answer of about 6 lines or fewer.
2. **Compute route:** "plot sample B colored by cell type". The status moves through
   `On it…` → `Spinning up…` → `Getting set up…` → live grey steps, and ends as one grey `✓ …`
   line. The answer leads with the result and has a few bullets. No `Done in`, no timers.
3. **Brief edit in compute:** only one short save-brief note appears, not two.
4. **Error:** with `AGENT_MAX_TURNS=1`, the status ends `⚠️ Stopped: error_max_turns` and a short
   "Want me to try again?" message follows.
5. **Custom emoji:** with `AGENT_LOADING_EMOJI` set to an animated emoji, it animates in both
   the control-plane and worker phases.

Deploy: rebuild the worker image (`cloudbuild/build-slack-agent.yaml`) and the API image as
usual. No Slack app reinstall or manifest change is needed.
