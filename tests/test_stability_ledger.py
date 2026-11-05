"""StabilityLedger: deployments, pushes and incidents, newest wins, kept across reopening; stability."""

import os
import shutil
import tempfile
import unittest

from lazarus_backend.stability_ledger import Deployment, Incident, Push, StabilityLedger

DAY = 24 * 3600


class StabilityLedgerTest(unittest.TestCase):
    def test_latest_deployment_is_the_running_commit(self):
        ledger = StabilityLedger(clock=lambda: 500.0)
        self.addCleanup(ledger.close)
        self.assertIsNone(ledger.current_deployment())

        ledger.record_deployment("aaa", "live", 100.0)
        ledger.record_deployment("bbb", "live")

        self.assertEqual(Deployment("bbb", "live", 500.0), ledger.current_deployment())
        self.assertEqual(["bbb", "aaa"], [d.sha for d in ledger.deployments()])

    def test_last_push(self):
        ledger = StabilityLedger(clock=lambda: 700.0)
        self.addCleanup(ledger.close)
        self.assertIsNone(ledger.last_push())
        ledger.record_push("aaa", 600.0)
        ledger.record_push("bbb")
        self.assertEqual(Push("bbb", 700.0), ledger.last_push())

    def test_survives_reopening(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        path = os.path.join(folder, "data", "ledger.db")
        ledger = StabilityLedger(path)
        ledger.record_deployment("aaa", "live", 100.0)
        ledger.record_push("aaa", 101.0)
        ledger.record_incident("aaa", 102.0)
        ledger.close()

        reopened = StabilityLedger(path)
        self.addCleanup(reopened.close)
        self.assertEqual(Deployment("aaa", "live", 100.0), reopened.current_deployment())
        self.assertEqual(Push("aaa", 101.0), reopened.last_push())
        self.assertEqual([Incident("aaa", 102.0)], reopened.incidents())


class StabilityTest(unittest.TestCase):
    """A commit is stable once it ran 24 h without an incident; `latest_stable(other_than)`."""

    def setUp(self):
        self.now = 10 * DAY
        self.ledger = StabilityLedger(clock=lambda: self.now)
        self.addCleanup(self.ledger.close)

    def test_a_commit_becomes_stable_after_24_h_without_an_incident(self):
        self.ledger.record_deployment("aaa", "live", self.now)
        self.now += DAY - 1
        self.assertIsNone(self.ledger.latest_stable())
        self.now += 1
        self.assertEqual("aaa", self.ledger.latest_stable().sha)

    def test_an_incident_within_24_h_disqualifies_the_commit(self):
        self.ledger.record_deployment("aaa", "live", self.now)
        self.ledger.record_incident("aaa", self.now + DAY - 60)
        self.now += 30 * DAY
        self.assertIsNone(self.ledger.latest_stable())

    def test_an_incident_after_24_h_doesnt_take_the_stability_back(self):
        self.ledger.record_deployment("aaa", "live", self.now)
        self.ledger.record_incident("aaa", self.now + DAY + 60)
        self.now += 2 * DAY
        self.assertEqual("aaa", self.ledger.latest_stable().sha)

    def test_a_commit_replaced_before_24_h_isnt_stable(self):
        self.ledger.record_deployment("aaa", "live", self.now)
        self.ledger.record_deployment("bbb", "live", self.now + DAY - 1)
        self.now += 5 * DAY
        self.assertEqual("bbb", self.ledger.latest_stable().sha)
        self.assertIsNone(self.ledger.latest_stable(other_than="bbb"))

    def test_latest_stable_other_than_skips_that_commit_and_unstable_ones(self):
        start = self.now
        self.ledger.record_deployment("old", "live", start)                 # ran 2 days: stable
        self.ledger.record_deployment("good", "live", start + 2 * DAY)      # ran 3 days: stable
        self.ledger.record_deployment("crashy", "live", start + 5 * DAY)    # incident after an hour
        self.ledger.record_incident("crashy", start + 5 * DAY + 3600)
        self.ledger.record_deployment("short", "live", start + 7 * DAY)     # replaced after an hour
        self.ledger.record_deployment("running", "live", start + 7 * DAY + 3600)
        self.now = start + 7 * DAY + 7200                                     # running for an hour

        self.assertEqual(Deployment("good", "live", start + 2 * DAY), self.ledger.latest_stable(other_than="running"))
        self.assertEqual("old", self.ledger.latest_stable(other_than="good").sha)
        self.now += DAY
        self.assertEqual("running", self.ledger.latest_stable().sha, "the running commit too, once it ran 24 h")
        self.assertEqual("good", self.ledger.latest_stable(other_than="running").sha)

    def test_now_and_the_stable_time_can_be_given(self):
        self.ledger.record_deployment("aaa", "live", self.now)
        self.assertIsNone(self.ledger.latest_stable(now=self.now + 3599, stable_seconds=3600))
        self.assertEqual("aaa", self.ledger.latest_stable(now=self.now + 3600, stable_seconds=3600).sha)

    def test_no_deployments_no_stable_commit(self):
        self.assertIsNone(self.ledger.latest_stable(other_than="aaa"))
        self.assertEqual([], self.ledger.incidents())


if __name__ == "__main__":
    unittest.main()
