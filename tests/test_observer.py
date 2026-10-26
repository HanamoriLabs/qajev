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
    meta = {"id": local, "argv": ["run", "fixture.yaml"], "started_at": data["started_at"]}
    (job / "job.json").write_text(json.dumps(meta))
    (job / "result.json").write_text(json.dumps(data))
    for name in ["report.json", "report.html", "report.md"]:
        (run / name).write_text(json.dumps(data) if name == "report.json" else "Synthetic report")
    return job, run, data


class Server:
    def __init__(self):
        self.rows = {}
        self.files = {}
        self.contents = {}
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
        self.contents[(run, name)] = content


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


@pytest.mark.parametrize("escape", ["file", "ancestor", "inside", "outside"])
def test_evidence_never_reads_a_symlink_or_unapproved_report_root(tmp_path, escape):
    job, run, data = report(tmp_path / "home", tmp_path / "reports")
    private = run / ".private" if escape == "inside" else tmp_path / "private"
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
        assert all(b"DO-NOT-UPLOAD" not in blob for blob in server.contents.values())


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
        final = {"gate": "PASS", "started_at": 1_792_972_800, "seconds": 4, "scenarios": [
            {"name": "visible", "outcome": "pass", "checks": [{"check": "marker", "ok": True}]}]}
        (job / "result.json").write_text(json.dumps(final))
        watch.inventory()
        watch.flush(server)
        assert server.rows["qms_job-1"]["gate"] == "pass"
        final_record = json.dumps(server.rows["qms_job-1"], sort_keys=True)
        (job / "result.json").unlink()
        for old_events in ["", json.dumps({"event": "run", "suite": "Fixture"}) + "\n"]:
            (job / "events.jsonl").write_text(old_events)
            watch.inventory()
            assert watch.pending() == 0, "A stale queued/running snapshot replaced the final record"
            assert watch.flush(server)["acknowledged"] == 0
            assert json.dumps(server.rows["qms_job-1"], sort_keys=True) == final_record


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


def test_http_redirect_never_forwards_machine_credential(tmp_path):
    import http.server
    import threading

    from qajev.observer import Client

    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
            self.end_headers()

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_):
            pass

    credential = tmp_path / "machine.credential"
    credential.write_text("qjm_" + "fake-fixture-credential" * 2)
    credential.chmod(0o600)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = Client(f"http://127.0.0.1:{server.server_port}", credential)
        with pytest.raises(ObserverError, match="redirected"):
            client.heartbeat()
        assert requests == ["/api/v1/observer/heartbeat"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_observation_transport_sanitizes_metadata_before_outbound_request(tmp_path):
    import io

    from qajev.observer import Client

    credential = tmp_path / "machine.credential"
    credential.write_text("qjm_" + "fake-fixture-credential" * 2)
    credential.chmod(0o600)
    client = Client("https://cloud.example.test", credential)
    captured = []

    class Endpoint:
        def open(self, outgoing, **_kwargs):
            captured.append(outgoing.data)
            return io.BytesIO(b"{}")

    client._opener = Endpoint()
    client.observe({"report": {"source": "/Users/private-observer-fixture/reports/run.json"}})
    assert len(captured) == 1
    assert b"private-observer-fixture" not in captured[0]
    assert b"qjm_" not in captured[0]


def test_early_harness_without_output_directory_keeps_full_selected_job_result(tmp_path):
    job = tmp_path / "home/jobs/early"
    job.mkdir(parents=True)
    data = {"gate": "HARNESS", "started_at": 1_792_972_800, "error": "failure detail " * 1000, "scenarios": []}
    (job / "job.json").write_text(json.dumps({"id": "early", "started_at": data["started_at"]}))
    (job / "result.json").write_text(json.dumps(data))
    with observer(tmp_path) as watch:
        watch.inventory()
        server = Server()
        watch.flush(server)
        row = server.rows["qms_early"]
        assert row["report"]["reason"].startswith("failure detail")
        assert [file["path"] for file in row["files"]] == ["report.json"]
        assert json.loads(server.contents[("qms_early", "report.json")]) == data


def test_outbox_cap_refuses_next_blob_before_any_bytes_are_written(tmp_path):
    job, run, data = report(tmp_path / "home", tmp_path / "reports")
    (run / "shots").mkdir()
    (run / "shots/large.jpg").write_bytes(b"x" * (256 * 1024))
    data["scenarios"][0]["shot"] = "shots/large.jpg"
    (job / "result.json").write_text(json.dumps(data))
    with observer(tmp_path) as watch:
        watch.max_outbox_bytes = 128 * 1024
        before = sorted(p.name for p in watch.blob_dir.iterdir())
        with pytest.raises(ObserverError, match="outbox"):
            watch.inventory()
        assert sorted(p.name for p in watch.blob_dir.iterdir()) == before
        assert watch.pending() == 0


def test_inventory_reports_deduplicated_blob_bytes_and_cap_survives_restart(tmp_path):
    report(tmp_path / "home", tmp_path / "reports")
    with observer(tmp_path) as watch:
        first = watch.inventory()
        assert first["blob_bytes"] == sum(len(blob) for blob in watch.blobs()) > 0
        assert first["outbox_bytes"] >= first["blob_bytes"]
        assert first["max_outbox_bytes"] == 512 * 1024**2
        assert watch.inventory()["blob_bytes"] == first["blob_bytes"]
    with observer(tmp_path) as reopened:
        assert reopened.inventory()["blob_bytes"] == first["blob_bytes"]


def test_database_metadata_cannot_grow_beyond_total_outbox_cap(tmp_path):
    from qajev.observer import Observer

    cap = 256 * 1024
    job, _, data = report(tmp_path / "home", tmp_path / "reports")
    data["scenarios"][0]["checks"] = [{"check": "x" * 20_000, "ok": True}]
    data["source_detail"] = "p" * 100_000  # Full artifact bytes deduplicate; each database row still costs space.
    (job / "result.json").write_text(json.dumps(data))
    with Observer(tmp_path / "home", [tmp_path / "reports"], tmp_path / "state", "fixture",
                  max_outbox_bytes=cap) as watch:
        with pytest.raises(ObserverError, match="outbox"):
            for number in range(20):
                watch._enqueue(f"metadata-{number}", data, None, {})
        assert sum(p.stat().st_size for p in (tmp_path / "state").rglob("*") if p.is_file()) <= cap
        assert watch.pending() < 20


def test_cli_inventory_cap_is_explicit_and_prints_retained_byte_counts(tmp_path, capsys):
    from qajev.cli import main

    job, run, data = report(tmp_path / "home", tmp_path / "reports")
    (run / "shots").mkdir()
    (run / "shots/large.jpg").write_bytes(b"x" * (2 * 1024**2))
    data["scenarios"][0]["shot"] = "shots/large.jpg"
    (job / "result.json").write_text(json.dumps(data))
    code = main(["observe", "--home", str(tmp_path / "home"), "--report-root", str(tmp_path / "reports"),
                 "--state-dir", str(tmp_path / "state"), "--project", "fixture", "--inventory",
                 "--max-outbox-mib", "1"])
    captured = capsys.readouterr()
    assert code == 3
    counters = json.loads(captured.out)
    assert counters["error"] == "outbox_full" and counters["blob_bytes"] == 0
    assert counters["max_outbox_bytes"] == 1024**2 and counters["outbox_bytes"] <= 1024**2
    assert "--max-outbox-mib" in captured.err


def test_permanent_refusal_identifies_the_pending_local_run(tmp_path):
    report(tmp_path / "home", tmp_path / "reports", "a-refused")
    server = Server()

    def refuse(_record):
        raise ObserverError("HTTP 400; work stays pending")

    server.observe = refuse
    with observer(tmp_path) as watch:
        watch.inventory()
        with pytest.raises(ObserverError, match="a-refused"):
            watch.flush(server)
        assert watch.pending() == 1


def test_invalid_effect_metadata_becomes_missing_evidence_without_a_traceback(tmp_path):
    job, run, data = report(tmp_path / "home", tmp_path / "reports")
    data["scenarios"][0]["effect"] = "not an effect mapping"
    (job / "result.json").write_text(json.dumps(data))
    (run / "report.json").write_text(json.dumps(data))
    with observer(tmp_path) as watch:
        failure = None
        summary = None
        try:
            summary = watch.inventory()
        except ValueError as error:
            failure = error
        assert failure is None, "Malformed evidence stopped the whole observation cycle"
        assert summary is not None
        assert summary["missing_evidence"] == 1
        server = Server()
        watch.flush(server)
        assert server.rows["qms_job-1"]["report"]["missing_evidence"] == ["invalid image evidence metadata"]


def test_confirmed_upload_prunes_blobs_without_restaging_unchanged_inventory(tmp_path, monkeypatch):
    report(tmp_path / "home", tmp_path / "reports")
    server = Server()
    with observer(tmp_path) as watch:
        before = watch.inventory()
        assert before["blob_bytes"] > 0
        assert watch.flush(server)["acknowledged"] == 1
        assert list(watch.blob_dir.iterdir()) == [], "Confirmed evidence still occupies the outbox"

        def no_restage(_content):
            raise AssertionError("Unchanged confirmed evidence was copied back into the outbox")

        monkeypatch.setattr(watch, "_blob", no_restage)
        after = watch.inventory()
        assert after["blob_bytes"] == 0 and after["pending"] == 0
        assert after["outbox_bytes"] < before["outbox_bytes"]
        assert watch.flush(server)["acknowledged"] == 0
    with observer(tmp_path) as reopened:
        assert reopened.inventory()["blob_bytes"] == 0
        assert reopened.pending() == 0


def test_unconfirmed_manifest_keeps_every_blob_for_retry(tmp_path):
    report(tmp_path / "home", tmp_path / "reports")
    server = Server()
    server.upload = lambda *_: None
    with observer(tmp_path) as watch:
        before = watch.inventory()
        hashes = {path.name for path in watch.blob_dir.iterdir()}
        with pytest.raises(ObserverError, match="missing"):
            watch.flush(server)
        assert {path.name for path in watch.blob_dir.iterdir()} == hashes
        assert watch._blob_bytes == before["blob_bytes"] and watch.pending() == 1
        assert watch.flush(Server())["acknowledged"] == 1
        assert watch._blob_bytes == 0


def test_confirmed_pruning_preserves_blobs_shared_with_pending_runs(tmp_path):
    report(tmp_path / "home", tmp_path / "reports", "a-confirmed")
    job, run, _ = report(tmp_path / "home", tmp_path / "reports", "b-pending")
    # Both reports use the same HTML/Markdown content-addressed blob.
    server = Server()
    observe = server.observe

    def refuse_second(record):
        if record["id"] == "b-pending":
            raise ObserverError("synthetic refusal")
        return observe(record)

    server.observe = refuse_second
    with observer(tmp_path) as watch:
        watch.inventory()
        raw = watch.db.execute("SELECT record FROM outbox WHERE id='b-pending'").fetchone()[0]
        pending_hashes = {file["sha256"] for file in json.loads(raw)["files"]}
        with pytest.raises(ObserverError, match="b-pending"):
            watch.flush(server)
        assert watch.pending() == 1
        assert {path.name for path in watch.blob_dir.iterdir()} == pending_hashes
        assert watch._blob_bytes == sum(path.stat().st_size for path in watch.blob_dir.iterdir())
    import shutil
    shutil.rmtree(job)
    shutil.rmtree(run)
    server.observe = observe
    with observer(tmp_path) as reopened:
        assert reopened.flush(server)["acknowledged"] == 1
        assert reopened.pending() == 0 and reopened._blob_bytes == 0


def test_restart_reclaims_confirmed_blobs_after_interrupted_cleanup(tmp_path, monkeypatch):
    report(tmp_path / "home", tmp_path / "reports")
    with observer(tmp_path) as watch:
        watch.inventory()

        def interrupted_cleanup():
            raise OSError("synthetic cleanup interruption")

        monkeypatch.setattr(watch, "_prune_confirmed", interrupted_cleanup, raising=False)
        with pytest.raises(OSError, match="cleanup interruption"):
            watch.flush(Server())
        assert watch.pending() == 0
        assert watch._blob_bytes > 0
    with observer(tmp_path) as reopened:
        assert list(reopened.blob_dir.iterdir()) == []
        assert reopened.inventory()["blob_bytes"] == 0 and reopened.pending() == 0


def test_changed_confirmed_report_snapshots_new_evidence_before_upload(tmp_path):
    job, run, data = report(tmp_path / "home", tmp_path / "reports")
    (run / "shots").mkdir()
    image = run / "shots/fixture.jpg"
    image.write_bytes(b"first synthetic frame")
    data["scenarios"][0]["shot"] = "shots/fixture.jpg"
    (job / "result.json").write_text(json.dumps(data))
    server = Server()
    with observer(tmp_path) as watch:
        watch.inventory()
        watch.flush(server)
        assert watch._blob_bytes == 0
        image.write_bytes(b"second synthetic frame")
        watch.inventory()
        assert watch.pending() == 1 and watch._blob_bytes > 0
        server.disconnect = True
        with pytest.raises(ObserverError, match="disconnect"):
            watch.flush(server)
        assert watch.pending() == 1 and watch._blob_bytes > 0
        server.disconnect = False
        assert watch.flush(server)["acknowledged"] == 1
        assert server.contents[("qms_job-1", "shots/fixture.jpg")] == b"second synthetic frame"
        assert watch._blob_bytes == 0


def test_changing_evidence_during_inventory_is_not_checkpointed(tmp_path, monkeypatch):
    _, run, _ = report(tmp_path / "home", tmp_path / "reports")
    source = run / "report.html"
    read_bytes = type(source).read_bytes
    reads = []

    def changing_read(path):
        content = read_bytes(path)
        if path == source:
            reads.append(path)
            if len(reads) == 1:
                path.write_text("changed synthetic report")
        return content

    monkeypatch.setattr(type(source), "read_bytes", changing_read)
    with observer(tmp_path) as watch:
        with pytest.raises(ObserverError, match="evidence changed"):
            watch.inventory()
        assert watch.pending() == 0
