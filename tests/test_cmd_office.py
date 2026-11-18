"""Tests for the local animated office view; no browser or AI CLI is launched."""

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-office-test-")

from omnicli.commands import office  # noqa: E402


class OfficeTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="omni-office-project-")).resolve()
        team = self.project / ".omnicli" / "team"
        team.mkdir(parents=True)
        (team / "state.json").write_text(json.dumps({
            "version": 1,
            "goal": "Build a test project",
            "project_name": "Test Project",
            "phase": "running",
            "pid": os.getpid(),
            "lead": "claude",
            "workers": ["codex"],
            "tasks": [],
        }), encoding="utf-8")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.project, ignore_errors=True)

    def test_local_snapshot_has_agent_summaries(self):
        data = office.read_snapshot(self.project)
        self.assertEqual(data["project_name"], "Test Project")
        self.assertEqual([a["id"] for a in data["agents"]], ["claude", "codex"])
        self.assertFalse(any(k.startswith("_") for k in data))

    def test_page_is_self_contained_with_both_scenes(self):
        html = office.build_page()
        self.assertIn('fetch("/snapshot"', html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn('id: "office"', html)
        self.assertIn('id: "cricket"', html)
        self.assertIn("demoSnapshot", html)          # #demo previews every state without a run
        self.assertIn("OmniOffice.start()", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org/2000/svg", ""))
        self.assertEqual(html.lower().count("</script>"), html.lower().count("<script>"))

    def test_server_refuses_foreign_host_headers(self):
        import urllib.request
        server, url = office.start_background(self.project, port=0)
        try:
            with urllib.request.urlopen(url + "snapshot") as r:
                self.assertEqual(r.status, 200)
            port = server.server_address[1]
            req = urllib.request.Request("http://127.0.0.1:{}/snapshot".format(port),
                                         headers={"Host": "evil.example:{}".format(port)})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()

    def test_lead_shows_planning_and_final_check(self):
        state_file = self.project / ".omnicli" / "team" / "state.json"
        state = json.loads(state_file.read_text())
        state["phase"] = "planning"
        state_file.write_text(json.dumps(state))
        lead = [a for a in office.read_snapshot(self.project)["agents"] if a["id"] == "claude"][0]
        self.assertEqual(lead["status"], "planning")
        state.update(phase="running", tasks=[{"id": "t1", "title": "x", "assignee": "codex", "status": "done",
                                               "attempts": []}])
        state_file.write_text(json.dumps(state))
        lead = [a for a in office.read_snapshot(self.project)["agents"] if a["id"] == "claude"][0]
        self.assertEqual(lead["status"], "checking")

    def test_idle_snapshot_shows_team_at_rest(self):
        snap = office.idle_snapshot(self.project, "claude", ["codex", "claude", "agy"])
        self.assertEqual(snap["phase"], "idle")
        self.assertFalse(snap["alive"])
        self.assertEqual([(a["id"], a["role"], a["status"]) for a in snap["agents"]],
                         [("claude", "lead", "idle"), ("codex", "worker", "idle"), ("agy", "worker", "idle")])
        self.assertEqual(office.idle_snapshot(None, "claude", [])["project_name"], "No project yet")

    def test_office_setting_only_accepts_known_modes(self):
        from omnicli import settings
        self.assertEqual(settings.coerce("office", "OPEN"), "open")
        self.assertEqual(settings.get("office"), "on")
        with self.assertRaises(settings.SettingsError):
            settings.coerce("office", "sometimes")

    def test_missing_state_returns_none(self):
        self.assertIsNone(office.read_snapshot(self.project / "missing"))


if __name__ == "__main__":
    unittest.main()
