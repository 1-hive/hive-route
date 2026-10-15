# SPDX-License-Identifier: GPL-3.0-or-later
"""Call Claude (claude -p) or Codex (codex exec) headless with the host's normal logins."""

import json
import re
import subprocess
import tempfile
from pathlib import Path

CLAUDE_SYSTEM = "You are a careful assistant. Output only what is asked, with no preamble."


def call_claude(prompt: str, model: str = "opus", timeout: int = 900) -> str:
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["claude", "-p", "--tools", "", "--no-session-persistence",
                            "--model", model, "--system-prompt", CLAUDE_SYSTEM,
                            "--output-format", "text"],
                           input=prompt, capture_output=True, text=True, cwd=d, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude exit {r.returncode}: {r.stderr[-500:]} {r.stdout[-500:]}")
    return r.stdout


def call_codex(prompt: str, model: str = "gpt-6-astra", effort: str = "high",
               timeout: int = 1800) -> str:
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.txt"
        r = subprocess.run(["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                            "-m", model, "-c", f"model_reasoning_effort={effort}", "-C", d,
                            "-o", str(out), "-"],
                           input=prompt, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"codex exit {r.returncode}: {r.stderr[-800:]}")
        return out.read_text()


def extract_json(s: str):
    """The first JSON array or object in a reply (fenced or bare)."""
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m:
        s = m.group(1)
    starts = [i for i in (s.find("["), s.find("{")) if i >= 0]
    if not starts:
        raise ValueError("no JSON in reply")
    i = min(starts)
    obj, _ = json.JSONDecoder().raw_decode(s[i:])
    return obj
