from __future__ import annotations

from locks import hooks
from locks.store import Store

from .conftest import git


def _kept(store: Store, repo, text: str, changed: list[str], base: str) -> dict:
    task = store.create_task(text, status="done", source="queue", session_id="s", repo="r", cwd=str(repo))
    git(repo, "branch", f"locks/task-{task['id']}")
    store.add_event(task["id"], "finished", status="done", changed=changed, base=base,
                    branch=f"locks/task-{task['id']}")
    return task


def test_a_kept_branch_whose_files_head_already_holds_is_not_named_in_the_overlap(store: Store, git_repo):
    base = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "-b", "side")
    (git_repo / "a.txt").write_text("merged\n")
    git(git_repo, "add", "a.txt")
    git(git_repo, "commit", "-q", "-m", "side")
    tip = git(git_repo, "rev-parse", "HEAD")
    git(git_repo, "checkout", "-q", "main")
    other = _kept(store, git_repo, "merged work", ["a.txt"], base)
    git(git_repo, "branch", "-f", f"locks/task-{other['id']}", tip)
    mine = _kept(store, git_repo, "mine", ["a.txt"], base)
    ended = {**store.get_task(mine["id"]), "finished_data": store.final_event(mine["id"])["data"]}
    assert any(f"#{other['id']} " in line for line in hooks._overlaps(store, ended))
    # The same content lands on main by a different commit: byte for byte, so it is merged.
    (git_repo / "a.txt").write_text("merged\n")
    git(git_repo, "add", "a.txt")
    git(git_repo, "commit", "-q", "-m", "same content")
    assert hooks._overlaps(store, ended) == []
