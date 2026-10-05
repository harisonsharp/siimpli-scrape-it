import pytest
from utils import db


def test_upsert_on_key_replaces_and_accumulates():
    run1 = db.start_run("j")
    db.store_results("j", run1, [{"date": "2025-01-01", "p": 1}, {"date": "2025-01-02", "p": 2}], key=["date"])
    run2 = db.start_run("j")
    db.store_results("j", run2, [{"date": "2025-01-02", "p": 20}, {"date": "2025-01-03", "p": 3}], key=["date"])

    rows = db.load_results("j")
    assert rows == [
        {"date": "2025-01-01", "p": 1},
        {"date": "2025-01-02", "p": 20},   # updated in place
        {"date": "2025-01-03", "p": 3},    # new
    ]


def test_without_key_every_run_appends():
    for _ in range(2):
        run = db.start_run("j")
        db.store_results("j", run, [{"a": 1}, {"a": 1}])
    assert len(db.load_results("j")) == 4


def test_results_are_scoped_per_job():
    db.store_results("a", db.start_run("a"), [{"x": 1}], key=["x"])
    db.store_results("b", db.start_run("b"), [{"x": 1}], key=["x"])
    assert db.load_results("a") == [{"x": 1}]
    assert len(db.load_results("b")) == 1


def test_missing_key_field_raises_and_stores_nothing():
    run = db.start_run("j")
    with pytest.raises(ValueError, match="date"):
        db.store_results("j", run, [{"date": "d1"}, {"other": 1}], key=["date"])
    assert db.load_results("j") == []


def test_run_history_roundtrip():
    r1 = db.start_run("j")
    db.finish_run(r1, "success", 1.5, 1, item_count=7)
    r2 = db.start_run("k")
    db.finish_run(r2, "failed", 0.2, 3, error="boom")

    runs = db.recent_runs()
    assert [r["id"] for r in runs] == [r2, r1]
    assert runs[1]["status"] == "success" and runs[1]["item_count"] == 7
    assert runs[0]["error"] == "boom" and runs[0]["attempts"] == 3
    assert [r["job_name"] for r in db.recent_runs("j")] == ["j"]
    assert len(db.recent_runs(limit=1)) == 1


# ---- abandoned-run cleanup ----------------------------------------------

def _dead_pid():
    """PID of a process that has definitely exited."""
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _set_pid(run_id, pid):
    with db._connect() as conn:
        conn.execute("UPDATE runs SET pid=? WHERE id=?", (pid, run_id))


def test_reap_marks_dead_process_runs_failed_and_spares_live_ones():
    dead = db.start_run("j")
    _set_pid(dead, _dead_pid())
    live = db.start_run("j")                       # recorded with this (live) process's pid
    legacy = db.start_run("j")
    _set_pid(legacy, None)                         # a row from before pids were tracked
    done = db.start_run("j")
    db.finish_run(done, "success", 1.0, 1, item_count=1)

    assert db.reap_stale_runs() == 1

    assert db.get_run(dead)["status"] == "failed"
    assert "Abandoned" in db.get_run(dead)["error"] and db.get_run(dead)["finished_at"]
    assert db.get_run(live)["status"] == "running"
    assert db.get_run(legacy)["status"] == "running"
    assert db.get_run(done)["status"] == "success"
    assert db.reap_stale_runs() == 0               # idempotent


def test_history_reads_reap_automatically():
    run = db.start_run("j")
    _set_pid(run, _dead_pid())
    assert db.recent_runs("j")[0]["status"] == "failed"

    run2 = db.start_run("k")
    _set_pid(run2, _dead_pid())
    assert db.get_run(run2)["status"] == "failed"


def test_old_database_without_pid_column_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_name TEXT NOT NULL,"
        " started_at TEXT NOT NULL, finished_at TEXT, duration_s REAL, status TEXT NOT NULL,"
        " attempts INTEGER NOT NULL DEFAULT 0, item_count INTEGER, error TEXT);"
        "INSERT INTO runs (job_name, started_at, status) VALUES ('old', '2025-01-01T00:00:00', 'success');"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("SIIMPLI_DB", str(path))

    run = db.start_run("new")
    assert db.get_run(run)["pid"] is not None
    assert [r["job_name"] for r in db.recent_runs()] == ["new", "old"]
