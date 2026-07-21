# Skill Analytics — Claude Code

A local, zero-dependency dashboard that parses your [Claude Code](https://claude.com/claude-code)
session transcripts and shows how often you actually use **Skills** vs **MCP tools**,
plus a used-vs-available **coverage matrix** and a **live log** of every invocation.

It doubles as a health probe: if fresh rows keep appearing in the log panel while
you work, the whole parsing pipeline is healthy.

## What it does

- Reads the transcript `*.jsonl` files under `~/.claude/projects/**` (the only
  authoritative data source) and normalizes every `Skill` and `mcp__*` tool call
  into an event, deduplicated by tool-use id.
- Aggregates counts (top skills, top MCP tools, per-server, daily timeline).
- Builds a **coverage matrix**: which of the skills available to you (global,
  plugin, built-in, project) are actually being invoked — the never-used rows are
  the ones worth activating.
- Splits everything across three tabs — **Salesforce / General / Combined** — and a
  per-project filter.
- Streams a **live log** on the right rail: each invocation with its triggering
  prompt, the session title, the project, and a relative timestamp.
- **Missed-opportunity advisor**: flags turns where a relevant skill was available
  but not invoked. A cheap local prefilter (keyword/domain overlap) narrows
  candidates, then an LLM judge (`claude -p`, headless, batched) rules strictly on
  each — precision over recall. Retrospective and observational only; it never
  changes what the model does live. Runs in a background thread, cost-capped, with
  every turn judged exactly once (cached by turn id). Disable with `SKILL_ADVISOR=0`.

Everything is served by a single Python `http.server` and rendered with vanilla JS.
**No third-party Python packages.** PM2 is optional (only for keeping it running).

> **Note on retention:** Claude Code prunes old transcripts (~30 days), so the
> dashboard is a *moving snapshot*, not a full historical archive.

## Run

```bash
python3 server.py            # http://127.0.0.1:8787
python3 server.py 9000       # custom port
```

Or keep it always-on with [PM2](https://pm2.keymetrics.io/):

```bash
pm2 start ecosystem.config.js
pm2 logs skill-analytics
pm2 restart skill-analytics   # after editing the code
```

## Live auto-refresh (optional)

`hook.py` is a `PostToolUse` hook that bumps a `live-signal` marker whenever a
Skill or MCP tool runs, so the UI can auto-refresh within a few seconds. Wire it in
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Skill|mcp__",
        "hooks": [{ "type": "command", "command": "python3 ~/.claude/skill-analytics/hook.py" }] }
    ]
  }
}
```

The hook is **not** a data source — the dashboard always reads from the `.jsonl`
transcripts. It just nudges the front-end to re-poll.

## Layout

| File | Role |
|------|------|
| `parser.py` | Reads/dedups transcripts, mtime-cached; aggregates, `recent_events()` (log), `user_turns()` (advisor). |
| `catalog.py` | Enumerates available skills (global / plugin / built-in / project). |
| `advisor.py` | Missed-opportunity pipeline: prefilter → `claude -p` judge (batched) → verdict cache. Runnable standalone (`python3 advisor.py` for a free dry-run, `--run` to judge). |
| `server.py` | `http.server` + JSON API (`/api/data`, `/api/log`, `/api/missed`, `/api/heartbeat`) + background advisor thread. |
| `index.html` | Vanilla-JS UI (12-col dashboard + live log rail). |
| `hook.py` | Optional PostToolUse hook for live refresh. |
| `ecosystem.config.js` | PM2 process config. |

## Customizing the Salesforce / General split

The SF-vs-General classification is heuristic and lives at the top of `parser.py` —
edit `SF_ROOT`, `SF_MCP_SERVERS`, and `SF_SKILL_KEYWORDS` to match your own setup
(or collapse everything into one domain). These are personal conventions, not
required config.

## License

MIT
