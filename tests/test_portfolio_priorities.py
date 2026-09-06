import hashlib
import json
from datetime import date

import pytest

from nightshift.bag import select_bag, run_bag, load_bag
from nightshift.config import Settings
from nightshift.demo import seed_widget
from nightshift.gitops import git
from nightshift.models import SafetyError
from nightshift import priorities


def row(ident, allocation, group="Focus"):
    return dict(id=ident, allocation=allocation, group=group,
                repo=f"https://github.com/sw30labs/{ident}",
                night=f"Repair the {ident} failing test", human="Review evidence", gate="Passing oracle",
                amend="Prefer bounded evidence to features")


def setup(tmp_path, ns_home, monkeypatch, rows):
    roots = tmp_path / "repos"
    roots.mkdir()
    for r in rows:
        p = seed_widget(roots / r['id'])
        git(p, "remote", "add", "origin", r['repo'])
    raw = json.dumps(dict(date=date.today().isoformat(), projects=rows)).encode()
    monkeypatch.setattr(priorities, "fetch_source", lambda: raw)
    (ns_home / "portfolio.json").write_text(json.dumps(dict(source=priorities.SOURCE)))
    return Settings(home=ns_home, roots=[roots], mock=True, observe=False, max_turns=4), raw


def test_priority_over_recency_and_preview_does_not_spend(tmp_path, ns_home, monkeypatch):
    s, raw = setup(tmp_path, ns_home, monkeypatch, [row("high", 25), row("low", 5)])
    for _ in range(2):
        p = select_bag(s)
        assert [t.name for t in p.targets] == ["high"]
        assert p.priorities['sha256'] == hashlib.sha256(raw).hexdigest()
    assert not (ns_home / "priority-service.json").exists()
    assert load_bag(ns_home)['priorities']['source'] == priorities.SOURCE


def test_rotates_by_attempted_service_and_requires_review(tmp_path, ns_home, monkeypatch):
    s, _ = setup(tmp_path, ns_home, monkeypatch, [row("high", 25), row("low", 5)])
    plan = select_bag(s)
    priorities.record_attempt(ns_home, plan.bag_id, plan.targets[0])
    with pytest.raises(SafetyError, match="Morning Prayers"):
        select_bag(s)
    path = ns_home / "priority-service.json"
    data = json.loads(path.read_text())
    next(iter(data.values()))['review'] = 'reject'
    path.write_text(json.dumps(data))
    assert select_bag(s).targets[0].name == 'low'


def test_zero_gate_and_unapproved_frontier_do_not_run(tmp_path, ns_home, monkeypatch):
    s, _ = setup(tmp_path, ns_home, monkeypatch,
                 [row("zero", 0), row("gated", 20, "Gate"), row("frontier", 10, "Frontier"), row("focus", 5)])
    plan = select_bag(s)
    assert [t.name for t in plan.targets] == ['focus']
    assert len(plan.priorities['diagnostics']) == 3


def test_live_refresh_changes_next_selection(tmp_path, ns_home, monkeypatch):
    s, raw = setup(tmp_path, ns_home, monkeypatch, [row("alpha", 25), row("beta", 5)])
    assert select_bag(s).targets[0].name == 'alpha'
    data = json.loads(raw)
    data['projects'][0]['allocation'] = 0
    monkeypatch.setattr(priorities, 'fetch_source', lambda: json.dumps(data).encode())
    assert select_bag(s).targets[0].name == 'beta'


def test_refresh_failure_never_falls_back(tmp_path, ns_home, monkeypatch):
    s, _ = setup(tmp_path, ns_home, monkeypatch, [row("alpha", 25)])
    select_bag(s)
    def fail(): raise OSError('offline')
    monkeypatch.setattr(priorities, 'fetch_source', fail)
    with pytest.raises(SafetyError, match='no bag started'):
        select_bag(s)
    assert (ns_home / 'priority-error.json').exists()
    assert 'latest attempt' in priorities.morning(ns_home)


def test_pause_allows_preview_but_never_starts_writer(tmp_path, ns_home, monkeypatch):
    s, _ = setup(tmp_path, ns_home, monkeypatch, [row('alpha', 25)])
    (ns_home / 'portfolio.json').write_text(json.dumps(dict(source=priorities.SOURCE, paused=True)))
    plan = select_bag(s)
    with pytest.raises(SafetyError, match='paused'):
        run_bag(plan, s)
    assert not (ns_home / 'priority-service.json').exists()


@pytest.mark.parametrize('weight', [-1, 101, True, float('nan')])
def test_invalid_allocation_rejected(tmp_path, ns_home, monkeypatch, weight):
    s, _ = setup(tmp_path, ns_home, monkeypatch, [row('alpha', weight)])
    with pytest.raises(SafetyError):select_bag(s)


def test_identity_uses_remote_not_folder_name(tmp_path, ns_home, monkeypatch):
    s, _ = setup(tmp_path, ns_home, monkeypatch, [row('alpha', 25)])
    p = s.roots[0] / 'alpha'
    p.rename(s.roots[0] / 'arbitrary-local-name')
    assert select_bag(s).targets[0].priority['id'] == 'alpha'


def test_context_reaches_freeze_and_morning(tmp_path, ns_home, monkeypatch):
    from nightshift.llm import Critic
    s, _ = setup(tmp_path, ns_home, monkeypatch, [row('alpha', 25)])
    original = Critic.propose_brief
    seen = []
    def capture(self, snapshot, size=2):
        seen.append(snapshot)
        return original(self, snapshot, size=size)
    monkeypatch.setattr(Critic, 'propose_brief', capture)
    plan = select_bag(s)
    run_bag(plan, s)
    assert seen and 'Repair the alpha failing test' in seen[0]
    assert plan.priorities['sha256'] in seen[0]
    report = (ns_home / 'morning-prayers.md').read_text()
    assert 'Repair the alpha failing test' in report
    assert 'HOTL disposition' in report
    assert json.loads((ns_home / 'priority-service.json').read_text())
