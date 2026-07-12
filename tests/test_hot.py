from engine.memory import hot


def test_seed_claudemd_goal_lock(tmp_path):
    hot.seed_claudemd(str(tmp_path), "benerin test")
    text = (tmp_path / "CLAUDE.md").read_text()
    assert text.splitlines()[0] == "# GOAL: benerin test"  # goal-lock: baris atas
    assert hot.FACTS_HEADER in text


def test_seed_with_lessons(tmp_path):
    hot.seed_claudemd(str(tmp_path), "g", lessons=["jangan pakai sudo", "cache di /tmp"])
    text = (tmp_path / "CLAUDE.md").read_text()
    assert "- jangan pakai sudo" in text
    assert "- cache di /tmp" in text


def test_append_fact(tmp_path):
    hot.seed_claudemd(str(tmp_path), "g")
    hot.append_fact(str(tmp_path), "port 8080 udah kepake")
    assert "- port 8080 udah kepake" in (tmp_path / "CLAUDE.md").read_text()


def test_claudemd_cap_evicts_oldest_fact(tmp_path):
    hot.seed_claudemd(str(tmp_path), "g")
    hot.append_fact(str(tmp_path), "FAKTA-PERTAMA " + "x" * 300)
    for i in range(10):
        hot.append_fact(str(tmp_path), f"fakta-{i} " + "y" * 300)
    text = (tmp_path / "CLAUDE.md").read_text()
    assert len(text.encode()) <= hot.CLAUDEMD_CAP
    assert "FAKTA-PERTAMA" not in text          # paling lama kebuang
    assert "fakta-9" in text                    # paling baru tetap ada
    assert text.startswith("# GOAL:")           # goal nggak pernah kebuang


def test_journal_roundtrip(tmp_path):
    wd = str(tmp_path)
    assert hot.recent_journal(wd) == []
    assert hot.journal_block(wd) == ""
    for i in range(1, 8):
        hot.append_journal(wd, {"idx": i, "action_summary": f"aksi {i}",
                                "verifier_passed": i == 7})
    recent = hot.recent_journal(wd, n=5)
    assert [e["idx"] for e in recent] == [3, 4, 5, 6, 7]  # tail
    block = hot.journal_block(wd, n=3)
    assert "APA YANG UDAH DICOBA" in block
    assert "iter 7: aksi 7 → PASS" in block
    assert "iter 5: aksi 5 → FAIL" in block
    assert "iter 1" not in block
