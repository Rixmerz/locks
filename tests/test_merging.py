"""Merging the branches tasks kept, on the person's word (`locks merge`)."""
from __future__ import annotations

from locks import cli, merging
from locks.store import Store

from .conftest import git


def _kept(store: Store, repo, text: str, files: dict[str, str]) -> dict:
    task = store.create_task(text, status="done", source="locks", repo="r", cwd=str(repo))
    git(repo, "checkout", "-q", "-b", f"locks/task-{task['id']}")
    for name, body in files.items():
        (repo / name).write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", text)
    git(repo, "checkout", "-q", "main")
    store.add_event(task["id"], "finished", status="done", changed=sorted(files), branch=f"locks/task-{task['id']}")
    return task


def test_the_branches_that_touch_no_other_go_first():
    assert merging.order({3: ["a.ts", "b.ts"], 4: ["c.ts"], 5: ["b.ts"]}) == [4, 3, 5]


def test_merge_takes_each_branch_in_order_and_stops_at_a_conflict(git_repo, capsys):
    store = Store.open()
    try:
        alone = _kept(store, git_repo, "add the forum", {"forum.ts": "forum\n"})
        first = _kept(store, git_repo, "add the API service", {"api.spec.ts": "first\n"})
        clash = _kept(store, git_repo, "add the admin", {"api.spec.ts": "second\n", "admin.ts": "a\n"})
    finally:
        store.close()
    assert cli.main(["merge", str(first["id"]), str(alone["id"]), str(clash["id"]), "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith(f"would merge, in this order: #{alone['id']}")
    assert cli.main(["merge", str(first["id"]), str(alone["id"]), str(clash["id"]), "--verify", "true"]) == 1
    out = capsys.readouterr()
    assert f"merged #{alone['id']}" in out.out and "`true` passes after it" in out.out
    assert f"#{clash['id']} (locks/task-{clash['id']}) conflicts with what is in main in api.spec.ts" in out.err
    assert "locks run 'Merge branch locks/task-" in out.err and f"not merged: #{clash['id']}" in out.out
    assert (git_repo / "forum.ts").exists() and (git_repo / "api.spec.ts").read_text() == "first\n"
    assert git(git_repo, "status", "--porcelain").strip() == ""  # the conflicting merge was aborted


def test_merge_refuses_a_dirty_checkout_and_stops_where_the_check_fails(git_repo, capsys):
    store = Store.open()
    try:
        one = _kept(store, git_repo, "add a", {"a.ts": "a\n"})
    finally:
        store.close()
    (git_repo / "README.md").write_text("changed\n")
    git(git_repo, "add", "README.md")
    assert cli.main(["merge", str(one["id"])]) == 1
    assert "uncommitted changes" in capsys.readouterr().err
    git(git_repo, "reset", "-q", "--hard")
    assert cli.main(["merge", str(one["id"]), "--verify", "false"]) == 1
    assert "`false` fails after merging" in capsys.readouterr().err
    assert cli.main(["merge", "999"]) == 1


def test_a_pull_request_carries_the_verdict_and_opens_only_through_gh(git_repo, capsys, monkeypatch):
    import subprocess as sp

    store = Store.open()
    try:
        task = _kept(store, git_repo, "add the export", {"export.py": "x\n"})
        store.update_task(task["id"], result="Added the CSV export.")
        store.add_event(task["id"], "finished", status="done", changed=["export.py"],
                        verdict={"check": {"command": "pytest -q"},
                                 "criteria": [{"id": "S1", "met": True, "evidence": "test_export passes"}]})
    finally:
        store.close()
    assert cli.main(["pr", str(task["id"]), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("title: add the export") and "Added the CSV export." in out
    assert "- check: `pytest -q` passed, run by locks in its worktree" in out and "- `export.py`" in out
    assert "do not re-read" not in out
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return sp.CompletedProcess(argv, 0, "https://example.test/pr/1\n", "")

    opened, said = merging.open_pr(git_repo, f"locks/task-{task['id']}", "t", "b", run=run)
    assert opened and said == "https://example.test/pr/1"
    assert calls[0][:3] == ["git", "push", "-u"] and calls[1][:4] == ["gh", "pr", "create", "--draft"]
    refused = merging.open_pr(git_repo, "x", "t", "b", run=lambda argv, **kw: sp.CompletedProcess(argv, 1, "", "no"))
    assert refused == (False, "git push failed: no")
    assert cli.main(["pr", "999"]) == 1


def test_merge_with_pr_goes_onto_its_own_branch_and_opens_one_draft(git_repo, capsys, monkeypatch):
    import subprocess as sp

    store = Store.open()
    try:
        one = _kept(store, git_repo, "add the forum", {"forum.ts": "forum\n"})
        two = _kept(store, git_repo, "add the admin", {"admin.ts": "a\n"})
    finally:
        store.close()
    opened = []

    def fake(top, branch, title, body, *, base=None):
        opened.append((branch, title, body, base))
        return True, "https://example.test/pr/2"

    real_open_pr = merging.open_pr
    monkeypatch.setattr(merging, "open_pr", fake)
    assert cli.main(["merge", str(one["id"]), str(two["id"]), "--pr", "--verify", "true"]) == 0
    branch, title, body, base = opened[0]
    assert branch == f"locks/merge-{one['id']}-{two['id']}" and base == "main"
    assert title == "Merge 2 locks branches" and f"- #{one['id']} add the forum" in body
    assert "https://example.test/pr/2" in capsys.readouterr().out
    # The person's branch is as it was, and the merges are on the new one.
    assert git(git_repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert not (git_repo / "forum.ts").exists()
    assert "forum.ts" in git(git_repo, "ls-tree", "-r", "--name-only", branch)
    # A failing check stops before any push, on the new branch.
    store = Store.open()
    try:
        three = _kept(store, git_repo, "add the shop", {"shop.ts": "s\n"})
    finally:
        store.close()
    assert cli.main(["merge", str(three["id"]), "--pr", "--verify", "false"]) == 1
    assert "stays checked out" in capsys.readouterr().err and len(opened) == 1
    git(git_repo, "checkout", "-q", "main")
    # The pull request names the repository and the head in full.
    git(git_repo, "remote", "add", "origin", "http://proxy@127.0.0.1:9/git/Owner/project")
    assert merging.github_repo(git_repo) == "Owner/project"
    calls = []
    real_open_pr(git_repo, "b", "t", "x", run=lambda argv, **kw: calls.append(argv) or sp.CompletedProcess(
        argv, 0, "ok", ""))
    assert calls[1][calls[1].index("--head") + 1] == "Owner:b" and calls[1][-2:] == ["--repo", "Owner/project"]


def test_a_git_that_outlasts_its_timeout_is_a_failed_call_not_a_hang(git_repo, monkeypatch):
    import subprocess as sp

    def hang(argv, **kw):
        raise sp.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(merging.subprocess, "run", hang)
    done = merging._git(git_repo, "status")
    assert done.returncode == 124 and done.stdout == "" and "git status timed out after 120s" in done.stderr
    assert merging.github_repo(git_repo) is None


def test_git_calls_of_merging_and_the_push_are_bounded_and_never_prompt(git_repo, monkeypatch):
    import subprocess as sp

    seen = []
    real = merging.subprocess.run

    def spy(argv, **kw):
        seen.append(kw)
        return real(argv, **kw)

    monkeypatch.setattr(merging.subprocess, "run", spy)
    merging._git(git_repo, "status")
    assert seen[0]["timeout"] == 120 and seen[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"

    pushes = []

    def run(argv, **kw):
        pushes.append(kw)
        raise sp.TimeoutExpired(argv, kw["timeout"])

    assert merging.open_pr(git_repo, "b", "t", "x", run=run) == (False, "git push timed out after 120s")
    assert pushes[0]["timeout"] == 120 and pushes[0]["env"]["GIT_TERMINAL_PROMPT"] == "0"
