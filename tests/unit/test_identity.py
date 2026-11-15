from agent_center.runtime.identity import verify_process_identity


def test_process_identity_fails_closed_without_full_saved_identity() -> None:
    assert not verify_process_identity(0, "/usr/bin/python3", "", "")
    assert not verify_process_identity(99999999, "/usr/bin/python3", "old-start", "/usr/bin/python3 worker")
