#!/usr/bin/env python3
"""
Windows diagnostic for RunTool's executable resolution / shim logic.

WHAT THIS TESTS
================
Neon Architect's RunTool resolves program names (npm, npx, git, etc.) to
real executables via shutil.which(), with a Windows-specific fallback that
retries .cmd/.exe/.bat suffixes if the bare name isn't found. This script
exercises that logic directly against your real Windows environment and
reports exactly what it finds — no mocking, no simulation.

It covers the three things the patch author (an LLM) explicitly could NOT
verify without a real Windows machine:

  1. Does _resolve_executable actually find real installs? (npm, node, git,
     python, pip — whichever you have installed)
  2. Does the PATHEXT-stripped fallback path actually work? (this is the
     scenario the fix specifically targets: shutil.which(bare_name) failing
     even though e.g. npm.cmd exists, because PATHEXT doesn't cover it)
  3. Does a RESOLVED executable actually LAUNCH correctly via
     subprocess.run(shell=False)? (this checks the .cmd/.bat execution
     semantics that were explicitly LEFT UNCHANGED in the patch — if this
     fails, that's a separate, deeper issue than what was patched)

HOW TO RUN
==========
    python test_windows_shim.py

Optionally point it at your actual neon_architect.py if it's not in the
same directory:

    python test_windows_shim.py --file C:\\path\\to\\neon_architect.py

No dependencies beyond the standard library. Safe to run repeatedly —
nothing here writes files or touches your project directory; the only
subprocess calls made are cheap, safe, read-only version/help queries
(e.g. `npm --version`), never anything from RunTool's destructive-git
gate and never anything that mutates state.

WHAT TO SEND BACK
==================
Copy-paste the full output. Each section prints PASS/FAIL/SKIP per check.
If anything is FAIL, the surrounding diagnostic detail (raw exception,
PATHEXT value, resolved path) is exactly what's needed to fix it.
"""

import argparse
import importlib.util
import os
import subprocess
import sys
import shutil
from pathlib import Path


def load_run_tool(file_path: Path):
    spec = importlib.util.spec_from_file_location("neon_architect", str(file_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neon_architect"] = mod
    spec.loader.exec_module(mod)
    return mod.RunTool


def section(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def result(label, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {label}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}")
    return ok


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--file",
        default="neon_architect.py",
        help="Path to neon_architect.py (default: neon_architect.py in cwd)",
    )
    args = parser.parse_args()

    file_path = Path(args.file).resolve()

    print("Windows shim diagnostic for RunTool")
    print(f"Python:        {sys.version}")
    print(f"sys.platform:  {sys.platform}")
    print(f"PATHEXT:       {os.environ.get('PATHEXT', '<not set>')}")
    print(f"Target file:   {file_path}")

    if sys.platform != "win32":
        print()
        print("WARNING: sys.platform is not 'win32'. This script is meant to run")
        print("on a real Windows machine — running it elsewhere will not exercise")
        print("the code paths it's designed to check. Continuing anyway, but the")
        print("Windows-specific sections below will trivially no-op.")

    if not file_path.exists():
        print()
        print(f"ERROR: {file_path} not found. Pass --file <path> to point at it.")
        sys.exit(2)

    try:
        RunTool = load_run_tool(file_path)
    except Exception as e:
        print()
        print(f"ERROR: failed to import RunTool from {file_path}: {e!r}")
        sys.exit(2)

    # _resolve_executable is an instance method (needs self.cwd for the
    # venv-awareness check added alongside this diagnostic script), so a
    # real instance is needed up front rather than calling it as a bare
    # classmethod.
    tool = RunTool(Path.cwd())

    all_ok = True

    # ------------------------------------------------------------------
    # SECTION 1: Basic resolution — does _resolve_executable find real
    # installs on this machine, for whatever's actually on PATH?
    # ------------------------------------------------------------------
    section("SECTION 1 — Basic executable resolution")
    print("Checking whether _resolve_executable finds each registered program.")
    print("FAIL here for a program you know is installed = a real bug.")
    print("FAIL for a program you don't have installed is expected, not a bug.")
    print()

    resolved_any = {}
    for prog in sorted(RunTool._REGISTRY.keys()):
        try:
            resolved = tool._resolve_executable(prog, project_root=Path.cwd())
        except Exception as e:
            resolved = None
            print(f"  [ERROR] {prog}: exception during resolution: {e!r}")
            all_ok = False
            continue
        resolved_any[prog] = resolved
        status = "found" if resolved else "not found"
        print(f"  {prog:10} -> {resolved or '(not found)'}  [{status}]")

    # ------------------------------------------------------------------
    # SECTION 2: PATHEXT-stripped fallback — the actual scenario the
    # patch targets. Temporarily clear PATHEXT and confirm resolution
    # STILL succeeds via the new explicit-suffix retry.
    # ------------------------------------------------------------------
    section("SECTION 2 — Resolution with PATHEXT stripped (the actual fix)")
    print("This is the scenario the patch specifically targets: if PATHEXT")
    print("doesn't include .CMD, shutil.which('npm') alone can return None")
    print("even though npm.cmd exists on PATH. The patch adds an explicit")
    print("retry with .cmd/.exe/.bat suffixes when the bare name lookup fails.")
    print()

    # Pick a program that resolved in section 1 with a .cmd/.bat shim,
    # since that's the case this fix actually matters for. npm/npx are
    # the most likely real-world case; fall back to whatever resolved.
    candidates = [p for p in ("npm", "npx") if resolved_any.get(p)]
    if not candidates:
        candidates = [p for p, r in resolved_any.items() if r]

    if not candidates:
        print("  [SKIP] Nothing resolved in Section 1 to test against.")
    else:
        old_pathext = os.environ.get("PATHEXT")
        try:
            # Strip PATHEXT entirely to force the failure mode this fix
            # targets, then confirm resolution still succeeds.
            os.environ["PATHEXT"] = ""
            for prog in candidates:
                before = resolved_any.get(prog)
                try:
                    after = tool._resolve_executable(prog, project_root=Path.cwd())
                except Exception as e:
                    after = None
                    print(f"  [ERROR] {prog}: exception during resolution: {e!r}")
                    all_ok = False
                    continue
                ok = after is not None
                all_ok = result(
                    f"{prog}: resolves with PATHEXT stripped",
                    ok,
                    f"normal PATHEXT resolved: {before}\nstripped PATHEXT resolved: {after}"
                    + ("" if ok else "\n  -> This means the shim fallback in _resolve_executable did not "
                                      "kick in, or shutil.which() still failed even with explicit suffixes. "
                                      "Check whether npm.cmd/npm.exe genuinely exists on PATH by hand: "
                                      "`where npm` in cmd.exe."),
                ) and all_ok
        finally:
            if old_pathext is None:
                os.environ.pop("PATHEXT", None)
            else:
                os.environ["PATHEXT"] = old_pathext

    # ------------------------------------------------------------------
    # SECTION 3: Execution semantics — does the resolved path actually
    # LAUNCH via subprocess.run(shell=False)? This is the part the patch
    # explicitly did NOT touch. A failure here is a pre-existing issue,
    # not something introduced by this patch, but worth knowing about.
    # ------------------------------------------------------------------
    section("SECTION 3 — Execution: does the resolved path actually launch?")
    print("Runs a cheap, safe, read-only command (--version or --help) through")
    print("subprocess.run(shell=False) using exactly what RunTool resolved.")
    print("This checks .cmd/.bat launch semantics, which the patch left")
    print("untouched — a FAIL here indicates a separate, deeper issue.")
    print()

    safe_args = {
        "python": ["--version"],
        "pytest": ["--version"],
        "pip": ["--version"],
        "npm": ["--version"],
        "npx": ["--version"],
        "node": ["--version"],
        "git": ["--version"],
        "cargo": ["--version"],
        "go": ["version"],
        "make": ["--version"],
        "ruff": ["--version"],
        "black": ["--version"],
        "flake8": ["--version"],
        "mypy": ["--version"],
    }

    for prog, resolved in sorted(resolved_any.items()):
        if not resolved:
            print(f"  [SKIP] {prog}: not resolved in Section 1, nothing to launch.")
            continue
        argv = [resolved] + safe_args.get(prog, ["--version"])
        try:
            proc = subprocess.run(
                argv, shell=False, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=15,
            )
            ok = True
            detail = (
                f"argv: {argv}\n"
                f"exit code: {proc.returncode}\n"
                f"stdout (first 100 chars): {proc.stdout[:100]!r}\n"
                f"stderr (first 100 chars): {proc.stderr[:100]!r}"
            )
        except FileNotFoundError as e:
            ok = False
            detail = (
                f"argv: {argv}\n"
                f"FileNotFoundError: {e!r}\n"
                "  -> Resolution said this exists, but launching it directly failed.\n"
                "     This is exactly the .cmd/.bat launch-semantics gap the patch\n"
                "     did NOT attempt to fix — RunTool.execute() has a fallback for\n"
                "     this specific case (retries _windows_shim_variants), but this\n"
                "     diagnostic calls subprocess.run directly to isolate whether\n"
                "     THAT fallback is even necessary on your machine."
            )
        except Exception as e:
            ok = False
            detail = f"argv: {argv}\nUnexpected exception: {e!r}"
        all_ok = result(f"{prog}: launches via resolved path", ok, detail) and all_ok

    # ------------------------------------------------------------------
    # SECTION 4: venv-awareness — does RunTool prefer an in-project
    # .venv/venv over sys.executable when one exists at the project root?
    # ------------------------------------------------------------------
    section("SECTION 4 — venv-awareness for python/pip")
    print("Creates a throwaway .venv in a temp directory and confirms RunTool")
    print("resolves 'python'/'pip' to the venv's interpreter, not the agent's")
    print("own sys.executable, when run with that temp dir as the project root.")
    print("Cleans up the temp venv afterward regardless of outcome.")
    print()

    import tempfile
    import venv as venv_module

    tmp_dir = None
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="neon_venv_test_"))
        venv_module.create(tmp_dir / ".venv", with_pip=True)

        venv_tool = RunTool(tmp_dir)
        resolved_py = venv_tool._resolve_executable("python", project_root=tmp_dir)
        resolved_pip = venv_tool._resolve_executable("pip", project_root=tmp_dir)

        expected_bin = tmp_dir / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
        py_in_venv = resolved_py is not None and str(expected_bin) in str(resolved_py)
        pip_in_venv = resolved_pip is not None and str(expected_bin) in str(resolved_pip)

        all_ok = result(
            "python resolves inside the temp .venv, not sys.executable",
            py_in_venv,
            f"resolved: {resolved_py}\nexpected under: {expected_bin}\nsys.executable: {sys.executable}",
        ) and all_ok
        all_ok = result(
            "pip resolves inside the temp .venv, not sys.executable",
            pip_in_venv,
            f"resolved: {resolved_pip}\nexpected under: {expected_bin}",
        ) and all_ok

        # Confirm a directory WITHOUT a .venv correctly falls back to the
        # normal global resolution (i.e. this feature doesn't break the
        # no-venv case, which is still the common case for most projects).
        no_venv_dir = Path(tempfile.mkdtemp(prefix="neon_no_venv_test_"))
        try:
            no_venv_tool = RunTool(no_venv_dir)
            fallback_resolved = no_venv_tool._resolve_executable("python", project_root=no_venv_dir)
            all_ok = result(
                "python still resolves normally when no .venv exists",
                fallback_resolved is not None,
                f"resolved: {fallback_resolved}",
            ) and all_ok
        finally:
            shutil.rmtree(no_venv_dir, ignore_errors=True)

    except Exception as e:
        all_ok = result("venv-awareness test setup", False, f"Unexpected exception: {e!r}")
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # SECTION 5: Full RunTool.execute() round-trip, including the
    # FileNotFoundError -> shim-retry fallback path inside execute()
    # itself (separate from the Section 2 resolution-time fallback).
    # ------------------------------------------------------------------
    section("SECTION 5 — Full RunTool.execute() round-trip")
    print("Runs the actual public RunTool.execute() method end-to-end, the way")
    print("the agent itself would call it, for a couple of safe read-only ops.")
    print()

    trials = [
        ("git", ["--version"]),
        ("python", ["--version"]),
    ]
    if resolved_any.get("npm"):
        trials.append(("npm", ["--version"]))
    if resolved_any.get("node"):
        trials.append(("node", ["--version"]))

    for prog, prog_args in trials:
        try:
            r = tool.execute(program=prog, args=prog_args, timeout=15)
            ok = not r.is_error
            detail = f"is_error={r.is_error}\noutput/error (first 150 chars): {(r.text())[:150]!r}"
        except Exception as e:
            ok = False
            detail = f"Unexpected exception calling execute(): {e!r}"
        all_ok = result(f"RunTool.execute(program={prog!r}, args={prog_args!r})", ok, detail) and all_ok

    # ------------------------------------------------------------------
    # SECTION 6: test_digest lives on the per-call ToolResult, not a
    # shared RunTool class attribute (the class-attribute race condition
    # fix). Platform-independent — included here for completeness since
    # it's cheap to check alongside everything else.
    # ------------------------------------------------------------------
    section("SECTION 6 — test_digest is per-call, not shared class state")
    print("Confirms RunTool.last_test_digest (the old shared class attribute)")
    print("no longer exists, and that a test-invocation call's digest travels")
    print("on that call's own ToolResult.test_digest instead.")
    print()

    has_class_attr = hasattr(RunTool, "last_test_digest")
    all_ok = result(
        "RunTool.last_test_digest class attribute is gone",
        not has_class_attr,
        "Still present — the class-attribute race condition was not removed."
        if has_class_attr else "",
    ) and all_ok

    try:
        r = tool.execute(program="python", args=["-m", "pytest", "--version"], timeout=15)
        has_digest = bool(getattr(r, "test_digest", ""))
        all_ok = result(
            "test-invocation call sets ToolResult.test_digest",
            has_digest,
            f"test_digest (first 150 chars): {r.test_digest[:150]!r}"
            if has_digest else "test_digest was empty for a recognized test invocation.",
        ) and all_ok

        r2 = tool.execute(program="git", args=["--version"], timeout=15)
        no_digest_for_nontest = not getattr(r2, "test_digest", "")
        all_ok = result(
            "non-test call leaves ToolResult.test_digest empty",
            no_digest_for_nontest,
            f"unexpected test_digest on a non-test call: {r2.test_digest[:150]!r}"
            if not no_digest_for_nontest else "",
        ) and all_ok
    except Exception as e:
        all_ok = result("test_digest round-trip", False, f"Unexpected exception: {e!r}")

    # ------------------------------------------------------------------
    section("SUMMARY")
    if all_ok:
        print("  All checks passed. The Windows shim fix appears to work correctly")
        print("  on this machine, including under a stripped PATHEXT.")
    else:
        print("  One or more checks FAILED — see details above.")
        print("  Copy this entire output and send it back for diagnosis.")
    print()

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()