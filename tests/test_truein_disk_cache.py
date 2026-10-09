"""The Truein employee list survives a server restart (saved to disk, reloaded on first read)."""
import json
import time

from app.integrations import truein


def _fresh_process(monkeypatch, path):
    """Simulate a restarted server: empty in-memory cache, nothing loaded from disk yet."""
    monkeypatch.setattr(truein, "_DISK_CACHE_PATH", str(path))
    monkeypatch.setattr(truein, "_employees_cache", None)
    monkeypatch.setattr(truein, "_employees_cache_at", 0.0)
    monkeypatch.setattr(truein, "_disk_loaded", False)
    monkeypatch.setattr(truein, "_disk_cache_enabled", lambda: True)


MGR = {"empId": "E1", "name": "Ashwani Kumar", "is_manager": "1"}


def test_saved_pull_is_reloaded_after_restart(tmp_path, monkeypatch):
    f = tmp_path / "cache.json"
    f.write_text(json.dumps({"fetched_at": time.time() - 3600, "data": [MGR, {"empId": "E2", "name": "X", "is_manager": "0"}]}))
    _fresh_process(monkeypatch, f)
    assert truein.get_managers_from_cache_only() == [{"empId": "E1", "name": "Ashwani Kumar"}]
    assert truein.get_cached_employees_if_warm() is not None


def test_too_old_copy_is_not_treated_as_warm(tmp_path, monkeypatch):
    f = tmp_path / "cache.json"
    f.write_text(json.dumps({"fetched_at": time.time() - 40 * 3600, "data": [MGR]}))
    _fresh_process(monkeypatch, f)
    assert truein.get_cached_employees_if_warm() is None
    assert truein.get_managers_from_cache_only()        # still usable for the manager picker


def test_missing_or_corrupt_file_is_harmless(tmp_path, monkeypatch):
    _fresh_process(monkeypatch, tmp_path / "nope.json")
    assert truein.get_managers_from_cache_only() == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    _fresh_process(monkeypatch, bad)
    assert truein.get_cached_employees_if_warm() is None


def test_a_live_pull_is_saved_for_the_next_restart(tmp_path, monkeypatch):
    f = tmp_path / "cache.json"
    _fresh_process(monkeypatch, f)

    class R:
        def raise_for_status(self): pass
        def json(self): return {"data": [MGR], "more_rows": "0"}
    monkeypatch.setattr(truein.requests, "get", lambda *a, **k: R())
    assert truein._fetch_all_employees_raw() == [MGR]
    saved = json.loads(f.read_text())
    assert saved["data"] == [MGR] and saved["fetched_at"] > 0
    # ...and a "restart" picks it straight back up with no network call
    _fresh_process(monkeypatch, f)
    monkeypatch.setattr(truein.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no live call expected")))
    assert truein._fetch_all_employees_raw() == [MGR]


def test_disabled_under_tests_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(truein, "_DISK_CACHE_PATH", str(tmp_path / "c.json"))
    truein._save_disk_cache([MGR], time.time())
    assert not (tmp_path / "c.json").exists()
