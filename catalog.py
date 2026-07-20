"""
catalog.py — Enumerate the universe of *available* skills so the dashboard can
compare used-vs-available and list never-used skills.

Sources (union):
  1. Global   : ~/.claude/skills/*/SKILL.md  (symlinks resolve into ~/.agents/skills)
  2. Plugin   : <plugin cache>/skills/*/SKILL.md, filtered to enabledPlugins,
                namespaced as "<plugin>:<skill>"
  3. Project  : <cwd>/.claude/skills/*/SKILL.md  (only when filtering that project)

Each SKILL.md carries YAML-ish frontmatter with `name:` and `description:`.
"""

import os
import glob
import json

import parser as tx_parser  # reuse the SF-domain heuristic (single source of truth)

HOME = os.path.expanduser("~")


def skill_domain(name_or_folder):
    """'sf' | 'general' for a skill, via the same keyword heuristic as the parser."""
    return "sf" if tx_parser._skill_name_is_sf(name_or_folder) else "general"
CLAUDE_DIR = os.path.join(HOME, ".claude")
GLOBAL_SKILLS = os.path.join(CLAUDE_DIR, "skills")
PLUGINS_DIR = os.path.join(CLAUDE_DIR, "plugins")
SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
INSTALLED_PLUGINS = os.path.join(PLUGINS_DIR, "installed_plugins.json")
PLUGIN_CACHE = os.path.join(PLUGINS_DIR, "cache")


def _parse_frontmatter(path):
    """Minimal frontmatter reader: returns dict with name/description if present."""
    name = None
    desc = None
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("name:") and name is None:
            name = line[len("name:"):].strip().strip('"').strip("'")
        elif line.startswith("description:") and desc is None:
            desc = line[len("description:"):].strip().strip('"').strip("'")
    return {"name": name, "description": desc}


def _skill_from_dir(skill_dir, scope, namespace=None):
    md = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(md):
        return None
    fm = _parse_frontmatter(md)
    folder = os.path.basename(skill_dir.rstrip("/"))
    base = fm.get("name") or folder
    full = f"{namespace}:{base}" if namespace else base
    return {
        "name": full,
        "folder": folder,
        "scope": scope,
        "description": (fm.get("description") or "")[:300],
    }


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _enabled_plugins():
    """Return set of enabled plugin keys (e.g. 'chrome-devtools-mcp')."""
    settings = _load_json(SETTINGS) or {}
    enabled = settings.get("enabledPlugins") or {}
    keys = set()
    for k, v in enabled.items():
        if not v:
            continue
        # keys look like "chrome-devtools-mcp@claude-plugins-official"
        keys.add(k.split("@", 1)[0])
    return keys


def _global_skills():
    out = []
    for skill_dir in sorted(glob.glob(os.path.join(GLOBAL_SKILLS, "*"))):
        if not os.path.isdir(skill_dir):
            continue
        s = _skill_from_dir(skill_dir, "global")
        if s:
            out.append(s)
    return out


def _plugin_skills():
    """Skills from enabled plugins, discovered under the plugin cache."""
    out = []
    enabled = _enabled_plugins()
    if not os.path.isdir(PLUGIN_CACHE):
        return out
    # cache/<marketplace>/<plugin>/<version>/skills/<skill>/SKILL.md
    for md in glob.glob(os.path.join(PLUGIN_CACHE, "*", "*", "*", "skills", "*", "SKILL.md")):
        parts = md.split(os.sep)
        try:
            idx = parts.index("skills")
        except ValueError:
            continue
        plugin = parts[idx - 2] if idx >= 2 else ""
        if enabled and plugin not in enabled:
            continue
        skill_dir = os.path.dirname(md)
        s = _skill_from_dir(skill_dir, "plugin", namespace=plugin)
        if s:
            out.append(s)
    # de-dup by name (multiple cached versions)
    seen = {}
    for s in out:
        seen[s["name"]] = s
    return list(seen.values())


def _project_skills(project_cwd):
    out = []
    if not project_cwd or project_cwd == "all":
        return out
    base = os.path.join(project_cwd, ".claude", "skills")
    for skill_dir in sorted(glob.glob(os.path.join(base, "*"))):
        if not os.path.isdir(skill_dir):
            continue
        s = _skill_from_dir(skill_dir, "project")
        if s:
            out.append(s)
    return out


# Skill "bundled" con il harness di Claude Code. NON sono enumerabili dal disco
# in modo affidabile: vivono in /private/tmp/claude-*/bundled-skills/<ver>/<hash>/
# e compaiono lì SOLO dopo essere state invocate, spesso senza SKILL.md. La lista
# autoritativa è quella iniettata nel system prompt. Qui la teniamo curata ed
# editabile (aggiorna se il harness ne aggiunge/rimuove). Tutte dominio "general".
BUILTIN_SKILLS = {
    "dataviz": "Design system-agnostic per grafici, dashboard e data viz corrette.",
    "verify": "Verifica end-to-end che una modifica faccia davvero ciò che deve.",
    "code-review": "Review del diff per bug di correttezza e semplificazioni.",
    "simplify": "Applica pulizie di riuso/semplificazione/efficienza al codice modificato.",
    "run": "Avvia e pilota l'app del progetto per vedere una modifica funzionante.",
    "init": "Inizializza la configurazione/onboarding del progetto.",
    "review": "Review generica.",
    "security-review": "Review di sicurezza del codice.",
    "loop": "Esegue un prompt/comando a intervalli ricorrenti.",
    "claude-api": "Reference Claude API / Anthropic SDK (modelli, prezzi, tool use, ecc.).",
    "update-config": "Configura l'harness di Claude Code via settings.json (hook, permessi, env).",
    "keybindings-help": "Personalizza le scorciatoie da tastiera (~/.claude/keybindings.json).",
    "fewer-permission-prompts": "Analizza i transcript e propone un'allowlist per ridurre i prompt di permesso.",
}


def _builtin_skills():
    return [
        {"name": n, "folder": n, "scope": "builtin", "description": (d or "")[:300]}
        for n, d in sorted(BUILTIN_SKILLS.items())
    ]


def _skill_matches_domain(s, domain):
    if not domain or domain == "all":
        return True
    # match on folder or namespaced tail
    key = s.get("folder") or s.get("name") or ""
    return skill_domain(key) == domain


def available_skills(project_cwd=None, domain="all"):
    """
    Full available-skill catalog for a given scope.
    Global + enabled-plugin skills are always included; project skills only when
    a specific project_cwd is provided. Optionally filtered by domain (sf/general).
    """
    catalog = {}
    for s in _global_skills():
        catalog[s["name"]] = s
    for s in _plugin_skills():
        catalog[s["name"]] = s
    for s in _builtin_skills():
        catalog.setdefault(s["name"], s)  # don't override a real on-disk skill
    for s in _project_skills(project_cwd):
        catalog[s["name"]] = s
    skills = sorted(catalog.values(), key=lambda s: s["name"])
    if domain and domain != "all":
        skills = [s for s in skills if _skill_matches_domain(s, domain)]
    return skills


def coverage(used_skill_names, project_cwd=None, domain="all"):
    """
    Build the used-vs-available coverage matrix.
    used_skill_names: iterable of skill names actually invoked (from parser).
    Matching is tolerant: a used name matches an available skill if it equals the
    full name, the folder name, or the namespaced tail.
    """
    avail = available_skills(project_cwd, domain=domain)
    used = set(used_skill_names or [])

    # Build lookup of used names by their tail (after ':') too.
    used_tails = set()
    for u in used:
        used_tails.add(u)
        if ":" in u:
            used_tails.add(u.split(":", 1)[1])

    rows = []
    matched_used = set()
    for s in avail:
        candidates = {s["name"], s["folder"]}
        if ":" in s["name"]:
            candidates.add(s["name"].split(":", 1)[1])
        is_used = bool(candidates & used_tails)
        if is_used:
            matched_used |= (candidates & used_tails)
        rows.append({
            "name": s["name"],
            "scope": s["scope"],
            "description": s["description"],
            "used": is_used,
        })

    # Used skills that we couldn't match to any available catalog entry.
    unmatched = sorted(u for u in used if u not in matched_used
                       and (u.split(":", 1)[1] if ":" in u else u) not in matched_used)

    never_used = [r for r in rows if not r["used"]]
    used_rows = [r for r in rows if r["used"]]
    return {
        "available_count": len(avail),
        "used_count": len(used_rows),
        "never_used_count": len(never_used),
        "rows": rows,
        "never_used": never_used,
        "unmatched_used": unmatched,
    }


if __name__ == "__main__":
    import sys
    avail = available_skills()
    print("available (global+plugin):", len(avail))
    scopes = {}
    for s in avail:
        scopes[s["scope"]] = scopes.get(s["scope"], 0) + 1
    print("by scope:", scopes)
    # Optionally pass a project cwd to include its .claude/skills too.
    if len(sys.argv) > 1:
        proj = available_skills(sys.argv[1])
        print(f"available incl. {sys.argv[1]}:", len(proj))
