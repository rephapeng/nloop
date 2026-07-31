from engine.memory import hot


def test_seed_claudemd_goal_lock(tmp_path):
    hot.seed_claudemd(str(tmp_path), "fix the tests")
    text = (tmp_path / "CLAUDE.md").read_text()
    assert text.splitlines()[0] == "# GOAL: fix the tests"  # goal-lock: very first line
    assert hot.FACTS_HEADER in text


def test_seed_with_lessons(tmp_path):
    hot.seed_claudemd(str(tmp_path), "g", lessons=["don't use sudo", "cache in /tmp"])
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "- don't use sudo" in text
    assert "- cache in /tmp" in text


def test_append_fact(tmp_path):
    hot.seed_claudemd(str(tmp_path), "g")
    hot.append_fact(str(tmp_path), "port 8080 is already taken")
    assert "- port 8080 is already taken" in (tmp_path / "CLAUDE.md").read_text()


def test_claudemd_cap_evicts_oldest_fact(tmp_path):
    hot.seed_claudemd(str(tmp_path), "g")
    hot.append_fact(str(tmp_path), "FIRST-FACT " + "x" * 300)
    for i in range(10):
        hot.append_fact(str(tmp_path), f"fact-{i} " + "y" * 300)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert len(text.encode()) <= hot.CLAUDEMD_CAP
    assert "FIRST-FACT" not in text             # oldest one gets evicted
    assert "fact-9" in text                     # newest one survives
    assert text.startswith("# GOAL:")           # the goal is never evicted


def test_journal_roundtrip(tmp_path):
    wd = str(tmp_path)
    assert hot.recent_journal(wd) == []
    assert hot.journal_block(wd) == ""
    for i in range(1, 8):
        hot.append_journal(wd, {"idx": i, "action_summary": f"action {i}",
                                "verifier_passed": i == 7})
    recent = hot.recent_journal(wd, n=5)
    assert [e["idx"] for e in recent] == [3, 4, 5, 6, 7]  # tail
    block = hot.journal_block(wd, n=3)
    assert "WHAT HAS ALREADY BEEN TRIED" in block
    assert "iter 7: action 7 → PASS" in block
    assert "iter 5: action 5 → FAIL" in block
    assert "iter 1" not in block
