
import sys
import os
from pathlib import Path
from unittest import mock
import pytest
from typer.testing import CliRunner

# Ensure we can import convert from root
sys.path.append(str(Path(__file__).parent.parent))

import convert

runner = CliRunner()

@pytest.fixture
def mock_fd_success():
    with mock.patch("convert.run_command") as m:
        # First call checks version (success)
        # Second call runs fd (success, returns some paths)
        m.side_effect = [
            (0, "fd 8.0.0", ""), # version
            (0, "/path/to/repo1/.git\n/path/to/repo2/.git\n", "") # fd result
        ]
        yield m

@pytest.fixture
def mock_fd_not_found():
    with mock.patch("convert.run_command") as m:
        # First call checks version (fails)
        m.side_effect = [(127, "", "command not found")]
        yield m

def test_detect_decode():
    assert convert.detect_decode(b"hello") == "hello"
    assert convert.detect_decode(None) == ""
    # Test utf-8
    assert convert.detect_decode("한글".encode("utf-8")) == "한글"
    # Test fallback (mock chardet failure or strange bytes)
    # Using invalid utf-8 sequence
    bad_bytes = b'\xff\xfe\x00' # might be utf-16 BOM but partial?
    # chardet should detect it or we fallback.
    decoded = convert.detect_decode(bad_bytes)
    assert isinstance(decoded, str)

def test_find_git_repos_fd_success(mock_fd_success):
    repos = convert.find_git_repos(Path("/root"))
    assert len(repos) == 2
    assert Path("/path/to/repo1") in repos
    assert Path("/path/to/repo2") in repos

def test_find_git_repos_fallback(mock_fd_not_found):
    # We need to mock os.walk as well since fallback uses it
    with mock.patch("os.walk") as mock_walk:
        mock_walk.return_value = [
            ("/root/repo3", [".git", "src"], ["README.md"]),
            ("/root/repo4", ["src"], ["README.md"]),
        ]
        repos = convert.find_git_repos(Path("/root"))
        # Should find repo3, but not repo4
        assert len(repos) == 1
        assert Path("/root/repo3") in repos

def test_run_command_mock():
    with mock.patch("subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = b"output"
        mock_run.return_value.stderr = b""

        code, out, err = convert.run_command(["ls"])
        assert code == 0
        assert out == "output"

def test_main_batch_flow(tmp_path):
    # Test the main CLI flow in batch mode with mocked finding and git operations
    with mock.patch("convert.find_git_repos") as mock_find, \
         mock.patch("convert.run_command") as mock_run, \
         mock.patch("convert.track", side_effect=lambda x, **kwargs: x): # Mock track to just return iterator

        mock_find.return_value = [Path("/repo/a"), Path("/repo/b")]

        # We need to simulate the git calls:
        # For each repo:
        # 1. git remote get-url origin
        # 2. git remote set-url origin NEW
        # 3. git fetch origin

        # We can use side_effect to return different values based on command
        def git_side_effect(cmd, cwd=None, capture=True):
            cmd_str = " ".join(cmd)
            if "get-url" in cmd_str:
                return (0, "https://github.com/user/repo.git", "")
            elif "set-url" in cmd_str:
                return (0, "", "")
            elif "fetch" in cmd_str:
                return (0, "fetching...", "")
            return (1, "", "unknown")

        mock_run.side_effect = git_side_effect

        result = runner.invoke(convert.app, [
            str(tmp_path),
            "--find", "github.com",
            "--replace", "gitlab.com",
            "--batch"
        ])

        assert result.exit_code == 0
        assert "Found 2 repositories" in result.stdout
        assert "New Origin:     https://gitlab.com/user/repo.git" in result.stdout
        assert "Success: 2" in result.stdout

def test_main_interactive_exclude(tmp_path):
    # Test interactive exclusion (mocking questionary)
    with mock.patch("convert.find_git_repos") as mock_find, \
         mock.patch("convert.run_command") as mock_run, \
         mock.patch("convert.track", side_effect=lambda x, **kwargs: x), \
         mock.patch("questionary.checkbox") as mock_checkbox, \
         mock.patch("questionary.confirm") as mock_confirm:

        mock_find.return_value = [Path("/repo/a"), Path("/repo/b")]

        # Mock user deselecting /repo/b (only selecting /repo/a)
        mock_checkbox.return_value.ask.return_value = ["/repo/a"]
        mock_confirm.return_value.ask.return_value = True

        def git_side_effect(cmd, cwd=None, capture=True):
            cmd_str = " ".join(cmd)
            if "get-url" in cmd_str:
                return (0, "https://github.com/user/repo.git", "")
            return (0, "", "")

        mock_run.side_effect = git_side_effect

        result = runner.invoke(convert.app, [
            str(tmp_path),
            "--find", "github.com",
            "--replace", "gitlab.com"
        ])

        assert result.exit_code == 0
        # Should process repo a
        assert "repo: /repo/a" in result.stdout
        # Should NOT process repo b
        assert "repo: /repo/b" not in result.stdout
