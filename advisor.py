"""
advisor.py — "Occasioni mancate": for each user turn, decide whether an
available-but-not-invoked skill would have been relevant.

Pipeline (cheap → expensive), so the LLM judge only ever sees strong candidates:

  1. user_turns()          — segment transcripts into turns + tools-used (parser.py)
  2. _prefilter()          — LOCAL, free: keyword/domain overlap between the prompt
                             and each available skill NOT used in the turn. Turns
                             with no candidate are settled locally (no LLM).
  3. _judge_batch()        — the surviving turns are batched into ONE `claude -p`
                             call (json-schema output). The Claude Code system-prompt
                             boot (~27k tokens) dominates per-call cost, so batching
                             is what keeps this cheap.
  4. cache (advisor-cache) — every turn judged exactly once, keyed by turn_uuid.

Judgment is retrospective and observational only — it never changes what the model
does live. Precision over recall: better a few true suggestions than many noisy ones.
"""

import os
import re
import json
import subprocess

import parser as tx_parser
import catalog

HOME = os.path.expanduser("~")
CACHE_PATH = os.path.join(HOME, ".claude", "skill-analytics", "advisor-cache.json")
CACHE_VERSION = 1

# Judge model + thresholds
JUDGE_MODEL = "claude-haiku-4-5-20251001"
CONFIDENCE_THRESHOLD = 0.7      # keep a suggestion only at/above this
BATCH_SIZE = 12                 # turns per `claude -p` call (amortizes the boot cost)
JUDGE_TIMEOUT = 180             # seconds per batch call
MIN_PROMPT_CHARS = 15           # ignore trivially short prompts ("ok", "si", "grazie")

# Pre-filter tuning
PREFILTER_MIN_HITS = 2          # min distinct skill-keywords that must appear in the prompt
PREFILTER_MAX_CANDIDATES = 4    # cap candidate skills shown to the judge per turn

# Words too generic to carry signal when matching prompt <-> skill description.
_STOP = set("""
the a an and or of to in on for with without your you it this that these those is are be
use used using use when do does not no user asks about into over via per the una uno il lo la
le gli dei del di da in con per che non come cosa quando se già sono è ho hai fare fai puoi
skill skills trigger triggers when use do want need create build make get set list show run
salesforce sf task tasks file files code changes change project org data help me mi
""".split())

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-]{2,}")


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
            if (isinstance(data, dict) and data.get("version") == CACHE_VERSION
                    and isinstance(data.get("turns"), dict)):
                return data
    except (OSError, ValueError):
        pass
    return {"version": CACHE_VERSION, "turns": {}}


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


def _tokens(text):
    return {w.lower() for w in _WORD_RE.findall(text or "") if w.lower() not in _STOP}


def _skill_keywords(skill):
    """Signal tokens for a skill: its name parts + the description, with the
    TRIGGER clause weighted (counted twice) since that's the intent signal."""
    desc = skill.get("description") or ""
    name = (skill.get("name") or "").replace(":", " ").replace("-", " ")
    trigger = ""
    m = re.search(r"TRIGGER\b(.*?)(DO NOT TRIGGER|SKIP|$)", desc, re.IGNORECASE | re.DOTALL)
    if m:
        trigger = m.group(1)
    return _tokens(name) | _tokens(desc) | _tokens(trigger)


# Build the available-skill keyword index once per process, per (project, domain).
_kw_index_cache = {}


def _skill_index(project, domain):
    key = (project or "all", domain or "all")
    if key in _kw_index_cache:
        return _kw_index_cache[key]
    skills = catalog.available_skills(
        project_cwd=(project if project and project != "all" else None),
        domain=domain,
    )
    index = [(s, _skill_keywords(s)) for s in skills]
    _kw_index_cache[key] = index
    return index


# Pseudo-prompts that aren't real user intent: harness/system injections and
# command echoes. No point asking "should a skill have run here?".
_SYSTEM_PROMPT_PREFIXES = (
    "<task-notification", "<local-command-stdout", "<command-name",
    "<command-message", "<system-reminder", "[system", "caveat:",
    "<user-memory", "<bash-", "<local-command",
)


def _is_real_prompt(prompt):
    p = (prompt or "").strip()
    if len(p) < MIN_PROMPT_CHARS:
        return False
    low = p.lower()
    return not any(low.startswith(pre) for pre in _SYSTEM_PROMPT_PREFIXES)


def _prefilter(turn, skill_index):
    """Local, free candidate finder. Returns a ranked list of candidate skills
    (dicts) whose keywords overlap the prompt strongly and that were NOT used in
    the turn. Empty list => nothing to ask the judge."""
    prompt = turn.get("prompt") or ""
    if not _is_real_prompt(prompt):
        return []
    ptoks = _tokens(prompt)
    if not ptoks:
        return []
    used = set(turn.get("used_skills") or [])
    # normalize used names (handle namespaced plugin:skill and folder forms)
    used_norm = {u.split(":")[-1] for u in used} | used

    scored = []
    for skill, kws in skill_index:
        nm = skill.get("name") or ""
        if nm in used or nm.split(":")[-1] in used_norm:
            continue
        hits = ptoks & kws
        if len(hits) >= PREFILTER_MIN_HITS:
            scored.append((len(hits), skill, sorted(hits)))
    scored.sort(key=lambda x: -x[0])
    out = []
    for hits_n, skill, hits in scored[:PREFILTER_MAX_CANDIDATES]:
        out.append({
            "name": skill.get("name"),
            "description": (skill.get("description") or "")[:300],
            "hits": hits,
            "score": hits_n,
        })
    return out


def _build_judge_prompt(batch):
    """One prompt covering several turns; the judge returns one verdict per turn."""
    lines = [
        "Sei un revisore che valuta l'uso delle Skill in Claude Code.",
        "Per OGNI turno qui sotto: l'utente ha scritto un messaggio e sono elencate",
        "alcune Skill DISPONIBILI ma NON invocate in quel turno. Stabilisci se una di",
        "quelle skill sarebbe stata chiaramente pertinente e utile per rispondere al",
        "messaggio. Sii SEVERO: rispondi relevant=true solo se il match è netto, non",
        "per vaga attinenza tematica. Rispondi SOLO con l'array JSON richiesto.",
        "",
    ]
    for i, t in enumerate(batch):
        lines.append(f"### Turno {i} (id={t['turn_uuid']})")
        lines.append(f"Messaggio utente: {t['prompt'][:500]}")
        lines.append("Skill candidate non usate:")
        for c in t["candidates"]:
            lines.append(f"  - {c['name']}: {c['description']}")
        lines.append("")
    return "\n".join(lines)


_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turn_uuid": {"type": "string"},
                    "relevant": {"type": "boolean"},
                    "skill": {"type": "string"},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["turn_uuid", "relevant", "confidence"],
            },
        }
    },
    "required": ["verdicts"],
}


def _judge_batch(batch):
    """Call `claude -p` once for a batch of turns. Returns {turn_uuid: verdict}."""
    prompt = _build_judge_prompt(batch)
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(_JUDGE_SCHEMA),
        "--model", JUDGE_MODEL,
        # The judge only reasons over text — deny all tools so it can't touch the fs.
        "--disallowedTools", "Bash", "Edit", "Write", "Read", "Glob", "Grep",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=JUDGE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if res.returncode != 0:
        return {}
    try:
        payload = json.loads(res.stdout)
    except (ValueError, json.JSONDecodeError):
        return {}
    structured = payload.get("structured_output")
    if not isinstance(structured, dict):
        return {}
    out = {}
    for v in structured.get("verdicts", []):
        if isinstance(v, dict) and v.get("turn_uuid"):
            out[v["turn_uuid"]] = v
    return out


def analyze(project=None, domain="all", max_judge_turns=48, dry_run=False,
            use_cache=True):
    """
    Run the advisor pipeline.

    Returns a summary dict:
      {turns_total, already_cached, candidates (turns surviving prefilter),
       judged, llm_calls, missed:[...], skipped_over_cap}
    dry_run=True stops after the prefilter (no `claude -p`, zero cost) — use it to
    size the backfill before spending anything.
    """
    turns = tx_parser.user_turns(project=project, domain=domain)
    cache = _load_cache() if use_cache else {"version": CACHE_VERSION, "turns": {}}
    cached = cache["turns"]

    dom_for_index = domain if domain in ("sf", "general") else "all"

    pending = []          # turns needing a judgment this run
    already_cached = 0
    seen_prompts = set()  # dedup identical turns (sub-agent transcripts echo them)
    for t in turns:
        tid = t.get("turn_uuid")
        if not tid:
            continue
        if use_cache and tid in cached:
            already_cached += 1
            continue
        dedup_key = ((t.get("prompt") or "")[:200], t.get("project"))
        if dedup_key in seen_prompts:
            continue
        seen_prompts.add(dedup_key)
        index = _skill_index(project, tx_parser.project_domain(
            tx_parser.project_root(t.get("project"))) if dom_for_index == "all" else dom_for_index)
        cands = _prefilter(t, index)
        if not cands:
            # Settle locally: no relevant unused skill. Cache as "clean".
            cached[tid] = {"relevant": False, "settled": "prefilter",
                           "ts": t.get("ts")}
            continue
        proj_root = tx_parser.project_root(t.get("project"))
        pending.append({
            "turn_uuid": tid,
            "ts": t.get("ts"),
            "prompt": t.get("prompt"),
            "project": proj_root,
            "project_name": t.get("project_name"),
            "domain": tx_parser.project_domain(proj_root),
            "candidates": cands,
        })

    summary = {
        "turns_total": len(turns),
        "already_cached": already_cached,
        "candidates": len(pending),
        "judged": 0,
        "llm_calls": 0,
        "skipped_over_cap": 0,
        "missed": [],
    }

    if dry_run:
        # Persist the "clean" prefilter settlements so a later real run is cheaper,
        # but do NOT call the LLM.
        if use_cache:
            _save_cache(cache)
        summary["candidates_detail"] = pending
        return summary

    # Cap the number of turns judged per run so a first backfill can't spike.
    if max_judge_turns and len(pending) > max_judge_turns:
        summary["skipped_over_cap"] = len(pending) - max_judge_turns
        pending = pending[:max_judge_turns]

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        verdicts = _judge_batch(batch)
        summary["llm_calls"] += 1
        for t in batch:
            tid = t["turn_uuid"]
            v = verdicts.get(tid)
            if not v:
                # Judge failed to rule on this turn — leave uncached so it retries.
                continue
            summary["judged"] += 1
            relevant = bool(v.get("relevant")) and \
                float(v.get("confidence") or 0) >= CONFIDENCE_THRESHOLD
            entry = {
                "relevant": relevant,
                "skill": v.get("skill"),
                "confidence": v.get("confidence"),
                "reason": v.get("reason"),
                "prompt": t["prompt"],
                "project": t.get("project"),
                "project_name": t["project_name"],
                "domain": t.get("domain"),
                "ts": t["ts"],
            }
            cached[tid] = entry

    if use_cache:
        _save_cache(cache)

    summary["missed"] = missed(project=project, domain=domain, cache=cache)
    return summary


def missed(project=None, domain="all", limit=60, cache=None):
    """Return cached, confident missed-opportunity suggestions, newest first.
    Filtered by project root and project-domain, matching the dashboard tabs."""
    cache = cache or _load_cache()
    out = []
    for tid, e in cache.get("turns", {}).items():
        if not isinstance(e, dict) or not e.get("relevant"):
            continue
        if project and project != "all" and e.get("project") != project:
            continue
        if domain and domain != "all" and e.get("domain") != domain:
            continue
        out.append({
            "turn_uuid": tid,
            "skill": e.get("skill"),
            "confidence": e.get("confidence"),
            "reason": e.get("reason"),
            "prompt": e.get("prompt"),
            "project_name": e.get("project_name"),
            "ts": e.get("ts"),
        })
    out.sort(key=lambda x: (x.get("ts") or ""), reverse=True)
    if limit and limit > 0:
        out = out[:limit]
    return out


if __name__ == "__main__":
    import sys
    dry = "--run" not in sys.argv
    s = analyze(dry_run=dry, use_cache="--no-cache" not in sys.argv)
    print("=== advisor", "DRY-RUN (prefilter only)" if dry else "FULL RUN", "===")
    print("turni totali          :", s["turns_total"])
    print("già in cache          :", s["already_cached"])
    print("candidati (post-filtro):", s["candidates"])
    if not dry:
        print("giudicati             :", s["judged"])
        print("chiamate LLM          :", s["llm_calls"])
        print("saltati (oltre cap)   :", s["skipped_over_cap"])
        print("occasioni mancate     :", len(s["missed"]))
    if dry and s.get("candidates_detail"):
        print("\n--- esempi di candidati che andrebbero al giudice ---")
        for t in s["candidates_detail"][:12]:
            names = ", ".join(c["name"] for c in t["candidates"])
            print(f"[{t['project_name']}] {(t['prompt'] or '')[:70]!r}")
            print(f"    -> {names}")
