from __future__ import annotations

import pytest

from locks import isolate

from .conftest import git


def test_a_passed_task_leaves_one_branch_and_the_checkout_untouched(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 7, tmp_path / "home")
    assert ws.branch == "locks/task-7" and ws.path.is_dir()
    (ws.path / "feature.py").write_text("x = 1\n")
    assert isolate.has_changes(ws)
    assert isolate.finish(ws, keep=True, message="locks task #7") == "locks/task-7"
    assert not ws.path.exists()
    assert not (git_repo / "feature.py").exists()
    assert "feature.py" in git(git_repo, "show", "--name-only", "locks/task-7")


def test_a_worker_that_committed_its_work_and_left_only_artifacts_keeps_its_branch(git_repo, tmp_path):
    # A real Haiku docs worker committed README.md itself and left __pycache__ behind: locks' own
    # commit had nothing staged once the artifacts were dropped, and the run died on it twice.
    ws = isolate.prepare(git_repo, 16, tmp_path / "home")
    (ws.path / "README.md").write_text("# docs\n")
    git(ws.path, "add", "README.md")
    git(ws.path, "commit", "-q", "-m", "Document it")
    (ws.path / "__pycache__").mkdir()
    (ws.path / "__pycache__" / "calc.pyc").write_bytes(b"\0")
    assert isolate.finish(ws, keep=True, message="Document it") == "locks/task-16"
    assert git(git_repo, "log", "-1", "--format=%s", "locks/task-16") == "Document it"
    assert "__pycache__" not in git(git_repo, "show", "--name-only", "locks/task-16")


def test_reset_discards_a_wrong_approach(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 8, tmp_path / "home")
    (ws.path / "app.py").write_text("broken\n")
    (ws.path / "junk.txt").write_text("junk\n")
    isolate.reset(ws)
    assert (ws.path / "app.py").read_text() == "print('hi')\n"
    assert not (ws.path / "junk.txt").exists()
    assert not isolate.has_changes(ws)


def test_a_failed_task_leaves_nothing(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 9, tmp_path / "home")
    (ws.path / "x.py").write_text("x\n")
    assert isolate.finish(ws, keep=False, message="m") is None
    assert "locks/task-9" not in git(git_repo, "branch", "--list")


def test_a_pass_that_changed_nothing_keeps_no_branch(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 10, tmp_path / "home")
    assert isolate.finish(ws, keep=True, message="m") is None


def test_outside_git_is_an_error(tmp_path):
    with pytest.raises(isolate.IsolationError):
        isolate.prepare(tmp_path, 1, tmp_path / "home")


def _with_dependencies(repo):
    (repo / ".gitignore").write_text("node_modules/\n.claude/settings.local.json\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ignore")
    (repo / "node_modules" / "left-pad").mkdir(parents=True)
    (repo / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    (repo / "server" / "node_modules").mkdir(parents=True)
    (repo / "server" / "keep.js").write_text("1\n")
    git(repo, "add", "server/keep.js")
    git(repo, "commit", "-q", "-m", "server")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.local.json").write_text('{"permissions": {"allow": ["Bash(npm run build)"]}}')


def test_a_worktree_gets_the_checkouts_dependencies_and_local_settings_never_commits_them(git_repo, tmp_path):
    _with_dependencies(git_repo)
    ws = isolate.prepare(git_repo, 11, tmp_path / "home")
    assert ws.links == (".claude/settings.local.json", "node_modules", "server/node_modules")
    assert (ws.path / "node_modules" / "left-pad" / "index.js").read_text() == "module.exports = 1\n"
    copied = ws.path / ".claude" / "settings.local.json"
    assert not copied.is_symlink() and "npm run build" in copied.read_text()
    assert isolate.changed(ws) == () and not isolate.has_changes(ws)
    # a worker that adds everything and commits commits the links too
    (ws.path / "feature.js").write_text("x\n")
    git(ws.path, "add", "-A")
    git(ws.path, "-c", "user.name=w", "-c", "user.email=w@x", "commit", "-q", "-m", "worker")
    (ws.path / " spaced.js").write_text("y\n")
    (ws.path / "app.py").write_text("changed\n")
    assert isolate.changed(ws) == (" spaced.js", "app.py", "feature.js")
    assert isolate.finish(ws, keep=True, message="m") == "locks/task-11"
    files = git(git_repo, "ls-tree", "-r", "--name-only", "locks/task-11").split()
    assert "feature.js" in files and "app.py" in files
    assert not any("node_modules" in f or "settings.local" in f for f in files)
    assert (git_repo / "node_modules" / "left-pad" / "index.js").exists()
    assert (git_repo / ".claude" / "settings.local.json").exists()


def test_reset_puts_the_links_back(git_repo, tmp_path):
    _with_dependencies(git_repo)
    ws = isolate.prepare(git_repo, 12, tmp_path / "home")
    (ws.path / "node_modules").unlink()
    isolate.reset(ws)
    assert (ws.path / "node_modules").is_symlink()
    isolate.finish(ws, keep=False, message="m")
    assert (git_repo / "node_modules" / "left-pad").is_dir()


def test_a_kept_branch_is_where_a_resumed_task_continues(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 13, tmp_path / "home")
    assert isolate.kept(git_repo, 13)  # the branch exists while it runs
    (ws.path / "half.py").write_text("half\n")
    assert isolate.finish(ws, keep=True, message="locks task #13 (unverified, blocked)") == "locks/task-13"
    assert isolate.kept(git_repo, 13)
    again = isolate.prepare(git_repo, 13, tmp_path / "home")
    assert (again.path / "half.py").read_text() == "half\n"
    assert again.base == git(git_repo, "rev-parse", "HEAD")
    assert isolate.has_changes(again)  # the kept work alone keeps the branch
    assert isolate.finish(again, keep=True, message="m") == "locks/task-13"


def test_a_run_in_a_folder_holds_the_checkouts_inside_it(tmp_path):
    from locks import isolate

    (tmp_path / "api" / ".git").mkdir(parents=True)
    (tmp_path / "web" / ".git").mkdir(parents=True)
    (tmp_path / "notes").mkdir()
    folder = isolate.held_by(tmp_path, None)
    assert folder == sorted([tmp_path.resolve(), (tmp_path / "api").resolve(), (tmp_path / "web").resolve()])
    assert isolate.held_by(tmp_path / "api", tmp_path / "api") == [(tmp_path / "api").resolve()]
    assert isolate.held_by(tmp_path / "gone", None) == [(tmp_path / "gone").resolve()]


def test_tool_artifacts_are_no_work():
    assert isolate.is_artifact(".playwright-mcp/page-1.yml")
    assert isolate.is_artifact("web/.playwright-mcp/console.log")
    assert not isolate.is_artifact("src/playwright-mcp.ts")
    assert isolate.is_artifact(".fresh-dev/dirty.json")  # a dev-server plugin's own state
    assert not isolate.is_artifact(".playwright-mcp")
    assert isolate.is_artifact(".mcp-docs/docs.db")  # the database a worker's livespec index builds


def test_a_kept_branch_never_commits_tool_artifacts(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 14, tmp_path / "home")
    (ws.path / ".playwright-mcp").mkdir()
    (ws.path / ".playwright-mcp" / "page.yml").write_text("snapshot")
    assert not isolate.has_changes(ws)
    (ws.path / "fix.py").write_text("x = 1\n")
    assert isolate.finish(ws, keep=True, message="m") == "locks/task-14"
    shown = git(git_repo, "show", "--name-only", "locks/task-14")
    assert "fix.py" in shown
    assert ".playwright-mcp" not in shown


def test_a_tracked_artifact_a_check_deleted_or_rewrote_is_never_committed(git_repo, tmp_path):
    """A repository that tracks `__pycache__`: its deletion was committed as the work, and read by its review."""
    (git_repo / "pkg" / "__pycache__").mkdir(parents=True)
    (git_repo / "pkg" / "__pycache__" / "a.pyc").write_bytes(b"old")
    (git_repo / "pkg" / "__pycache__" / "b.pyc").write_bytes(b"old")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-qm", "tracked bytecode")
    ws = isolate.prepare(git_repo, 16, tmp_path / "home")
    (ws.path / "pkg" / "__pycache__" / "a.pyc").unlink()
    (ws.path / "pkg" / "__pycache__" / "b.pyc").write_bytes(b"new")
    (ws.path / "pkg" / "__pycache__" / "c.pyc").write_bytes(b"new")
    (ws.path / "fix.py").write_text("x = 1\n")
    assert isolate.finish(ws, keep=True, message="m") == "locks/task-16"
    assert git(git_repo, "show", "--name-only", "--format=", "locks/task-16").split() == ["fix.py"]


def test_an_index_a_worker_built_in_its_worktree_is_no_work_and_is_never_committed(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 15, tmp_path / "home")
    (ws.path / ".mcp-docs").mkdir()
    (ws.path / ".mcp-docs" / "docs.db").write_text("index")
    assert not isolate.has_changes(ws)
    (ws.path / "fix.py").write_text("x = 1\n")
    assert isolate.changed(ws) == ("fix.py",)
    assert isolate.finish(ws, keep=True, message="m") == "locks/task-15"
    assert ".mcp-docs" not in git(git_repo, "show", "--name-only", "locks/task-15")


def test_a_commit_message_reads_as_the_change_with_the_task_in_its_trailers():
    msg = isolate.message(1144, title="Repo wapp-admin: the palette of the admin is wrong everywhere",
                          subject="Use the brand palette in the admin theme", summary="theme.css now reads tokens",
                          status="blocked")
    subject, body, trailers = msg.split("\n\n")
    assert subject == "Use the brand palette in the admin theme" and body == "theme.css now reads tokens"
    assert trailers.splitlines() == ["Locks-Task: #1144", "Locks-Verified: no (blocked)"]
    done = isolate.message(7, title="x " * 60, summary="")
    assert not done.split("\n\n")[0].endswith("…") and len(done.split("\n\n")[0]) <= isolate.SUBJECT_CHARS
    assert done.split("\n\n")[-1] == "Locks-Task: #7"
    assert isolate.message(8, title="", subject="key sk-abcdefghijklmnop").startswith("key [secret]")
    assert isolate.message(9, title="").startswith("Work of locks task #9")
    long = isolate.message(10, title="t", subject="word " * 30, summary="s " * 600)
    assert len(long.splitlines()[0]) <= isolate.SUBJECT_CHARS and long.split("\n\n")[-2].endswith("…")


def test_a_commit_subject_never_ends_in_an_ellipsis_and_the_whole_text_goes_in_the_body():
    long = "Add client role and portal, Render deploys linked to tickets and the audit trail of every change"
    for given in ({"title": long}, {"title": "t", "subject": long}):
        parts = isolate.message(46, summary="the work", **given).split("\n\n")
        subject = parts[0]
        assert len(subject) <= isolate.SUBJECT_CHARS and not subject.endswith(("…", "...", ",", " "))
        assert long.startswith(subject) and subject.endswith("linked to tickets")
        assert " ".join(parts[1].split()) == long and parts[2] == "the work"
    # A line that fits is the worker's, as proposed, and adds nothing to the body.
    fits = isolate.message(47, title=long, subject="Add the client portal", summary="")
    assert fits.split("\n\n") == ["Add the client portal", "Locks-Task: #47"]
    # No space to cut at: the limit is the cut, still no ellipsis.
    assert len(isolate.message(48, title="", subject="x" * 200).splitlines()[0]) == isolate.SUBJECT_CHARS


def test_a_cut_subject_ends_on_neither_a_dangling_word_nor_an_open_parenthesis():
    ends_on_its = "Refactor the orchestrator so that every attempt of a task reads its final ending from the store"
    assert isolate._subject_of(ends_on_its) == "Refactor the orchestrator so that every attempt of a task reads"
    open_paren = "Fix the thing (see the other thing, and the other, and then some more words) because yes yes"
    assert isolate._subject_of(open_paren) == "Fix the thing"
    # Only dangling words: the whole-word cut stands rather than collapsing to the first word.
    only_dangling = isolate._subject_of("A " * 50)
    assert only_dangling == " ".join(["A"] * 36) and len(only_dangling) <= isolate.SUBJECT_CHARS


def test_work_kept_unverified_under_the_old_subject_is_still_read_as_such(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 14, tmp_path / "home")
    (ws.path / "half.py").write_text("half\n")
    isolate.finish(ws, keep=True, message="locks task #14 (unverified, blocked): half")
    again = isolate.prepare(git_repo, 14, tmp_path / "home")
    isolate.finish(again, keep=True, message=isolate.message(14, title="half"))
    assert git(git_repo, "log", "-1", "--format=%s", "locks/task-14") == "half"  # it passed since: said so


def test_a_resume_after_its_process_died_continues_in_the_worktree_it_left(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 15, tmp_path / "home")
    (ws.path / "half.py").write_text("x = 1\n")  # the process dies here: no finish
    again = isolate.prepare(git_repo, 15, tmp_path / "home")
    assert again.path == ws.path and again.branch == "locks/task-15"
    assert (again.path / "half.py").read_text() == "x = 1\n"
    assert again.base == git(git_repo, "rev-parse", "HEAD").strip()


def _commit_in(path, name, text):
    (path / name).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", name)
    return git(path, "rev-parse", "HEAD")


def _lose(repo, sha):
    """What a git killed by a full disk leaves: a commit the branch names that cannot be read."""
    (repo / ".git" / "objects" / sha[:2] / sha[2:]).unlink()


@pytest.mark.parametrize("died", [True, False])
def test_a_resume_mends_a_branch_whose_newest_commit_a_full_disk_lost(git_repo, tmp_path, died):
    ws = isolate.prepare(git_repo, 16, tmp_path / "home")
    good = _commit_in(ws.path, "half.py", "x = 1\n")
    lost = _commit_in(ws.path, "more.py", "y = 2\n")
    if not died:
        git(git_repo, "worktree", "remove", "--force", str(ws.path))
    _lose(git_repo, lost)
    again = isolate.prepare(git_repo, 16, tmp_path / "home")
    assert again.base == git(git_repo, "rev-parse", "HEAD")
    assert git(git_repo, "rev-parse", "refs/heads/locks/task-16") == good
    assert (again.path / "half.py").read_text() == "x = 1\n"
    if died:
        # The lost commit's files stay where the run left them, as changes to commit.
        assert (again.path / "more.py").read_text() == "y = 2\n" and isolate.has_changes(again)


def test_a_branch_with_nothing_readable_stops_the_resume_in_a_line(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 17, tmp_path / "home")
    lost = _commit_in(ws.path, "half.py", "x = 1\n")
    _lose(git_repo, lost)
    # Its reflog's only other commit is gone too: nothing to go back to.
    (git_repo / ".git" / "logs" / "refs" / "heads" / "locks" / "task-17").unlink()
    with pytest.raises(isolate.IsolationError) as err:
        isolate.prepare(git_repo, 17, tmp_path / "home")
    said = str(err.value)
    assert said.startswith("the kept branch locks/task-17 cannot be read after a git failure (")
    assert "fsck" in said and "\n" not in said
    assert git(git_repo, "rev-parse", "refs/heads/locks/task-17") == lost  # kept as it was


def test_a_base_that_took_in_the_branch_s_own_work_under_another_commit_is_not_ahead_of_it(git_repo):
    """A run started on another run's kept branch; the base then got that run squashed. The
    base's tree is one the branch already had, so it brings nothing: no commits behind, no
    file both changed, even where the branch went on changing those files."""
    from .conftest import git

    base = git(git_repo, "symbolic-ref", "--short", "HEAD")
    git(git_repo, "checkout", "-q", "-b", "run")
    (git_repo / "a.txt").write_text("1\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "the earlier run, unsquashed")
    first = git(git_repo, "rev-parse", "HEAD")
    (git_repo / "a.txt").write_text("1\n2\n")
    (git_repo / "b.txt").write_text("b\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "this run's own work")
    git(git_repo, "checkout", "-q", base)
    git(git_repo, "checkout", "-q", first, "--", "a.txt")
    git(git_repo, "commit", "-q", "-m", "the earlier run, squashed")
    found = isolate.behind(git_repo, "run", base)
    assert found == {"base": base, "commits": 0, "both": []}
    assert isolate.behind_text(found) is None

    # What the base gains after that is behind again, measured from where they last agreed.
    (git_repo / "a.txt").write_text("1\nlater\n")
    (git_repo / "c.txt").write_text("c\n")
    git(git_repo, "add", "-A")
    git(git_repo, "commit", "-q", "-m", "someone else's change")
    assert isolate.behind(git_repo, "run", base) == {"base": base, "commits": 1, "both": ["a.txt"]}


def test_dependencies_installed_for_another_lockfile_are_not_linked(git_repo, tmp_path):
    _with_dependencies(git_repo)
    (git_repo / "package-lock.json").write_text('{"lockfileVersion": 3, "packages": {"pg": {}}}\n')
    git(git_repo, "add", "package-lock.json")
    git(git_repo, "commit", "-q", "-m", "add pg")
    base = git(git_repo, "rev-parse", "HEAD").strip()
    # The checkout moved back to dependencies without pg, and its node_modules was installed from them.
    git(git_repo, "checkout", "-q", "HEAD~1")
    ws = isolate.prepare(git_repo, 12, tmp_path / "home", base=base)
    # The step builds on the commit with pg: the checkout's node_modules is not what it needs.
    assert "node_modules" in ws.stale and "node_modules" not in ws.links
    assert not (ws.path / "node_modules").exists()
    # server/ has no lockfile on either side: still shared.
    assert "server/node_modules" in ws.links


def test_a_base_that_cannot_be_read_is_refused_never_replaced_by_head(git_repo, tmp_path):
    with pytest.raises(isolate.IsolationError, match="is gone"):
        isolate.prepare(git_repo, 13, tmp_path / "home", base="0" * 40)


def test_a_task_that_never_ran_sets_aside_a_branch_of_its_name_and_starts_on_its_base(git_repo, tmp_path):
    head = git(git_repo, "rev-parse", "HEAD").strip()
    git(git_repo, "checkout", "-q", "-b", "locks/task-21")
    old = _commit_in(git_repo, "foreign.py", "another store's work\n").strip()
    git(git_repo, "checkout", "-q", "-")
    ws = isolate.prepare(git_repo, 21, tmp_path / "home", fresh=True)
    assert ws.set_aside == f"locks/foreign/task-21-{old[:12]}"
    assert ws.base == head and not (ws.path / "foreign.py").exists()
    # nothing is lost, and the set-aside branch is no kept branch of any task
    assert git(git_repo, "rev-parse", ws.set_aside).strip() == old
    assert 21 in isolate.kept_branches(git_repo) and isolate.kept_branches(git_repo)[21] == head
    assert len(isolate.kept_branches(git_repo)) == 1
    # a fresh task with no such branch sets nothing aside
    assert isolate.prepare(git_repo, 22, tmp_path / "home", fresh=True).set_aside is None


def test_a_task_folder_git_no_longer_knows_is_moved_aside_and_the_resume_adds_its_worktree(git_repo, tmp_path):
    """A cancel left a task's folder holding a build cache, and the resume failed to add
    its worktree until the person deleted it by hand."""
    home = tmp_path / "home"
    stray = home / "worktrees" / "task-11"
    (stray / ".angular" / "cache").mkdir(parents=True)
    (stray / ".angular" / "cache" / "blob").write_text("x")
    ws = isolate.prepare(git_repo, 11, home)
    assert ws.path == stray and (ws.path / "app.py").is_file() and not (ws.path / ".angular").exists()
    moved = list((home / "worktrees" / "stray").iterdir())
    assert ws.stray == moved[0]
    assert len(moved) == 1 and (moved[0] / ".angular" / "cache" / "blob").is_file()  # never deleted


def test_finishing_removes_what_git_left_of_the_worktree(git_repo, tmp_path):
    ws = isolate.prepare(git_repo, 12, tmp_path / "home")
    (ws.path / ".angular").mkdir()
    (ws.path / ".angular" / "blob").write_text("x")
    git(git_repo, "worktree", "remove", "--force", str(ws.path))
    (ws.path / ".angular").mkdir(parents=True)  # a dev server wrote again while it went
    isolate.finish(ws, keep=False, message="locks task #12")
    assert not ws.path.exists()


def test_a_branch_far_behind_the_one_work_lands_on_is_said_and_one_close_to_it_is_not(git_repo):
    """A workflow run started from a feature branch 27 commits behind the trunk and nobody said so."""
    trunk = git(git_repo, "symbolic-ref", "--short", "HEAD")
    git(git_repo, "checkout", "-q", "-b", "feature/specs")
    git(git_repo, "checkout", "-q", trunk)
    for n in range(isolate.TRUNK_BEHIND):
        git(git_repo, "commit", "-q", "--allow-empty", "-m", f"trunk {n}")
    assert isolate.behind_trunk(git_repo) is None  # on the trunk itself
    git(git_repo, "checkout", "-q", "feature/specs")
    said = isolate.behind_trunk(git_repo)
    assert said.startswith("base: ") and f"{isolate.TRUNK_BEHIND} commits behind {trunk}" in said
    git(git_repo, "reset", "-q", "--hard", f"{trunk}~1")
    assert isolate.behind_trunk(git_repo) is None


def _timeout(argv, **kwargs):
    import subprocess

    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))


def test_git_that_outlasts_its_timeout_is_an_isolation_error(git_repo, monkeypatch):
    monkeypatch.setattr(isolate.subprocess, "run", _timeout)
    with pytest.raises(isolate.IsolationError, match=r"git status timed out after 120s"):
        isolate._git(git_repo, "status")
    with pytest.raises(isolate.IsolationError, match="timed out"):
        isolate._git(git_repo, "status", check=False)


def test_every_git_call_is_bounded_and_never_prompts(git_repo, monkeypatch):
    seen = []
    real = isolate.subprocess.run

    def spy(argv, **kwargs):
        seen.append(kwargs)
        return real(argv, **kwargs)

    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    monkeypatch.setattr(isolate.subprocess, "run", spy)
    isolate._git(git_repo, "status")
    isolate.numstat(git_repo, "HEAD", "HEAD")
    assert isolate.GIT_TIMEOUT_S == 120
    assert seen and all(k["timeout"] == isolate.GIT_TIMEOUT_S for k in seen)
    assert all(k["env"]["GIT_TERMINAL_PROMPT"] == "0" for k in seen)
    assert "PATH" in seen[0]["env"]  # the process's own environment is kept


def test_a_git_that_hangs_fails_each_caller_the_way_it_already_fails(git_repo, tmp_path, monkeypatch):
    ws = isolate.prepare(git_repo, 31, tmp_path / "home")
    monkeypatch.setattr(isolate.subprocess, "run", _timeout)
    assert isolate.numstat(git_repo, "HEAD", "HEAD") is None
    assert isolate.generated(git_repo, ["a.py"]) == set()
    with pytest.raises(isolate.IsolationError, match="timed out"):  # read through _git, which raises
        isolate.landed(git_repo, "locks/task-31")
    assert isolate.delete_branch(git_repo, ws.branch) is False
    assert isolate.snapshot(ws.path) is None
    assert isolate.fingerprint(ws.path) is None
    assert isolate.put_back(ws.path, "HEAD", ["app.py"]) == ["app.py"]
    assert isolate.ignored_named(ws, "see ./nothing.txt") == []
    assert isolate.adopt_legacy(git_repo) == []
    with pytest.raises(isolate.IsolationError, match="timed out"):
        isolate.undo(ws, ["app.py"])
    with pytest.raises(isolate.IsolationError, match="timed out"):
        isolate.join(ws, ["locks/other"], message=lambda b: b)


def test_no_git_subprocess_call_in_isolate_or_merging_lacks_a_timeout():
    import ast
    from pathlib import Path

    checked = 0
    for name in ("isolate.py", "merging.py"):
        path = Path(isolate.__file__).with_name(name)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.List)):
                continue
            func = ast.unparse(node.func)
            first = node.args[0].elts[0] if node.args[0].elts else None
            if func not in ("subprocess.run", "run") or not (isinstance(first, ast.Constant) and first.value == "git"):
                continue
            checked += 1
            assert "timeout" in {k.arg for k in node.keywords}, f"{name}:{node.lineno} runs git with no timeout"
    assert checked >= 3  # isolate._run_git, merging._git and merging.open_pr's push


def test_a_release_bump_of_only_the_projects_own_version_leaves_dependencies_linked(git_repo, tmp_path):
    lock = ('version = 1\n\n[[package]]\nname = "dep"\nversion = "1.0"\n\n[[package]]\nname = "me"\n'
            'version = "%s"\nsource = { editable = "." }\n')
    (git_repo / ".venv").mkdir()
    (git_repo / ".venv" / "x").write_text("x")
    (git_repo / "uv.lock").write_text(lock % "1.0")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "uv.lock").write_text(lock % "1.1")
    assert not isolate._stale(git_repo, wt, ".venv")
    (wt / "uv.lock").write_text((lock % "1.1").replace('version = "1.0"', 'version = "2.0"'))
    assert isolate._stale(git_repo, wt, ".venv")


def test_package_lock_ignores_only_the_top_level_version(tmp_path):
    def lock(version: str, dep: str) -> str:
        return ('{"version": "' + version + '", "packages": {"": {"version": "' + version
                + '"}, "node_modules/a": {"version": "' + dep + '"}}}')

    mine, theirs = tmp_path / "w", tmp_path / "c"
    for d in (mine, theirs):
        (d / "node_modules").mkdir(parents=True)
    (theirs / "package-lock.json").write_text(lock("1.0.0", "3.0.0"))
    (mine / "package-lock.json").write_text(lock("1.1.0", "3.0.0"))
    assert not isolate._stale(theirs, mine, "node_modules")
    (mine / "package-lock.json").write_text(lock("1.1.0", "3.1.0"))
    assert isolate._stale(theirs, mine, "node_modules")


def test_files_that_differ_only_because_head_moved_are_not_the_runs_changes(git_repo):
    place = isolate.stand(git_repo, [git_repo])[0]
    git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "merged.txt").write_text("m\n")
    git(git_repo, "add", "merged.txt")
    git(git_repo, "commit", "-q", "-m", "side work")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merged by the session", "side")
    assert isolate.moved_in_place(place) == ()
    (git_repo / "wrote.txt").write_text("w\n")
    assert isolate.moved_in_place(place) == ("wrote.txt",)


def test_a_merge_commit_that_moved_head_is_not_the_runs_but_a_commit_on_the_branch_is(git_repo):
    place = isolate.stand(git_repo, [git_repo])[0]
    git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "from_side.txt").write_text("s\n")
    git(git_repo, "add", "from_side.txt")
    git(git_repo, "commit", "-q", "-m", "side")
    git(git_repo, "checkout", "-q", "main")
    git(git_repo, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    assert isolate.moved_in_place(place) == ()
    (git_repo / "own.txt").write_text("o\n")
    git(git_repo, "add", "own.txt")
    git(git_repo, "commit", "-q", "-m", "worker")
    assert isolate.moved_in_place(place) == ("own.txt",)


def test_a_staged_rename_names_both_paths_whole(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`git status -z` gives a rename as `R  new\\0old`: the old name, read as an entry with a status
    before it, lost three characters (`src/a.py` as `/a.py`), a path outside every surface, and the
    work of a part that moved files was undone as a wrong approach."""
    import subprocess

    from locks import launch

    repo = tmp_path / "r"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "auto.py").write_text("RATE = 1\n" * 20)
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"]):
        subprocess.run(["git", *argv], cwd=repo, check=True)
    (repo / "src" / "pkg" / "line").mkdir()
    subprocess.run(["git", "mv", "src/pkg/auto.py", "src/pkg/line/auto.py"], cwd=repo, check=True)

    assert isolate.dirty(repo) == ["src/pkg/auto.py", "src/pkg/line/auto.py"]
    state = launch._status(repo)
    assert state is not None
    assert sorted(p for p in state if not p.startswith("\0")) == ["src/pkg/auto.py", "src/pkg/line/auto.py"]


def _built_repo(tmp_path):  # type: ignore[no-untyped-def]
    import subprocess

    repo = tmp_path / "site"
    (repo / "_fresh").mkdir(parents=True)
    (repo / "_fresh" / "app.js").write_text("build 1\n")
    (repo / "routes.ts").write_text("export const a = 1;\n")
    for argv in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"]):
        subprocess.run(["git", *argv], cwd=repo, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    return repo, isolate.Workspace(repo_dir=repo, path=repo, branch="locks/task-1", base=base)


def test_a_committed_build_folder_the_check_rebuilt_goes_back_to_the_base(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Every task whose check builds rewrote the committed `_fresh/`, and two such branches conflicted
    there when merged. The source stays changed; the build is as at the base, its new files gone."""
    repo, ws = _built_repo(tmp_path)
    (repo / "routes.ts").write_text("export const a = 2;\n")
    (repo / "_fresh" / "app.js").write_text("build 2\n")
    (repo / "_fresh" / "chunk-9.js").write_text("new\n")

    restored = isolate.restore_built(ws, ["_fresh/app.js", "_fresh/chunk-9.js"])

    assert restored == {"_fresh": 2}
    assert (repo / "_fresh" / "app.js").read_text() == "build 1\n"
    assert not (repo / "_fresh" / "chunk-9.js").exists()
    assert isolate.changed(ws) == ("routes.ts",)


def test_a_build_folder_the_task_names_or_that_was_never_committed_is_kept(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo, ws = _built_repo(tmp_path)
    (repo / "_fresh" / "app.js").write_text("build 2\n")
    (repo / "dist").mkdir()
    (repo / "dist" / "out.js").write_text("x\n")

    assert isolate.restore_built(ws, ["_fresh/app.js"], "Rebuild and commit _fresh/ for the deploy") == {}
    assert isolate.restore_built(ws, ["dist/out.js"]) == {}
    assert (repo / "_fresh" / "app.js").read_text() == "build 2\n"
    assert (repo / "dist" / "out.js").exists()
    assert isolate.built_dirs(["src/build.py", "packages/web/dist/a.js"]) == ["packages/web/dist"]
