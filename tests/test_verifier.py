import asyncio

from engine import verifier


def test_pass(tmp_path):
    v = asyncio.run(verifier.verify("exit 0", cwd=str(tmp_path)))
    assert v.passed and v.exit_code == 0


def test_fail_with_output(tmp_path):
    v = asyncio.run(verifier.verify("echo boom >&2; exit 3", cwd=str(tmp_path)))
    assert not v.passed
    assert v.exit_code == 3
    assert "boom" in v.output  # stderr ikut ke-capture


def test_output_capped_from_tail(tmp_path):
    v = asyncio.run(verifier.verify(
        "python3 -c \"print('a'*9000); print('EKOR')\"",
        cwd=str(tmp_path), output_cap=200,
    ))
    assert len(v.output) < 300
    assert "EKOR" in v.output          # bagian akhir yang disimpan
    assert v.output.startswith("...[dipotong]...")


def test_timeout(tmp_path):
    v = asyncio.run(verifier.verify("sleep 5", cwd=str(tmp_path), timeout_sec=1))
    assert not v.passed
    assert "timeout" in v.output
