import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, NotFoundError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def make_service(path):
    repo = SQLiteRepository(path)
    return DomainService(repo, RuleEngine())


class ReportOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = make_service(Path(self.tmp.name) / "test.db")
        self.analyst = Actor("ana-1", "analyst")

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, title, event_id=None):
        data = {"title": title, "origin_time": "2026-03-01T00:00:00Z", "location": "R"}
        if event_id:
            data["id"] = event_id
        return self.service.create(self.analyst, "event", data)

    def _report(self, code, event_id, station="STA", magnitude=3.0, observed_at="2026-03-01T00:00:05Z"):
        return self.service.create(
            self.analyst,
            "report",
            {
                "id": "rep-" + code,
                "code": code,
                "observed_at": observed_at,
                "event_id": event_id,
                "station": station,
                "magnitude": magnitude,
            },
        )

    def test_reassign_recomputes_both_events(self):
        event_a = self._event("A")
        event_b = self._event("B")
        self._report("R1", event_a["id"], "S1", 3.0)
        self._report("R2", event_a["id"], "S2", 5.0)
        target = self._report("R3", event_a["id"], "S3", 4.0)

        updated, affected = self.service.reassign_report(
            self.analyst, target["id"], event_b["id"], expected_version=1
        )
        self.assertEqual(updated["data"]["event_id"], event_b["id"])
        by_id = {item["id"]: item for item in affected}
        self.assertEqual(by_id[event_a["id"]]["data"]["station_count"], 2)
        self.assertEqual(by_id[event_a["id"]]["data"]["magnitude"], 4.0)  # median(3, 5)
        self.assertEqual(by_id[event_b["id"]]["data"]["station_count"], 1)
        self.assertEqual(by_id[event_b["id"]]["data"]["magnitude"], 4.0)

        persisted_a = self.service.get(event_a["id"])
        self.assertEqual(persisted_a["data"]["station_count"], 2)
        self.assertEqual(persisted_a["version"], 2)

    def test_report_belongs_to_exactly_one_event(self):
        event_a = self._event("A")
        event_b = self._event("B")
        report = self._report("R1", event_a["id"])
        self.service.reassign_report(
            self.analyst, report["id"], event_b["id"], expected_version=1
        )
        owned_a = self.service.list("report")
        owners = {item["id"]: item["data"]["event_id"] for item in owned_a}
        self.assertEqual(owners[report["id"]], event_b["id"])
        self.assertEqual(
            self.service.repository.reassign_report.__name__, "reassign_report"
        )

    def test_published_event_falls_back_and_outgoing_version_frozen(self):
        event_a = self._event("A")
        event_b = self._event("B")
        self._report("R1", event_a["id"], "S1", 3.0)
        report = self._report("R2", event_a["id"], "S2", 5.0)

        reviewer = Actor("rev-1", "reviewer")
        self.service.transition(self.analyst, event_a["id"], "associate", {})
        self.service.transition(reviewer, event_a["id"], "review",
                                {"reviewer": "rev-1", "magnitude": 4.0})
        self.service.transition(reviewer, event_a["id"], "publish",
                                {"communication_id": "COMM-1"})

        publications_before = self.service.publications(event_a["id"])
        self.assertEqual(len(publications_before), 1)
        frozen = publications_before[0]["payload"]
        self.assertEqual(len(frozen["reports"]), 2)

        _, affected = self.service.reassign_report(
            self.analyst, report["id"], event_b["id"], expected_version=report["version"]
        )
        by_id = {item["id"]: item for item in affected}
        self.assertEqual(by_id[event_a["id"]]["status"], "pending_review")

        # Old outgoing content stays untouched; no new publication appeared.
        publications_after = self.service.publications(event_a["id"])
        self.assertEqual(publications_after, publications_before)
        self.assertEqual(len(publications_after[0]["payload"]["reports"]), 2)

        # Re-review from pending_review is possible.
        self.service.transition(reviewer, event_a["id"], "review",
                                {"reviewer": "rev-1", "magnitude": 3.0})
        self.assertEqual(self.service.get(event_a["id"])["status"], "reviewed")

    def test_concurrent_reassign_first_wins_loser_sees_owner(self):
        event_a = self._event("A")
        event_b = self._event("B")
        event_c = self._event("C")
        report = self._report("R1", event_a["id"])

        barrier = threading.Barrier(2)
        results = []

        def move(actor_id, target_event):
            service = make_service(Path(self.tmp.name) / "test.db")
            actor = Actor(actor_id, "analyst")
            # Both analysts based their decision on the same observed v1.
            seen_version = service.get(report["id"])["version"]
            barrier.wait()
            try:
                service.reassign_report(
                    actor, report["id"], target_event, expected_version=seen_version
                )
                results.append((actor_id, "ok", None))
            except ConflictError as exc:
                results.append((actor_id, "conflict", exc.details))

        t1 = threading.Thread(target=move, args=("ana-A", event_b["id"]))
        t2 = threading.Thread(target=move, args=("ana-B", event_c["id"]))
        t1.start(); t2.start()
        t1.join(); t2.join()

        statuses = {item[0]: item[1] for item in results}
        self.assertEqual(sorted(statuses.values()), ["conflict", "ok"])
        winner = next(item for item in results if item[1] == "ok")[0]
        loser_detail = next(item for item in results if item[1] == "conflict")[2]
        final = self.service.get(report["id"])
        expected_event = event_b["id"] if winner == "ana-A" else event_c["id"]
        self.assertEqual(final["data"]["event_id"], expected_event)
        self.assertEqual(loser_detail["code"], "R1")
        self.assertEqual(loser_detail["event_id"], expected_event)
        self.assertEqual(loser_detail["claimed_by"], winner)
        self.assertEqual(loser_detail["version"], 2)

    def test_stale_expected_version_rejected(self):
        event_a = self._event("A")
        event_b = self._event("B")
        report = self._report("R1", event_a["id"])
        self.service.reassign_report(
            self.analyst, report["id"], event_b["id"], expected_version=1
        )
        with self.assertRaises(ConflictError):
            self.service.reassign_report(
                self.analyst, report["id"], event_a["id"], expected_version=1
            )

    def test_reassign_requires_known_event(self):
        event_a = self._event("A")
        report = self._report("R1", event_a["id"])
        with self.assertRaises(Exception):
            self.service.reassign_report(
                self.analyst, report["id"], "missing-event", expected_version=1
            )

    def test_reassign_requires_version_fence(self):
        event_a = self._event("A")
        event_b = self._event("B")
        report = self._report("R1", event_a["id"])
        with self.assertRaises(ValidationError):
            self.service.reassign_report(self.analyst, report["id"], event_b["id"])

    def test_report_code_unique(self):
        event_a = self._event("A")
        self._report("DUP", event_a["id"])
        with self.assertRaises(ConflictError):
            self._report("DUP", event_a["id"])


class BatchImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = make_service(Path(self.tmp.name) / "test.db")
        self.analyst = Actor("ana-1", "analyst")
        self.event = self.service.create(
            self.analyst,
            "event",
            {"id": "ev-1", "title": "A", "origin_time": "2026-03-01T00:00:00Z", "location": "R"},
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _row(self, code, magnitude=3.0):
        return {
            "code": code,
            "observed_at": "2026-03-01T00:00:0%dZ" % (int(code[1]) % 10),
            "event_id": self.event["id"],
            "station": "STA-" + code,
            "magnitude": magnitude,
        }

    def test_partial_failure_then_retry_only_fills_missing(self):
        rows = [
            self._row("B1"),
            {"code": "B2"},  # invalid: missing required fields
            self._row("B3"),
            "not-an-object",
        ]
        result = self.service.import_reports(self.analyst, rows)
        self.assertEqual([item["code"] for item in result["created"]], ["B1", "B3"])
        self.assertEqual(len(result["failed"]), 2)
        self.assertEqual(result["recomputed_events"], [self.event["id"]])

        event = self.service.get(self.event["id"])
        self.assertEqual(event["data"]["station_count"], 2)

        retry = self.service.import_reports(self.analyst, rows)
        self.assertEqual(retry["created"], [])
        self.assertEqual([item["code"] for item in retry["skipped"]], ["B1", "B3"])
        self.assertEqual(len(retry["failed"]), 2)
        self.assertEqual(len(self.service.list("report")), 2)

    def test_empty_batch_ok(self):
        result = self.service.import_reports(self.analyst, [])
        self.assertEqual(result["total"], 0)


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "legacy.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_embedded_reports_migrated_to_records(self):
        # Build an old-schema database with reports embedded in the event.
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "CREATE TABLE entities (id TEXT PRIMARY KEY, kind TEXT, status TEXT, "
            "version INTEGER, data TEXT, created_by TEXT, created_at TEXT, updated_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT, "
            "actor_id TEXT, actor_role TEXT, action TEXT, from_status TEXT, to_status TEXT, "
            "detail TEXT, created_at TEXT)"
        )
        connection.execute(
            "CREATE TABLE idempotency (actor_id TEXT, idem_key TEXT, entity_id TEXT, "
            "created_at TEXT, PRIMARY KEY(actor_id, idem_key))"
        )
        import json
        legacy = {
            "title": "Legacy",
            "origin_time": "2026-03-01T00:00:00Z",
            "location": "R",
            "reports": [
                {"station": "S1", "time_offset": 2, "distance_km": 1.0, "magnitude": 3.0},
                {"station": "S2", "time_offset": -1, "distance_km": 1.5, "magnitude": 5.0},
            ],
        }
        connection.execute(
            "INSERT INTO entities VALUES ('ev-old', 'event', 'candidate', 1, ?, 'old', 't', 't')",
            (json.dumps(legacy),),
        )
        connection.commit()
        connection.close()

        service = make_service(self.db_path)
        reports = service.list("report")
        self.assertEqual(len(reports), 2)
        for report in reports:
            self.assertEqual(report["data"]["event_id"], "ev-old")
            self.assertIn("observed_at", report["data"])
            self.assertTrue(report["data"]["code"].startswith("MIG-ev-old-"))

        # Second startup must not duplicate migrated rows.
        service2 = make_service(self.db_path)
        self.assertEqual(len(service2.list("report")), 2)

        # Migrated reports drive association and recompute like new ones.
        analyst = Actor("ana", "analyst")
        event = service2.get("ev-old")
        self.assertEqual(event["data"]["station_count"], 2)
        self.assertEqual(event["data"]["magnitude"], 4.0)  # median(3, 5)
        service2.transition(analyst, "ev-old", "associate", {})
        event = service2.get("ev-old")
        self.assertEqual(event["status"], "associated")
        self.assertEqual(event["data"]["associated_count"], 2)


if __name__ == "__main__":
    unittest.main()
