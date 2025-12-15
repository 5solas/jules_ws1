# /// script
# dependencies = [
#   "typer",
#   "questionary",
#   "chardet",
#   "rich",
# ]
# ///

import subprocess
import sys
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple
import typer
import questionary
import chardet
from rich.console import Console
from rich.progress import track

console = Console()
app = typer.Typer(add_completion=False)

def detect_decode(data: bytes) -> str:
    """
    Detects the encoding of the byte data and decodes it to a string.
    Falls back to utf-8 (replace) if detection fails.
    """
    if not data:
        return ""
    result = chardet.detect(data)
    encoding = result['encoding'] or 'utf-8'
    try:
        return data.decode(encoding)
    except Exception:
        return data.decode('utf-8', errors='replace')

def run_command(cmd: List[str], cwd: Optional[Path] = None, capture: bool = True) -> Tuple[int, str, str]:
    """
    Runs a command and returns (returncode, stdout, stderr).
    Handles encoding detection for output.
    """
    try:
        # On Windows, we might need shell=True for some commands, but usually not for list-based args.
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            check=False
        )
        stdout = detect_decode(result.stdout)
        stderr = detect_decode(result.stderr)
        return result.returncode, stdout, stderr
    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", str(e)

def find_git_repos(root_path: Path) -> List[Path]:
    """
    Finds git repositories using 'fd' or falls back to os.walk.
    Detects standard repos, worktrees, and submodules.
    """
    # Check if fd is installed
    code, _, _ = run_command(["fd", "--version"])

    repos = []

    if code == 0:
        console.print(f"[green]Scanning {root_path} using fd...[/green]")
        # fd -H (hidden) -I (no-ignore) "^.git$" (name regex) <root_path>
        # This will find both .git directories and .git files
        cmd = ["fd", "-H", "-I", "^.git$", str(root_path)]
        code, stdout, stderr = run_command(cmd)

        if code != 0:
            console.print(f"[red]fd execution failed:[/red] {stderr}")
            console.print("[yellow]Falling back to standard scan...[/yellow]")
        else:
            lines = stdout.strip().splitlines()
            for line in lines:
                git_item = Path(line.strip())
                repos.append(git_item.parent)
            return repos

    if code != 0:
        console.print("[yellow]fd not found or failed. Using standard os.walk (this might be slower)...[/yellow]")

    for dirpath, dirnames, filenames in os.walk(root_path):
        # Check for .git directory
        if ".git" in dirnames:
            repos.append(Path(dirpath))
            # Don't recurse into .git
            dirnames.remove(".git")
        # Check for .git file (worktree or submodule)
        elif ".git" in filenames:
            repos.append(Path(dirpath))

    return repos

def get_new_url(current_url: str, find: str, replace: str, regex: bool) -> Optional[str]:
    """
    Calculates the new URL based on find/replace logic.
    Returns None if no change is needed or match not found.
    """
    if regex:
        try:
            if re.search(find, current_url):
                return re.sub(find, replace, current_url)
        except re.error as e:
            console.print(f"[red]Regex Error:[/red] {e}")
            return None
    else:
        # String mode with robust slash handling
        if find in current_url:
            return current_url.replace(find, replace)
        elif find.endswith('/') and find.rstrip('/') in current_url:
            # Handle user inputting 'repo/' but url is 'repo'
            stripped = find.rstrip('/')
            if stripped:
                return current_url.replace(stripped, replace)
    return None

@app.command()
def main(
    path: Path = typer.Argument(..., help="Target directory to scan for git repositories"),
    find: str = typer.Option(None, help="String to find in the origin URL"),
    replace: str = typer.Option(None, help="String to replace with"),
    regex: bool = typer.Option(False, "--regex", "-r", help="Enable regex matching. Supports backreferences (e.g. \\1) in replace string."),
    batch: bool = typer.Option(False, "--batch", "-b", help="Run in batch mode without interactive confirmation"),
):
    """
    Scans for git repositories (including worktrees and submodules) in the given PATH and updates their origin URL.
    Also updates .gitmodules and .git/config submodule URLs.

    Examples of Regex usage:
      --find "github\\.com" --replace "gitlab.com" --regex
      --find "server-(\\d+)" --replace "host-\\1" --regex
    """
    if not path.exists():
        console.print(f"[red]Path '{path}' does not exist.[/red]")
        raise typer.Exit(code=1)

    # Input cleaning (if provided via args)
    if find:
        find = find.strip()
    if replace:
        replace = replace.strip()

    repos = find_git_repos(path)

    if not repos:
        console.print("[yellow]No git repositories found.[/yellow]")
        raise typer.Exit()

    # Normalize paths for cleaner display
    try:
        display_repos = [r.resolve() for r in repos]
    except Exception:
        display_repos = repos

    # Deduplicate
    display_repos = sorted(list(set(display_repos)))

    console.print(f"Found {len(display_repos)} repositories.")

    # Select repos
    selected_repos = display_repos
    if not batch:
        # Ask user to exclude
        choices = [
            questionary.Choice(str(r), checked=True)
            for r in display_repos
        ]

        selected_strings = questionary.checkbox(
            "Select repositories to process (uncheck to exclude):",
            choices=choices
        ).ask()

        if selected_strings is None: # Cancelled
            console.print("Operation cancelled.")
            raise typer.Exit()

        selected_repos = [Path(s) for s in selected_strings]

    if not selected_repos:
        console.print("No repositories selected.")
        raise typer.Exit()

    # Inputs for Find/Replace
    if find is None:
        find = questionary.text("Enter text to find in Origin URL (e.g., github.com):").ask()
        if find:
            find = find.strip()

    if replace is None:
        replace = questionary.text("Enter replacement text (e.g., gitlab.com):").ask()
        if replace:
            replace = replace.strip()

    # Handle cancellation of prompts
    if find is None or replace is None:
         console.print("Operation cancelled.")
         raise typer.Exit()

    if not find:
        console.print("[red]Find string cannot be empty.[/red]")
        raise typer.Exit(code=1)

    # Confirm
    if not batch:
        msg = f"Ready to process {len(selected_repos)} repositories. Proceed?"
        if regex:
            msg += " (Regex Mode Enabled)"
        if not questionary.confirm(msg).ask():
            console.print("Operation cancelled.")
            raise typer.Exit()

    # Process
    success_count = 0
    fail_count = 0
    skip_count = 0

    for repo in track(selected_repos, description="Processing repositories..."):
        console.print(f"\n[bold blue]repo:[/bold blue] {repo}")

        # 1. Update Origin
        code, stdout, stderr = run_command(["git", "remote", "get-url", "origin"], cwd=repo)

        # If the command failed, it might not be a repo (e.g. invalid state), or no remote named origin
        if code != 0:
             console.print(f"  [yellow]Skipping origin update:[/yellow] {stderr.strip()}")
        else:
            current_url = stdout.strip()
            console.print(f"  Current Origin: {current_url}")
            new_url = get_new_url(current_url, find, replace, regex)

            if new_url and new_url != current_url:
                console.print(f"  New Origin:     {new_url}")
                # Set URL
                code, stdout, stderr = run_command(["git", "remote", "set-url", "origin", new_url], cwd=repo)
                if code != 0:
                    console.print(f"  [red]Failed to set origin:[/red] {stderr.strip()}")
                    fail_count += 1
                else:
                    # Fetch
                    console.print("  Fetching...")
                    code, stdout, stderr = run_command(["git", "fetch", "origin"], cwd=repo)
                    if code != 0:
                        console.print(f"  [red]Fetch failed:[/red] {stderr.strip()}")
                        console.print("  [red]The origin was changed, but fetch failed. Please check the URL.[/red]")
                        # We still count this as a success for the URL update itself
                        success_count += 1
                    else:
                        console.print("  [green]Success![/green]")
                        success_count += 1
            else:
                 console.print(f"  [yellow]Skipping:[/yellow] URL unchanged or pattern not found.")
                 skip_count += 1

        # 2. Update Submodules (.gitmodules and .git/config)
        gitmodules_path = repo / ".gitmodules"
        if gitmodules_path.exists():
            console.print("  [blue]Checking .gitmodules...[/blue]")
            # Get submodule URLs
            code, stdout, stderr = run_command(["git", "config", "-f", ".gitmodules", "--get-regexp", "submodule\\..*\\.url"], cwd=repo)

            submodule_changes = False
            if code == 0:
                lines = stdout.strip().splitlines()
                for line in lines:
                    parts = line.split(" ", 1)
                    if len(parts) != 2: continue
                    key, url = parts

                    new_sub_url = get_new_url(url, find, replace, regex)
                    if new_sub_url and new_sub_url != url:
                         console.print(f"    Updating submodule '{key}':")
                         console.print(f"      Old: {url}")
                         console.print(f"      New: {new_sub_url}")

                         # Update .gitmodules
                         run_command(["git", "config", "-f", ".gitmodules", key, new_sub_url], cwd=repo)

                         # Update .git/config
                         run_command(["git", "config", key, new_sub_url], cwd=repo)

                         submodule_changes = True

            if submodule_changes:
                console.print("    Syncing submodules...")
                run_command(["git", "submodule", "sync"], cwd=repo)
                console.print("    [green]Submodules synced.[/green]")

    console.print(f"\n[bold]Done.[/bold] Origin Updates - Success: {success_count}, Failed: {fail_count}, Skipped: {skip_count}")

if __name__ == "__main__":
    app()
