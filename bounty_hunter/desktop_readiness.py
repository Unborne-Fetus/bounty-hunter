"""Bounded local readiness checks. No credential output is returned or saved."""
import os
import shutil
import subprocess


TOOL_HELP = {
    "git": "Install Git for Windows, then restart Bounty Hunter.",
    "gh": "Install GitHub CLI and sign in with gh auth login.",
    "codex": "Install Codex CLI and sign in before generating patches.",
    "node": "Node.js is optional for scanning; an npm-installed Codex CLI needs Node.js.",
    "npm": "npm is optional for scanning; it is used to install the npm version of Codex.",
}


def check_readiness(settings, finder=None, runner=None):
    finder = finder or shutil.which
    runner = runner or subprocess.run
    checks = []

    def check(name, command, advice):
        binary = finder(command[0])
        if not binary:
            checks.append({"name": name, "status": "missing", "message": advice})
            return
        try:
            # Never display auth output: gh may include account and token metadata.
            result = runner(
                [binary] + command[1:], capture_output=True, text=True,
                errors="replace", timeout=10, shell=False,
                **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
            )
            ready = result.returncode == 0
            message = "Check passed." if ready else advice
        except (OSError, subprocess.TimeoutExpired):
            ready, message = False, advice
        checks.append({"name": name, "status": "ready" if ready else "needs_attention",
                       "message": message})

    for tool in ("git", "codex", "node", "npm"):
        check(tool, [tool, "--version"], TOOL_HELP[tool])
    check("GitHub CLI login", ["gh", "auth", "status"], TOOL_HELP["gh"])
    # Support the same Docker/Podman alternatives as the execution backend.
    runtimes = [name for name in ("docker", "podman") if finder(name)]
    if not runtimes:
        checks.append({"name": "Container runtime", "status": "missing",
                       "message": "Install and start Docker Desktop or Podman for isolated tests."})
    for name in runtimes:
        check(name + " daemon", [name, "info"], "Start " + name + " and enable Linux containers.")
    token_set = bool(os.environ.get(settings["github_token_env"]))
    checks.append({"name": "GitHub scan token", "status": "configured" if token_set else "optional",
                   "message": "Environment variable is set; validity is checked on requests." if token_set
                   else "Public scanning works without a token with lower request limits."})
    return checks


def allowed_actions(status, *, has_patch=False, has_package=False):
    """UI hints only; backend state and hash checks remain authoritative."""
    actions = {"inspect"}
    if status == "queued":
        actions.add("approve")
    elif status == "approved":
        actions.add("preflight")
    elif status == "running":
        actions.add("verify" if has_patch else "patch")
    elif status == "verified":
        actions.update(("bundle", "approve_bundle"))
    elif status == "submission_approved":
        actions.update(("bundle", "prepare"))
        if has_package:
            actions.add("publish")
    return actions
