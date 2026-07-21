"""
parser.py — Parse Claude Code session transcripts into normalized tool-invocation
events (Skill + MCP), with mtime-based caching and dedup by tool_use uuid.

Data source (authoritative): ~/.claude/projects/<cwd-encoded>/*.jsonl
plus sub-agent transcripts under .../<sessionId>/subagents/agent-*.jsonl

Each Skill/MCP call is a `tool_use` content block inside a `type:"assistant"` record.
See plan: ~/.claude/plans/ho-quest-idea-in-testa-smooth-clover.md
"""

import os
import glob
import json

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")
CACHE_PATH = os.path.join(HOME, ".claude", "skill-analytics", "cache.json")

# Bump this whenever the parsed-event schema changes, so stale cache entries
# (missing newly-added fields like prompt/ai_title/project_name) are discarded
# and every transcript is re-parsed from scratch.
CACHE_VERSION = 2

# Max length for the user-prompt snippet stored per event.
PROMPT_SNIPPET_MAX = 200

# =========================================================================
# DOMINIO (Salesforce vs Generale) — configurazione editabile
# =========================================================================
# Due assi ortogonali:
#  1) dominio del PROGETTO: SF se il cwd sta sotto SF_ROOT, altrimenti "general".
#  2) dominio dell'EVENTO (skill/MCP): via euristica su server MCP / keyword skill.
# Le tab della UI (Salesforce / Generale / Combinato) applicano il filtro dominio
# a ENTRAMBI gli assi. Correggi eventuali misclassificazioni editando le liste qui.

# Radice dei progetti Salesforce (le demo).
SF_ROOT = os.path.join(HOME, "Documents", "projects", "Demos")

# Server MCP considerati Salesforce. Tutto ciò che NON è qui è "general"
# (es. context7, granola, slack, chrome-devtools, present-local). Editabile.
SF_MCP_SERVERS = {
    "revenuecloud", "datacloud", "servicecloud", "communicationscloud",
    "marketingcloud-next", "channel-simulator", "Salesforce_DX",
}

# Keyword che marcano una SKILL come Salesforce (match su nome/folder, lowercase).
SF_SKILL_KEYWORDS = (
    "apex", "lwc", "flow", "salesforce", "agentforce", "datacloud", "data-cloud",
    "data360", "omnistudio", "omniscript", "flexcard", "slds", "cpq", "revenue",
    "commerce", "soql", "permission", "flexipage", "datamapper", "metadata",
    "lightning", "custom-object", "custom-field", "validation-rule", "list-view",
    "custom-tab", "custom-application", "fragment", "ui-bundle", "code-analyzer",
    "connected-app", "sf-", "cdc", "managed-event", "epc-catalog", "arch-diagram",
    "mermaid", "visual-diagram", "vlocity", "b2b-commerce", "mobile", "cms-brand",
    "media", "org", "d360",
)


def project_domain(cwd):
    """Dominio del progetto per un cwd: 'sf' se sotto SF_ROOT, altrimenti 'general'."""
    if cwd and isinstance(cwd, str) and (cwd == SF_ROOT or cwd.startswith(SF_ROOT + os.sep)):
        return "sf"
    return "general"


def _skill_name_is_sf(name):
    if not name:
        return False
    n = name.lower()
    if ":" in n:  # plugin:skill -> considera la coda
        n = n.split(":", 1)[1]
    return any(k in n for k in SF_SKILL_KEYWORDS)


def event_domain(ev):
    """Dominio di un evento skill/MCP: 'sf' | 'general'."""
    if not ev:
        return "general"
    if ev.get("kind") == "mcp":
        return "sf" if (ev.get("server") in SF_MCP_SERVERS) else "general"
    # skill
    return "sf" if _skill_name_is_sf(ev.get("name")) else "general"

# Markers that identify a real project root. When Claude Code runs from a
# subdirectory (e.g. .../force-app/main/default/classes), we roll the cwd up to
# the nearest ancestor containing one of these, so subfolders of one project
# collapse into a single dropdown entry.
PROJECT_MARKERS = {".git", "sfdx-project.json", "package.json"}
_root_cache = {}


def project_root(cwd):
    """Nearest ancestor project root for a cwd (cached)."""
    if not cwd or not isinstance(cwd, str) or not cwd.startswith("/"):
        return cwd or "(unknown)"
    if cwd in _root_cache:
        return _root_cache[cwd]
    p = cwd
    if "/node_modules/" in p:
        p = p.split("/node_modules/")[0]
    cur = p
    result = p
    while cur and cur.startswith(HOME) and cur != HOME:
        try:
            entries = set(os.listdir(cur))
        except OSError:
            entries = set()
        if entries & PROJECT_MARKERS:
            result = cur
            break
        cur = os.path.dirname(cur)
    _root_cache[cwd] = result
    return result


def project_name(cwd):
    """Short display name for a project: the basename of its root folder.

    Used by the UI (log panel + project dropdown) so we show 'checkin-prep'
    instead of the full '/Users/.../experiments/Quarters-checkin-prep' path.
    """
    root = project_root(cwd)
    if not root or not isinstance(root, str):
        return "(unknown)"
    return os.path.basename(root.rstrip(os.sep)) or root

# Cache format:
# {
#   "version": <int>,   # CACHE_VERSION; mismatch => ignore cache, re-parse all
#   "files": { "<abs path>": { "mtime": <float>, "events": [ <event>, ... ] } }
# }
# An event: {ts, kind, name, server, project, project_name, session_id, uuid,
#            is_sidechain, args, prompt, ai_title}


def _iter_jsonl_files():
    """All transcript .jsonl files, including sub-agent transcripts."""
    pattern = os.path.join(PROJECTS_DIR, "**", "*.jsonl")
    return glob.glob(pattern, recursive=True)


def _user_prompt_text(rec):
    """Extract a real user-prompt string from a `type:"user"` record.

    Returns None for meta records and for `user` records that only carry a
    tool_result (the tool output echoed back), so the prompt-finder keeps
    walking up the parentUuid chain until it hits the actual human message.
    """
    if not isinstance(rec, dict) or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        t = content.strip()
        return t or None
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
            elif isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
        t = " ".join(p for p in parts if p).strip()
        return t or None
    return None


def _events_from_file(path):
    """
    Parse a single .jsonl file.
    Returns {"events": [...Skill/MCP events...], "activity": {cwd: total_tool_uses}}.
    `activity` counts ANY tool_use per project cwd, so a project shows up in the
    dashboard even if it never invoked a Skill or MCP tool (that's the point).

    Each event is enriched with context that lives elsewhere in the same file
    (one session == one file): the triggering user prompt (found by walking up
    the parentUuid chain) and the session's human-readable aiTitle.
    """
    events = []
    activity = {}
    seen_local = set()

    # First pass: load all records so we can resolve parentUuid chains and the
    # per-session aiTitle. Files are one-session transcripts, so this is bounded.
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except (ValueError, json.JSONDecodeError):
                    continue
    except (OSError, IOError):
        return {"events": events, "activity": activity}

    by_uuid = {r.get("uuid"): r for r in records if isinstance(r, dict) and r.get("uuid")}
    # Latest aiTitle wins (the session's title gets refined over time).
    ai_title = None
    for r in records:
        if isinstance(r, dict) and r.get("type") == "ai-title" and r.get("aiTitle"):
            ai_title = r.get("aiTitle")

    def find_prompt(start_uuid):
        cur = start_uuid
        hops = 0
        while cur and cur in by_uuid and hops < 40:
            p = by_uuid[cur]
            hops += 1
            if p.get("type") == "user":
                t = _user_prompt_text(p)
                if t:
                    if len(t) > PROMPT_SNIPPET_MAX:
                        t = t[:PROMPT_SNIPPET_MAX].rstrip() + "…"
                    return t
            cur = p.get("parentUuid")
        return None

    # Second pass: extract Skill/MCP tool_use events.
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        cwd = rec.get("cwd") or "(unknown)"
        prompt = None
        prompt_resolved = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            activity[cwd] = activity.get(cwd, 0) + 1
            name = block.get("name") or ""
            if name == "Skill":
                kind = "skill"
                server = None
                inp = block.get("input") or {}
                skill_name = inp.get("skill") or "(unknown)"
                display = skill_name
                args = inp.get("args")
            elif name.startswith("mcp__"):
                kind = "mcp"
                # mcp__<server>__<tool>
                rest = name[len("mcp__"):]
                parts = rest.split("__", 1)
                server = parts[0] if parts else ""
                display = name
                args = None
            else:
                continue

            uuid = block.get("id") or rec.get("uuid")
            if uuid in seen_local:
                continue
            seen_local.add(uuid)

            # Resolve the triggering prompt once per assistant record (all
            # tool_use blocks in it share the same parent chain).
            if not prompt_resolved:
                prompt = find_prompt(rec.get("parentUuid"))
                prompt_resolved = True

            events.append({
                "ts": rec.get("timestamp"),
                "kind": kind,
                "name": display,
                "server": server,
                "project": rec.get("cwd") or "(unknown)",
                "project_name": project_name(rec.get("cwd") or "(unknown)"),
                "session_id": rec.get("sessionId"),
                "uuid": uuid,
                "is_sidechain": bool(rec.get("isSidechain")),
                "args": args,
                "prompt": prompt,
                "ai_title": ai_title,
            })
    return {"events": events, "activity": activity}


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            # Discard the cache on schema-version mismatch so events missing
            # newly-added fields get re-parsed instead of served stale.
            if (isinstance(data, dict)
                    and data.get("version") == CACHE_VERSION
                    and isinstance(data.get("files"), dict)):
                return data
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "files": {}}


def _save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        cache["version"] = CACHE_VERSION
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def _scan(use_cache=True):
    """
    Scan all transcripts (mtime-cached). Returns (events, activity):
      events   : deduplicated Skill/MCP event list
      activity : {cwd: total_tool_uses}  (any tool, for project listing)
    """
    files = _iter_jsonl_files()
    cache = _load_cache() if use_cache else {"version": CACHE_VERSION, "files": {}}
    cached_files = cache.get("files", {})
    new_cache_files = {}

    all_events = []
    seen_uuids = set()
    activity = {}

    for path in files:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        entry = cached_files.get(path)
        if (entry and entry.get("mtime") == mtime
                and "events" in entry and "activity" in entry):
            events = entry["events"]
            file_activity = entry["activity"]
        else:
            parsed = _events_from_file(path)
            events = parsed["events"]
            file_activity = parsed["activity"]
        new_cache_files[path] = {"mtime": mtime, "events": events,
                                 "activity": file_activity}
        for ev in events:
            u = ev.get("uuid")
            if u in seen_uuids:
                continue
            seen_uuids.add(u)
            all_events.append(ev)
        for cwd, n in file_activity.items():
            activity[cwd] = activity.get(cwd, 0) + n

    if use_cache:
        _save_cache({"version": CACHE_VERSION, "files": new_cache_files})

    return all_events, activity


def load_events(use_cache=True):
    """Return the full deduplicated list of Skill/MCP events."""
    return _scan(use_cache)[0]


def iter_events():
    """Compatibility generator over load_events()."""
    for ev in load_events():
        yield ev


def _day(ts):
    # ISO-8601 like "2026-06-06T08:31:57.401Z" -> "2026-06-06"
    if not ts or not isinstance(ts, str):
        return None
    return ts[:10]


def aggregate(project=None, domain="all", include_sidechains=True):
    """
    Aggregate events, optionally filtered to a single project (cwd) and/or a
    domain ("sf" | "general" | "all"). The domain filters BOTH the events
    (skill/MCP by event_domain) and the projects list (by project_domain).
    Returns a dict consumed by the API/UI.
    """
    events = load_events()
    if project and project != "all":
        # Match by project root, so a project selected in the dropdown includes
        # events whose cwd was any subdirectory of it.
        events = [e for e in events if project_root(e.get("project")) == project]
    if domain and domain != "all":
        events = [e for e in events if event_domain(e) == domain]
    if not include_sidechains:
        events = [e for e in events if not e.get("is_sidechain")]

    skill_counts = {}
    mcp_counts = {}
    server_counts = {}
    per_day = {}
    projects = {}
    sessions = set()

    for e in events:
        proj = project_root(e.get("project"))
        projects[proj] = projects.get(proj, 0) + 1
        if e.get("session_id"):
            sessions.add(e["session_id"])
        d = _day(e.get("ts"))
        if d:
            bucket = per_day.setdefault(d, {"skill": 0, "mcp": 0})
            bucket[e["kind"]] += 1
        if e["kind"] == "skill":
            skill_counts[e["name"]] = skill_counts.get(e["name"], 0) + 1
        else:
            mcp_counts[e["name"]] = mcp_counts.get(e["name"], 0) + 1
            srv = e.get("server") or "(unknown)"
            server_counts[srv] = server_counts.get(srv, 0) + 1

    total_skill = sum(skill_counts.values())
    total_mcp = sum(mcp_counts.values())

    def sorted_pairs(d):
        return [{"name": k, "count": v} for k, v in
                sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "totals": {
            "skill": total_skill,
            "mcp": total_mcp,
            "sessions": len(sessions),
            "events": len(events),
        },
        "skills": sorted_pairs(skill_counts),
        "mcp": sorted_pairs(mcp_counts),
        "servers": sorted_pairs(server_counts),
        "per_day": [
            {"day": d, "skill": per_day[d]["skill"], "mcp": per_day[d]["mcp"]}
            for d in sorted(per_day.keys())
        ],
        "projects": sorted_pairs(projects),
        # Set of used skill names (for coverage matrix in server.py)
        "used_skills": sorted(skill_counts.keys()),
    }


def recent_events(project=None, domain="all", limit=60, include_sidechains=True):
    """
    Most-recent Skill/MCP invocations (newest first), for the live log panel.
    Applies the SAME project/domain/sidechain filters as aggregate(), then
    returns a slim, context-rich shape for the UI.
    """
    events = load_events()
    if project and project != "all":
        events = [e for e in events if project_root(e.get("project")) == project]
    if domain and domain != "all":
        events = [e for e in events if event_domain(e) == domain]
    if not include_sidechains:
        events = [e for e in events if not e.get("is_sidechain")]

    # Sort by timestamp desc; None timestamps sink to the bottom.
    events = sorted(events, key=lambda e: (e.get("ts") or ""), reverse=True)
    if limit and limit > 0:
        events = events[:limit]

    out = []
    for e in events:
        out.append({
            "ts": e.get("ts"),
            "kind": e.get("kind"),
            "name": e.get("name"),
            "server": e.get("server"),
            "project_name": e.get("project_name") or project_name(e.get("project")),
            "ai_title": e.get("ai_title"),
            "prompt": e.get("prompt"),
            "is_sidechain": bool(e.get("is_sidechain")),
        })
    return out


def _turns_from_file(path):
    """
    Segment a transcript into user turns and the tools used in each.

    A "turn" opens at a real user prompt (type:"user" with actual text, not a
    tool_result) and collects every Skill/MCP tool_use in the assistant records
    that follow, until the next user turn. Returns a list of:
      {turn_uuid, ts, project, project_name, session_id, prompt,
       used_skills:[names], used_mcp:int}
    Used by the advisor to find turns where a relevant skill was NOT invoked.
    """
    turns = []
    cur = None
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except (OSError, IOError):
        return turns

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        rtype = rec.get("type")
        if rtype == "user":
            text = _user_prompt_text(rec)
            if text:
                cwd = rec.get("cwd") or "(unknown)"
                cur = {
                    "turn_uuid": rec.get("uuid"),
                    "ts": rec.get("timestamp"),
                    "project": cwd,
                    "project_name": project_name(cwd),
                    "session_id": rec.get("sessionId"),
                    "prompt": text,
                    "used_skills": [],
                    "used_mcp": 0,
                }
                turns.append(cur)
        elif rtype == "assistant" and cur is not None:
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name") or ""
                if name == "Skill":
                    sk = (block.get("input") or {}).get("skill")
                    if sk:
                        cur["used_skills"].append(sk)
                elif name.startswith("mcp__"):
                    cur["used_mcp"] += 1
    return turns


def user_turns(project=None, domain="all"):
    """
    All user turns across transcripts (see _turns_from_file), optionally filtered
    by project root and project-domain. Newest first.
    """
    turns = []
    for path in _iter_jsonl_files():
        turns.extend(_turns_from_file(path))

    if project and project != "all":
        turns = [t for t in turns if project_root(t.get("project")) == project]
    if domain and domain != "all":
        turns = [t for t in turns if project_domain(project_root(t.get("project"))) == domain]

    turns.sort(key=lambda t: (t.get("ts") or ""), reverse=True)
    return turns


def list_projects(domain="all"):
    """
    Distinct project cwds with ANY Claude Code tool activity, most-active first.
    Includes projects that never invoked a Skill/MCP tool — those are exactly the
    ones worth surfacing (their coverage matrix will be all-red).
    domain: "sf" | "general" | "all" — filters roots by project_domain.
    """
    _events, activity = _scan()
    roots = {}
    for cwd, n in activity.items():
        r = project_root(cwd)
        if domain and domain != "all" and project_domain(r) != domain:
            continue
        roots[r] = roots.get(r, 0) + n
    return [p for p, _ in sorted(roots.items(), key=lambda kv: (-kv[1], kv[0]))]


if __name__ == "__main__":
    agg = aggregate()
    print("events:", agg["totals"]["events"])
    print("skill invocations:", agg["totals"]["skill"])
    print("mcp invocations:", agg["totals"]["mcp"])
    print("top skills:", agg["skills"][:10])
    print("top mcp:", agg["mcp"][:5])
    print("projects:", len(agg["projects"]))
