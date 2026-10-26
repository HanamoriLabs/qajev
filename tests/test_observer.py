"""Observer durability and boundaries use local synthetic reports and fake control-plane replies."""

import json

import pytest

from qajev.observer import Observer, ObserverError, read_credential


def report(home, reports, local="job-1", count=2):
    job = home / "jobs" / local
    job.mkdir(parents=True)
    run = reports / local
    run.mkdir(parents=True)
    data = {"suite": "Synthetic suite", "gate": "PASS", "started_at": 1_792_972_800, "seconds": 8,
            "run_dir": str(run), "cost": {"usd": 0},
            "scenarios": [{"name": f"test-{i}", "about": "Static marker", "outcome": "pass",
                           "checks": [{"check": "marker", "ok": True}]} for i in range(count)]}
    (job / "job.json").write_text(json.dumps({"id": local, "argv": ["run", "fixture.yaml"], "started_at": data["started_at"]}))
    (job / "result.json").write_text(json.dumps(data))
    for name in ["report.json", "report.html", "report.md"]:
        (run / name).write_text(json.dumps(data) if name == "report.json" else "Synthetic report")
    return job, run, data


class Server:
    def __init__(self):
        self.rows = {}
        self.files = {}
        self.uploads = 0
        self.disconnect = False

    def heartbeat(self):
        return {"runner_id": "m_fixture"}

    def observe(self, record):
        cloud_id = "qms_" + record["id"]
        self.rows[cloud_id] = record
        return {"synced": [{"local_id": record["id"], "id": cloud_id}],
                "missing": [{"run": cloud_id, "path": f["path"], "url": "not used"}
                            for f in record["files"] if self.files.get((cloud_id, f["path"])) != f["sha256"]]}

    def upload(self, run, name, content, sha):
        self.uploads += 1
        if self.disconnect:
            raise ObserverError("synthetic disconnect")
        self.files[(run, name)] = sha


def observer(tmp_path, home=None, roots=None):
    return Observer(home or tmp_path / "home", roots or [tmp_path / "reports"], tmp_path / "state", "fixture")


def test_selected_home_external_reports_and_every_scenario(tmp_path):
    chosen = tmp_path / "home"
    report(chosen, tmp_path / "reports", count=564)
    report(tmp_path / "other", tmp_path / "foreign", "foreign")
    with observer(tmp_path) as watch:
        summary = watch.inventory()
        assert summary["jobs"] == 1
        server = Server()
        assert watch.flush(server)["acknowledged"] == 1
        assert len(server.rows["qms_job-1"]["tests"]) == 564
        assert len(server.rows) == 1


def test_disconnect_restart_and_source_pruning_preserve_unsent_artifacts(tmp_path):
    job, run, _ = report(tmp_path / "home", tmp_path / "reports")
    server = Server()
    with observer(tmp_path) as watch:
        watch.inventory()
        server.disconnect = True
        with pytest.raises(ObserverError, match="disconnect"):
            watch.flush(server)
        assert watch.pending() == 1
    import shutil
    shutil.rmtree(job)
    shutil.rmtree(run)
    server.disconnect = False
    with observer(tmp_path) as restarted:
        assert restarted.pending() == 1
        assert restarted.flush(server)["acknowledged"] == 1
        assert restarted.pending() == 0
        uploads = server.uploads
        assert restarted.flush(server)["acknowledged"] == 0
        assert server.uploads == uploads


def test_upload_ack_is_confirmed_by_another_manifest_read(tmp_path):
    report(tmp_path / "home", tmp_path / "reports")
    server = Server()
    server.upload = lambda *_: None  # The reply was lost/false: the server has no durable bytes.
    with observer(tmp_path) as watch:
        watch.inventory()
        with pytest.raises(ObserverError, match="missing"):
            watch.flush(server)
        assert watch.pending() == 1


@pytest.mark.parametrize("escape", ["file", "ancestor", "outside"])
def test_evidence_never_reads_a_symlink_or_unapproved_report_root(tmp_path, escape):
    job, run, data = report(tmp_path / "home", tmp_path / "reports")
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.jpg").write_bytes(b"DO-NOT-UPLOAD")
    if escape == "outside":
        data["run_dir"] = str(private)
    else:
        shots = run / "shots"
        if escape == "ancestor":
            shots.symlink_to(private, target_is_directory=True)
        else:
            shots.mkdir()
            (shots / "secret.jpg").symlink_to(private / "secret.jpg")
        data["scenarios"][0]["shot"] = "shots/secret.jpg"
    (job / "result.json").write_text(json.dumps(data))
    with observer(tmp_path) as watch:
        summary = watch.inventory()
        assert summary["missing_evidence"] > 0
        server = Server()
        watch.flush(server)
        row = next(iter(server.rows.values()))
        assert row["report"]["missing_evidence"]
        assert all(b"DO-NOT-UPLOAD" not in blob for blob in watch.blobs())


def test_backfill_reports_without_jobs_and_missing_images_are_explicit(tmp_path):
    job, run, data = report(tmp_path / "home", tmp_path / "reports")
    import shutil
    shutil.rmtree(job)
    data["scenarios"][0]["shot"] = "shots/missing.jpg"
    (run / "report.json").write_text(json.dumps(data))
    with observer(tmp_path) as watch:
        inventory = watch.inventory()
        assert inventory["historical_reports"] == 1 and inventory["missing_evidence"] == 1
        server = Server()
        watch.flush(server)
        assert next(iter(server.rows.values()))["report"]["missing_evidence"] == ["shots/missing.jpg"]


def test_live_queued_running_then_final_state_is_not_acknowledged_early(tmp_path):
    job = tmp_path / "home/jobs/job-1"
    job.mkdir(parents=True)
    (job / "job.json").write_text(json.dumps({"id": "job-1", "started_at": 1_792_972_800, "argv": ["run", "fixture"]}))
    with observer(tmp_path) as watch:
        server = Server()
        watch.inventory()
        watch.flush(server)
        assert server.rows["qms_job-1"]["gate"] == "queued"
        (job / "events.jsonl").write_text(json.dumps({"event": "run", "suite": "Fixture"}) + "\n")
        watch.inventory()
        watch.flush(server)
        assert server.rows["qms_job-1"]["gate"] == "running"


def test_state_cannot_be_reused_for_a_different_home_or_machine(tmp_path):
    report(tmp_path / "home", tmp_path / "reports")
    with observer(tmp_path) as watch:
        watch.inventory()
        watch.flush(Server())
        wrong = Server()
        wrong.heartbeat = lambda: {"runner_id": "m_other"}
        with pytest.raises(ObserverError, match="machine"):
            watch.flush(wrong)
    with pytest.raises(ObserverError, match="configuration"):
        observer(tmp_path, home=tmp_path / "foreign-home")


def test_machine_credential_must_be_private_owned_regular_and_not_a_symlink(tmp_path):
    file = tmp_path / "machine.credential"
    value = "qjm_" + "obviously-fake-fixture" * 2
    file.write_text(value)
    file.chmod(0o600)
    assert read_credential(file) == value
    file.chmod(0o644)
    with pytest.raises(ObserverError, match="0600"):
        read_credential(file)
    link = tmp_path / "alias"
    link.symlink_to(file)
    with pytest.raises(ObserverError):
        read_credential(link)


def test_cli_inventory_uses_explicit_home_without_credential_or_browser(tmp_path, capsys):
    from qajev.cli import main

    report(tmp_path / "home", tmp_path / "reports")
    assert main(["observe", "--home", str(tmp_path / "home"), "--report-root", str(tmp_path / "reports"),
                 "--state-dir", str(tmp_path / "state"), "--project", "fixture", "--inventory"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"] == 1
