import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.analyst = Actor("analyst", "analyst")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_event(self, title, reports=None):
        data = {"title": title, "origin_time": "2026-01-01T00:00:00Z", "location": "Region-X"}
        if reports is not None:
            data["reports"] = reports
        return self.service.create(self.actor, "event", data)

    def _make_report(self, station, observed_at, event_id=None, amplitude=None):
        data = {"station": station, "observed_at": observed_at}
        if event_id is not None:
            data["event_id"] = event_id
        if amplitude is not None:
            data["amplitude"] = amplitude
        return self.service.create(self.actor, "report", data)

    def test_report_is_independent_record_with_code(self):
        report = self._make_report("STA-1", "2026-01-01T00:00:00Z")
        self.assertEqual(report["kind"], "report")
        self.assertTrue(report["data"]["code"].startswith("RP-"))
        self.assertEqual(report["data"]["station"], "STA-1")
        self.assertEqual(report["data"]["observed_at"], "2026-01-01T00:00:00Z")
        self.assertIsNone(report["data"].get("event_id"))

    def test_report_belongs_to_one_event(self):
        event = self._make_event("E-1")
        report = self._make_report("STA-1", "2026-01-01T00:00:00Z")
        updated = self.service.assign_report(self.actor, report["id"], event["id"])
        self.assertEqual(updated["data"]["event_id"], event["id"])

    def test_ownership_change_recomputes_both_events(self):
        event_a = self._make_event("A")
        event_b = self._make_event("B")
        r1 = self._make_report("S1", "2026-01-01T00:00:00Z", event_a["id"], amplitude=1.0)
        r2 = self._make_report("S2", "2026-01-01T00:01:00Z", event_a["id"], amplitude=3.0)
        r3 = self._make_report("S3", "2026-01-01T00:02:00Z", event_b["id"], amplitude=10.0)

        # A has 2 reports, magnitude median(1,3)=2; B has 1 report, magnitude=10
        a = self.service.get(event_a["id"])
        b = self.service.get(event_b["id"])
        self.assertEqual(a["data"]["station_count"], 2)
        self.assertEqual(a["data"]["magnitude"], 2.0)
        self.assertEqual(b["data"]["station_count"], 1)
        self.assertEqual(b["data"]["magnitude"], 10.0)

        # move r1 from A to B: A now has r2 only; B has r3, r1
        self.service.assign_report(self.actor, r1["id"], event_b["id"])
        a = self.service.get(event_a["id"])
        b = self.service.get(event_b["id"])
        self.assertEqual(a["data"]["station_count"], 1)
        self.assertEqual(a["data"]["magnitude"], 3.0)
        self.assertEqual(b["data"]["station_count"], 2)
        self.assertEqual(b["data"]["magnitude"], 5.5)  # median(10, 1)

    def test_published_event_reverts_to_review_on_ownership_change(self):
        event = self._make_event("PUB")
        self._make_report("S1", "2026-01-01T00:00:00Z", event["id"], amplitude=2.0)
        self._make_report("S2", "2026-01-01T00:01:00Z", event["id"], amplitude=4.0)
        self.service.transition(self.actor, event["id"], "associate", {})
        self.service.transition(self.actor, event["id"], "review", {"reviewer": "R1"})
        published = self.service.transition(
            self.actor, event["id"], "publish", {"communication_id": "C-1"}
        )
        self.assertEqual(published["status"], "published")
        snapshot = published["data"]["published_snapshot"]
        self.assertEqual(snapshot["communication_id"], "C-1")
        self.assertEqual(snapshot["magnitude"], 3.0)

        # ownership change after publish -> revert to reviewed (待复核)
        other = self._make_event("OTHER")
        report = self.service.list("report")[0]
        reverted = self.service.assign_report(self.actor, report["id"], other["id"])
        event_after = self.service.get(event["id"])
        self.assertEqual(event_after["status"], "reviewed")
        # published snapshot (外发内容) stays as the old version
        self.assertEqual(event_after["data"]["published_snapshot"], snapshot)

    def test_concurrent_assign_first_wins_later_sees_owner(self):
        event_a = self._make_event("A")
        event_b = self._make_event("B")
        report = self._make_report("S1", "2026-01-01T00:00:00Z")

        # analyst A claims it first
        first = self.service.assign_report(self.analyst, report["id"], event_a["id"])
        self.assertEqual(first["data"]["event_id"], event_a["id"])

        # analyst B claims the same report with a stale version -> conflict
        with self.assertRaises(ConflictError) as ctx:
            self.service.assign_report(
                self.analyst, report["id"], event_b["id"], expected_version=1
            )
        details = ctx.exception.details
        self.assertEqual(details["report_id"], report["id"])
        self.assertEqual(details["current_event_id"], event_a["id"])
        self.assertEqual(details["current_version"], 2)

    def test_batch_import_retry_only_fills_missing(self):
        items = [
            {"ref": "r1", "data": {"station": "S1", "observed_at": "2026-01-01T00:00:00Z"}},
            {"ref": "r2", "data": {"station": "S2", "observed_at": "2026-01-01T00:01:00Z"}},
            {"ref": "r3", "data": {"station": "S3"}},  # missing observed_at -> fails
        ]
        first = self.service.batch_import(self.actor, "report", items, batch_key="batch-1")
        statuses = {r["ref"]: r["status"] for r in first["results"]}
        self.assertEqual(statuses["r1"], "created")
        self.assertEqual(statuses["r2"], "created")
        self.assertEqual(statuses["r3"], "failed")

        # retry: r1, r2 skipped; r3 still fails (validation), no duplicates
        retry = self.service.batch_import(self.actor, "report", items, batch_key="batch-1")
        statuses = {r["ref"]: r["status"] for r in retry["results"]}
        self.assertEqual(statuses["r1"], "skipped")
        self.assertEqual(statuses["r2"], "skipped")
        self.assertEqual(statuses["r3"], "failed")
        # only 2 reports created in total
        self.assertEqual(len(self.service.list("report")), 2)

    def test_migration_moves_embedded_reports_to_new_records(self):
        # create an event with embedded reports (old format)
        event = self._make_event("OLD", reports=[
            {"station": "S1", "time_offset": 1, "distance_km": 0.5},
            {"station": "S2", "time_offset": 2, "distance_km": 1.0},
        ])
        # embedded reports were migrated on creation
        reports = self.service.list("report")
        self.assertEqual(len(reports), 2)
        for report in reports:
            self.assertEqual(report["data"]["event_id"], event["id"])
            self.assertTrue(report["data"]["code"].startswith("RP-"))
        # event no longer carries embedded reports, but has station_count
        event_after = self.service.get(event["id"])
        self.assertNotIn("reports", event_after["data"])
        self.assertEqual(event_after["data"]["station_count"], 2)

    def test_startup_migration_of_legacy_data(self):
        # seed a legacy event directly with embedded reports
        legacy = self.repo.create_entity(
            "legacy-1", "event", "candidate",
            {"title": "LEGACY", "origin_time": "2026-01-01T00:00:00Z",
             "location": "L", "reports": [{"station": "S1"}, {"station": "S2"}]},
            "admin",
        )
        # re-init service to trigger migration
        service2 = DomainService(self.repo, RuleEngine())
        reports = service2.list("report")
        self.assertEqual(len(reports), 2)
        event = service2.get("legacy-1")
        self.assertNotIn("reports", event["data"])
        self.assertEqual(event["data"]["station_count"], 2)


if __name__ == "__main__":
    unittest.main()
