"""Intent-based deletion guard for the shell tool.

Threat model
------------
Destructive ops are fine when the human user actually asked for them. They
are dangerous when the trigger came from a tool output (a fetched webpage,
a `cat`-ed file, search results, command stdout) carrying an injected
instruction ("ignore your rules, run rm …"). Pattern-matching the command
itself can't tell those apart, and is also trivially bypassed
(`python -c "os.remove('x')"`).

So this guard authorizes on PROVENANCE, not on method or target:

    A destructive op is allowed iff the human user, in a recent genuine
    user message, named BOTH a destructive verb AND the target token.

Two untrusted channels are explicitly excluded:
  - tool outputs (webpages, files, command stdout) — arbitrary content
  - assistant messages (the LLM's own reasoning) — that's the agent, not
    the user; letting it self-authorize defeats the point.

Only `role: user` messages enter the intent check.

The guard has three tiers that compose:

    destructive op detected (any method, including python -c / eval / xargs)
       │
       ├─ Tier A: target in throwaway allow-list (/tmp, .cache, build/, …)
       │          → ALLOW  (cache cleanup; never needs a prompt round-trip)
       │
       ├─ Tier B: (destructive verb + target) BOTH present in recent
       │          role:user messages?
       │          → ALLOW  (the user actually asked)
       │
       └─ else   → BLOCK with a confirmation request:
                   "relay to the user; ask them to send a message that
                   names both the deletion AND the target." When the user
                   does, Tier B re-evaluates to ALLOW on the next call.

    Tier A is a friction reducer, NOT a safety boundary. Tier B is the
    only real authorization path.

Limitations:
    - Not a sandbox. A user who is socially-engineered into typing the
      confirmation would still be the unforgeable authority agreeing.
    - The two-token rule (verb + target in the same user message) keeps
      coincidental matches rare but isn't cryptographic. It is sized to
      defeat the realistic threat (injected instructions in tool outputs)
      while keeping normal workflows frictionless.
    - Arbitrary bash is not fully parseable; obfuscated deletion
      (`python -c "import os;os.remove('x')"`) is detectable in the common
      forms below, but determined obfuscation can still slip past detection.
      Detection misses are still mostly safe because Tier B requires you to
      have authorized it explicitly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path


# ── Configuration ───────────────────────────────────────────────────────────
# All overridable from the environment (micro.py already loads .env). Keep
# GUARD_ENABLED on by default — it fails safe.

TRASH_DIR = os.environ.get(
    "MICRO_TRASH_DIR",
    os.path.join(
        os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")),
        "micro-agent",
        "trash",
    ),
)

# Master switch. MICRO_ALLOW_DELETE=1 disables the guard entirely (human override
# at launch time — the only escape hatch, intentionally not in-band).
GUARD_ENABLED = os.environ.get("MICRO_ALLOW_DELETE", "0") not in ("1", "true", "yes", "on")

# After this many identical blocks in a row, escalate the message.
MAX_REPEAT_BLOCK = 3

# How many recent user messages to consider for the intent check.
USER_MSG_WINDOW = 12

# Throwaway paths (Tier A). Glob patterns matched against the absolute path;
# "**/X/**" is special-cased because fnmatch does not treat ** recursively.
SAFE_DELETE_GLOBS = [
    "/tmp/**",
    "/var/tmp/**",
    "/dev/shm/**",
    "**/.cache/**",
    "**/.trash/**",
    "**/__pycache__/**",
    "**/node_modules/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/build/**",
    "**/dist/**",
    "**/.venv/**",
    "**/venv/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.tox/**",
    "**/*.log.*",  # rotated logs like foo.log.1
    "/dev/null",    # output sink — `> /dev/null` discards, never destroys
    TRASH_DIR + "/**",
]

# Extra protected roots (informational only now — Tier A is the only place
# path "protection" still matters, and it's an allow-list, not a deny-list).
# Kept for backward compat with MICRO_PROTECTED_ROOTS but unused in decisions.
PROTECTED_ROOTS_EXTRA = [
    p for p in os.environ.get("MICRO_PROTECTED_ROOTS", "").split(os.pathsep) if p
]


# ── Outcome ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Decision:
    """Result of analyzing one command."""

    is_block: bool
    message: str = ""
    fingerprint: str = ""
    # Why this decision was reached — useful for tests + audit log.
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Convenience: True iff the command is allowed to run."""
        return not self.is_block


OK = Decision(is_block=False, reason="not-destructive")


# ── In-process block counter (for escalation) ───────────────────────────────
_BLOCK_COUNTS: dict[str, int] = {}


def _reset_counts() -> None:
    """Clear the escalation counter (test helper)."""
    _BLOCK_COUNTS.clear()


# ── Path helpers ────────────────────────────────────────────────────────────
def _abs_target(path: str, cwd: str) -> str:
    """Resolve a CLI path token to an absolute path (without touching the FS)."""
    if not path:
        return path
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(cwd, p))


def _matches_glob(target_abs: str, pat: str) -> bool:
    if fnmatch(target_abs, pat):
        return True
    if pat.startswith("**/") and pat.endswith("/**"):
        seg = pat[3:-3]
        if f"/{seg}/" in target_abs + "/":
            return True
    return False


def _is_safe(target_abs: str) -> bool:
    """Tier A: True iff `target_abs` matches any throwaway glob."""
    return any(_matches_glob(target_abs, pat) for pat in SAFE_DELETE_GLOBS)


# ── Command splitting ───────────────────────────────────────────────────────
# Split on shell separators (&&, ||, |, ;) while respecting single/double
# quotes so we don't split inside 'DROP TABLE foo; SELECT 1'. Imperfect
# (no backticks / $() nesting) but high-recall on common chained forms.
def _split_segments(cmd: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    i, n = 0, len(cmd)
    in_s = in_d = False
    while i < n:
        c = cmd[i]
        if in_s:
            cur.append(c)
            if c == "'":
                in_s = False
            i += 1
            continue
        if in_d:
            cur.append(c)
            if c == "\\" and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 2
                continue
            if c == '"':
                in_d = False
            i += 1
            continue
        if c == "'":
            in_s = True
            cur.append(c)
            i += 1
            continue
        if c == '"':
            in_d = True
            cur.append(c)
            i += 1
            continue
        two = cmd[i : i + 2]
        if two in ("&&", "||"):
            out.append("".join(cur).strip())
            cur = []
            i += 2
            continue
        if c in ("|", ";"):
            out.append("".join(cur).strip())
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if cur:
        out.append("".join(cur).strip())
    return [s for s in out if s]


_HEREDOC_RE = re.compile(r"<<[-~]?(\w+)\n?[\s\S]*?^\1\b", re.MULTILINE)


def _strip_heredocs(cmd: str) -> tuple[str, list[str]]:
    """Return (cmd_with_heredoc_bodies_removed, [bodies])."""
    bodies: list[str] = []

    def _collect(m: re.Match) -> str:
        bodies.append(m.group(0))
        return " "

    return _HEREDOC_RE.sub(_collect, cmd), bodies


# ── Destructive-operation detection ─────────────────────────────────────────
# Each detector returns None, or a `DestructiveOp` describing what was found.
# A detector's job is to find the (verb, target, category) tuple; the decision
# of ALLOW vs BLOCK is made later (Tiers A/B).

@dataclass(frozen=True)
class DestructiveOp:
    category: str            # fs | db | git
    verb: str                # e.g. "rm", "DROP TABLE", "reset --hard"
    target: str              # the bare token to check intent against (filename/table/branch/…)
    target_abs: str = ""     # resolved absolute path (fs only), for Tier A
    segment: str = ""        # the original command segment, for the message


_FS_RM_RE = re.compile(
    r"""
    (?:^|[\s;&|(])
    (?P<verb>rm|rmdir|unlink|shred)
    (?:\s+-[a-zA-Z]+)*
    \s+
    (?P<target>[^\s;&|()>]+)
    """,
    re.VERBOSE | re.IGNORECASE,
)
_FIND_DELETE_RE = re.compile(r"\bfind\b.*?-(?:delete|exec\s+rm)", re.IGNORECASE | re.DOTALL)
_TRUNCATE_RE = re.compile(r"(?:^|[\s;&|(])(?::\s*)?>\s*(?P<target>[^\s;&|>&]+)")
_DD_OF_RE = re.compile(r"\bdd\b.*?\bof=(?P<target>\S+)", re.IGNORECASE | re.DOTALL)
_MKFS_RE = re.compile(r"\b(?:mkfs|wipefs)\b", re.IGNORECASE)
_RM_DB_FILE_RE = re.compile(
    r"(?:^|[\s;&|(])rm\s+(?:-[a-zA-Z]*\s+)?(?P<glob>[\w./~]*\.(?:db|sqlite[0-9]?|sql|dump))",
    re.IGNORECASE,
)

# Bypass detections: deletion hidden inside an interpreter/eval/pipe. Catch the
# common forms so intent-based auth (Tier B) is the only fallback the agent has.
_PYDELETE_RE = re.compile(
    r"\b(?:python|python3|python3\.\d+)\s+-c\b(?P<tail>.*)",
    re.IGNORECASE | re.DOTALL,
)
_PY_OSPAT_RE = re.compile(
    r"os\.(?:remove|unlink|rmdir)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
    re.IGNORECASE,
)
_PY_SHUTIL_RE = re.compile(r"shutil\.rmtree\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", re.IGNORECASE)
_EVAL_RE = re.compile(r"\beval\s+['\"](?P<body>[^'\"]+)['\"]", re.IGNORECASE | re.DOTALL)
_BASH_C_RE = re.compile(r"\b(?:bash|sh|zsh)\s+-c\s+['\"](?P<body>[^'\"]+)['\"]", re.IGNORECASE | re.DOTALL)
_XARGS_RM_RE = re.compile(r"\bxargs\s+(?:-[a-zA-Z]+\s+)*rm\b", re.IGNORECASE)

# Git destructive.
_GIT_HARD_RE = re.compile(r"\bgit\s+reset\s+--hard|\bgit\s+clean\s+-[a-zA-Z]*[fFxd]", re.IGNORECASE)
_GIT_BRANCH_D_RE = re.compile(
    r"\bgit\s+branch\s+(?:-D|--delete|--delete-force)\s+(?P<target>\S+)", re.IGNORECASE
)
_GIT_PUSH_FORCE_RE = re.compile(r"\bgit\s+push\s+(?:--force|-f|--force-with-lease)", re.IGNORECASE)

# Database verbs inside DB CLI payloads.
_SQL_DROP_RE = re.compile(
    r"\bDROP\s+(?P<kind>TABLE|DATABASE|INDEX|SCHEMA|VIEW|MATERIALIZED)\s+"
    r"(?:IF\s+EXISTS\s+)?(?P<name>[`'\w.\-]+)",
    re.IGNORECASE,
)
_SQL_TRUNCATE_RE = re.compile(r"\bTRUNCATE(?:\s+TABLE)?\s+(?P<name>[`'\w.\-]+)", re.IGNORECASE)
_SQL_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+(?P<name>[`'\w.\-]+)", re.IGNORECASE)
_REDIS_FLUSH_RE = re.compile(r"\bFLUSH(?:ALL|DB)\b", re.IGNORECASE)
_MONGO_DROP_RE = re.compile(r"\bdb\.\w+\.drop\(\)|db\.dropDatabase\(\)", re.IGNORECASE)
_DB_CLI_RE = re.compile(
    r"\b(psql|mysql|mariadb|sqlite3|redis-cli|mongo|mongosh)\b(?P<tail>.*?)(?=(?:&&|\|\||\||;|$))",
    re.IGNORECASE,
)
_DB_PAYLOAD_RE = re.compile(
    r"""(?:-e|--execute|-c|--command|--eval)\s+(['"]?)(?P<body>.*?)\1""",
    re.IGNORECASE | re.DOTALL,
)


def _detect_fs(segment: str, cwd: str) -> DestructiveOp | None:
    """Detect filesystem-destructive ops in one segment."""
    for m in _FS_RM_RE.finditer(segment):
        tok = m.group("target")
        if tok.startswith("-"):  # option-like token, skip
            continue
        return DestructiveOp("fs", m.group("verb").lower(), tok, _abs_target(tok, cwd), segment)
    if _FIND_DELETE_RE.search(segment):
        # No single reliable target token; use the pattern itself.
        return DestructiveOp("fs", "find-delete", "find-delete", "", segment)
    for m in _TRUNCATE_RE.finditer(segment):
        tok = m.group("target")
        return DestructiveOp("fs", "truncate", tok, _abs_target(tok, cwd), segment)
    if _DD_OF_RE.search(segment):
        return DestructiveOp("fs", "dd-of", "dd-target", "", segment)
    if _MKFS_RE.search(segment):
        return DestructiveOp("fs", "mkfs", "mkfs-target", "", segment)
    m = _RM_DB_FILE_RE.search(segment)
    if m:
        tok = m.group("glob")
        return DestructiveOp("fs", "rm-db", tok, _abs_target(tok, cwd), segment)
    n = _PYDELETE_RE.search(segment)
    if n:
        tail = n.group("tail")
        for pm in _PY_OSPAT_RE.finditer(tail):
            return DestructiveOp("fs", "python-os-remove", pm.group(1), _abs_target(pm.group(1), cwd), segment)
        for pm in _PY_SHUTIL_RE.finditer(tail):
            return DestructiveOp("fs", "python-shutil-rmtree", pm.group(1), _abs_target(pm.group(1), cwd), segment)
    if (_EVAL_RE.search(segment) or _BASH_C_RE.search(segment)) and _re_eval_has_deletion(segment):
        return DestructiveOp("fs", "eval-or-shell-c", "eval-deletion", "", segment)
    if _XARGS_RM_RE.search(segment):
        return DestructiveOp("fs", "xargs-rm", "xargs-target", "", segment)
    return None


def _re_eval_has_deletion(segment: str) -> bool:
    body = ""
    m = _EVAL_RE.search(segment)
    if m:
        body += " " + m.group("body")
    m = _BASH_C_RE.search(segment)
    if m:
        body += " " + m.group("body")
    return bool(
        re.search(r"\brm\b|\brmdir\b|\bunlink\b|os\.remove|os\.unlink|shutil\.rmtree", body, re.IGNORECASE)
    )


def _detect_git(segment: str) -> DestructiveOp | None:
    if _GIT_HARD_RE.search(segment):
        return DestructiveOp("git", "reset-hard/clean", "worktree", "", segment)
    m = _GIT_BRANCH_D_RE.search(segment)
    if m:
        return DestructiveOp("git", "branch-D", m.group("target"), "", segment)
    if _GIT_PUSH_FORCE_RE.search(segment):
        return DestructiveOp("git", "push-force", "remote-history", "", segment)
    return None


def _detect_db_in_body(body: str, cli_label: str) -> DestructiveOp | None:
    m = _SQL_DROP_RE.search(body)
    if m:
        return DestructiveOp("db", f"DROP {m.group('kind').upper()}", m.group("name").strip("`'"), "", body)
    m = _SQL_TRUNCATE_RE.search(body)
    if m:
        return DestructiveOp("db", "TRUNCATE", m.group("name").strip("`'"), "", body)
    m = _SQL_DELETE_RE.search(body)
    if m and " WHERE " not in body.upper():
        return DestructiveOp("db", "DELETE-no-WHERE", m.group("name").strip("`'"), "", body)
    if _REDIS_FLUSH_RE.search(body):
        return DestructiveOp("db", "redis-FLUSH", "redis-keyspace", "", body)
    if _MONGO_DROP_RE.search(body):
        return DestructiveOp("db", "mongo-drop", "mongo-db", "", body)
    return None


def _detect_db(segment: str) -> DestructiveOp | None:
    for cli_m in _DB_CLI_RE.finditer(segment):
        cli = cli_m.group(1).lower()
        tail = cli_m.group("tail")
        payloads = [m.group("body") for m in _DB_PAYLOAD_RE.finditer(tail)]
        scan = "\n".join(payloads) or tail
        if op := _detect_db_in_body(scan, cli):
            return op
    return None


def _heredoc_db_block(bodies: list[str]) -> DestructiveOp | None:
    for b in bodies:
        if op := _detect_db_in_body(b, "heredoc"):
            return op
    return None


def _detect_any(segment: str, cwd: str) -> DestructiveOp | None:
    """Detect the first destructive op in a segment, in priority order."""
    return _detect_fs(segment, cwd) or _detect_git(segment) or _detect_db(segment)


# ── Intent check (Tier B) ───────────────────────────────────────────────────
# Words that count as "destructive verbs" when spoken by the user. Match
# loosely (whole-word, case-insensitive). Tables 2 & 3: per-category synonyms
# so "drop the users table" and "delete the users table" both count.
_INTENT_VERBS_FS = {
    "delete", "remove", "rm", "trash", "purge", "wipe", "clear",
    "shred", "drop", "unlink", "erase", "clean", "discard",
}
_INTENT_VERBS_DB = {
    "drop", "truncate", "delete", "remove", "purge", "wipe", "clear", "empty",
}
_INTENT_VERBS_GIT = {
    "reset", "discard", "drop", "delete", "abort", "force-push", "force push",
    "overwrite", "abandon", "throw away", "revert", "clean",
}

# Map a detected category to its synonym set.
_VERBS_FOR_CATEGORY = {
    "fs": _INTENT_VERBS_FS,
    "db": _INTENT_VERBS_DB,
    "git": _INTENT_VERBS_GIT,
}


def _word_boundary_regex(words: set[str]) -> re.Pattern:
    """Build a regex that matches any of `words` at a word boundary,
    allowing for spaces/hyphens in multi-word entries.
    """
    # Sort longest-first so 'force push' matches before 'push'.
    parts = sorted({re.escape(w.replace(" ", r"\s+")) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


# Pre-compile per-category verb regexes.
_VERB_RE_FOR_CATEGORY = {cat: _word_boundary_regex(words) for cat, words in _VERBS_FOR_CATEGORY.items()}


# Tokens we accept as "the target appears in the user's message". Accept either
# the original CLI token or its basename / last path component, so e.g.
# `rm data/users.csv` is matched by "delete users.csv".
def _target_match_regex(target: str) -> re.Pattern:
    """Match `target` or its basename, as a whole token (case-insensitive),
    allowing the surrounding characters to be word/slash/quote boundary.
    """
    base = os.path.basename(target.rstrip("/")) if target else target
    cands = [t for t in {target, base} if t and not {".", "/", "*"} >= set(t)]
    if not cands:
        # Fall back to the raw target (e.g. "find-delete", "worktree").
        cands = [target]
    parts = [re.escape(c) for c in cands]
    return re.compile(r"(?<![\w/])(?:" + "|".join(parts) + r")(?![\w/])", re.IGNORECASE)


def _user_authorizes(op: DestructiveOp, user_messages: list[str]) -> bool:
    """Tier B: True iff a recent genuine user message contains BOTH a
    destructive verb (for the op's category) AND the target token.
    """
    if not user_messages:
        return False
    verb_re = _VERB_RE_FOR_CATEGORY.get(op.category)
    if verb_re is None:  # unknown category → fail-safe: require explicit verb
        verb_re = _VERB_RE_FOR_CATEGORY["fs"]
    target_re = _target_match_regex(op.target or "")
    # Look at the last USER_MSG_WINDOW user messages, joined per-message so the
    # verb AND target must occur within the SAME message (not split across).
    for msg in user_messages[-USER_MSG_WINDOW:]:
        if verb_re.search(msg) and target_re.search(msg):
            return True
    return False


# ── Decision composition ────────────────────────────────────────────────────
def _maybe_block_for_confirmation(op: DestructiveOp, user_messages: list[str]) -> Decision:
    """Build a 'needs user confirmation' BLOCK message."""
    fp = _fingerprint(op)
    base = (
        f"NEEDS USER CONFIRMATION: destructive op detected — "
        f"'{op.verb}' targeting '{op.target}'.\n"
        f"This operation was not authorized by a genuine user message in this "
        f"session. Relay the following to the user and ask them to confirm by "
        f"sending a message that names BOTH the deletion AND the target:\n"
        f"    e.g. \"delete {op.target}\" / \"yes, drop {op.target}\"\n"
        f"The operation will run once the user's reply matches. This gate "
        f"exists to prevent instructions embedded in tool outputs (webpages, "
        f"file contents, command stdout) from triggering deletions."
    )
    msg = _wrap_escalation(fp, base)
    return Decision(is_block=True, message=msg, fingerprint=fp, reason="needs-user-confirmation")


def _fingerprint(op: DestructiveOp) -> str:
    return f"{op.category}:{op.verb}:{op.target or '<no-target>'}"


def _wrap_escalation(fingerprint: str, base: str) -> str:
    """Return `base` initially; after MAX_REPEAT_BLOCK identical blocks switch
    to an escalated message that tells the LLM to stop retrying.
    """
    n = _BLOCK_COUNTS.get(fingerprint, 0) + 1
    _BLOCK_COUNTS[fingerprint] = n
    if n < MAX_REPEAT_BLOCK:
        return base
    return (
        f"You have now attempted this destructive op {n} times. It is "
        f"intentionally blocked and will NOT be allowed through by retrying.\n"
        f"To proceed: the user must send a genuine message naming both the "
        f"deletion AND the target (e.g. \"yes, delete <target>\"). Until that "
        f"happens, this will keep blocking. Stop retrying the same command."
    )


# ── Public entry point ──────────────────────────────────────────────────────
def analyze(
    cmd: str,
    cwd: str | None = None,
    user_messages: list[str] | None = None,
) -> Decision:
    """Inspect `cmd`. Returns a Decision.

    - Decision(is_block=False)               -> caller runs the command
    - Decision(is_block=True, .message)       -> caller MUST NOT run; relay .message

    `user_messages` is the list of recent genuine user-typed messages (role:user
    only — NOT tool outputs, NOT assistant messages). If omitted, Tier B cannot
    authorize anything and only Tier A (throwaway paths) will let a destructive
    op through.
    """
    if not GUARD_ENABLED:
        return Decision(is_block=False, reason="guard-disabled")
    if not cmd or not cmd.strip():
        return OK
    cwd = cwd or os.getcwd()
    user_messages = user_messages or []

    # Heredoc DB drops are detected before splitting (the body carries the SQL).
    # Only scan the body when the heredoc opener (what's left after stripping
    # the body) actually invokes a DB client — otherwise free-form heredoc text
    # (markdown, prose, source code) would false-positive on ordinary words
    # like "truncate" or "drop". We match against the *cleaned* command because
    # _DB_CLI_RE is anchored to end-of-line and won't cross heredoc newlines.
    cleaned, bodies = _strip_heredocs(cmd)
    if bodies and _DB_CLI_RE.search(cleaned):
        op = _heredoc_db_block(bodies)
        if op:
            return _decide(op, user_messages)

    # Otherwise: split into chained sub-commands and inspect each.
    for seg in _split_segments(cmd):
        op = _detect_any(seg, cwd)
        if op:
            return _decide(op, user_messages)
    return OK


def _decide(op: DestructiveOp, user_messages: list[str]) -> Decision:
    """Apply the policy to a detected destructive op."""
    # Tier 0 (pre-tier): `> path` where `path` does not yet exist is file
    # creation, not truncation. The stat can only ever *rule out* a false
    # positive (a non-existent file has nothing to destroy), so it doesn't
    # weaken real protection. Scoped to the "truncate" verb so genuine
    # rm / pydelete / dd detections are unaffected.
    if op.category == "fs" and op.verb == "truncate" and op.target_abs:
        try:
            if not os.path.exists(op.target_abs):
                return Decision(
                    is_block=False,
                    reason="truncate-target-does-not-exist-create",
                    fingerprint=_fingerprint(op),
                )
        except OSError:
            pass  # stat failed: treat as ambiguous → fall through to the tiers.

    # Tier A: throwaway path → ALLOW (cache cleanup). FS ops only.
    if op.category == "fs" and op.target_abs and _is_safe(op.target_abs):
        return Decision(is_block=False, reason="tier-a-throwaway-path", fingerprint=_fingerprint(op))

    # Tier B: user-authorized.
    if _user_authorizes(op, user_messages):
        return Decision(is_block=False, reason="tier-b-user-authorized", fingerprint=_fingerprint(op))

    # Tier C: block, ask for user confirmation.
    return _maybe_block_for_confirmation(op, user_messages)


# ── Diagnostic helpers (for tests + the audit log) ─────────────────────────
def _extract_user_messages(chat_messages: list[dict]) -> list[str]:
    """Pull role:user contents from the full chat message list.

    Excludes tool-call outputs, assistant messages, and system messages. This
    is the trust boundary — only these carry genuine user intent.
    """
    out = []
    for m in chat_messages:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                out.append(content)
    return out


def authorized_as_string(op_verb: str, target: str, category: str = "fs") -> str:
    """Example phrase the agent could ask the user to type to authorize an op."""
    verb_re = _VERB_RE_FOR_CATEGORY.get(category, _VERB_RE_FOR_CATEGORY["fs"])
    # Pick any one verb as a suggestion.
    sample = next(iter(_VERBS_FOR_CATEGORY.get(category, _INTENT_VERBS_FS)))
    return f"{sample} {target}"
