"""
Tests for `omnicli chat`: the chatbot front end. A tiny fake "brain" script stands in
for the real AI CLI so no quota is spent.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import runs  # noqa: E402
from omnicli.commands import chat  # noqa: E402

BRAIN_SRC = r'''
import re, sys
prompt = open(sys.argv[1], encoding="utf-8").read()
mode = sys.argv[2] if len(sys.argv) > 2 else "smart"
if mode == "capped":
    print("You've hit your 5-hour limit · resets 2:20am"); sys.exit(1)
msg = prompt.rsplit("\nUser: ", 1)[1].strip()
if "--img" in sys.argv:
    print("SAW-IMAGE " + sys.argv[sys.argv.index("--img") + 1].rsplit("/", 1)[-1])
    if "copy it" in msg:
        print('ACTION: {"type": "build", "goal": "build a page like [Image #1]", "dir": "img-proj"}')
elif "build" in msg:
    print("Sure - I'll have the team build that.")
    print('ACTION: {"type": "build", "goal": "create hello.txt saying hi", "dir": "hello-proj"}')
elif "progress" in msg:
    print("Here's the status:")
    print('ACTION: {"type": "command", "args": ["status", "-y"]}')
elif "wipe" in msg:
    print('ACTION: {"type": "command", "args": ["rm", "-rf", "/"]}')
elif "remember" in msg:
    print("FIRST-SEEN" if "User: my name is Kai" in prompt else "NO-MEMORY")
else:
    print("You said: " + msg)
'''


class FakeConductor:
    instances = []

    def __init__(self, cfg):
        self.cfg = cfg
        FakeConductor.instances.append(self)

    def run(self):
        return 0


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omnicli-chat-"))
        self.brain = self.tmp / "brain.py"
        self.brain.write_text(BRAIN_SRC)
        self.out_lines = []
        self.cli_calls = []
        FakeConductor.instances = []
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)

    def spec(self, name, mode="smart"):
        return {"id": name, "name": name, "bin": sys.executable, "strengths": "test",
                "readonly": [str(self.brain), "{prompt_file}", mode], "edit": [str(self.brain), "{prompt_file}", mode]}

    def session(self, inputs, specs=None, project=None, lead=None):
        it = iter(inputs)

        def fake_input(_prompt=""):
            self.out_lines.append(_prompt)  # questions are asked via the prompt; keep them visible to asserts
            try:
                return next(it)
            except StopIteration:
                raise EOFError

        specs = specs or {"brain": self.spec("brain")}
        health = {n: {"installed": True, "ok": True} for n in specs}

        def run_cli(argv):
            self.cli_calls.append(argv)
            return 0

        return chat.ChatSession(project=project, lead=lead, input_fn=fake_input, out=self.out_lines.append,
                                specs=specs, health=health, run_cli=run_cli, make_conductor=FakeConductor,
                                interactive=False)

    def text(self):
        return "\n".join(str(l) for l in self.out_lines)


class TestParsing(unittest.TestCase):
    def test_parse_reply(self):
        r, a = chat.parse_reply('Okay.\nACTION: {"type": "build", "goal": "x", "dir": ""}')
        self.assertEqual(r, "Okay.")
        self.assertEqual(a["goal"], "x")
        r, a = chat.parse_reply("Just an answer.")
        self.assertEqual((r, a), ("Just an answer.", None))
        r, a = chat.parse_reply('ACTION: {broken json')
        self.assertIsNone(a)
        r, a = chat.parse_reply('```\nACTION: {"type": "command", "args": ["status"]}\n```')
        self.assertEqual(a["args"], ["status"])
        _, a = chat.parse_reply('ACTION: {"type": "delete-everything"}')
        self.assertIsNone(a)

    def test_sanitize_command(self):
        proj = Path("/tmp/p")
        argv, _ = chat.sanitize_command(["status"], proj)
        self.assertEqual(argv, ["status", "-d", "/tmp/p"])
        argv, _ = chat.sanitize_command(["omnicli", "undo", "t2", "-y"], proj)
        self.assertEqual(argv, ["undo", "t2", "-d", "/tmp/p"])  # -y always stripped
        self.assertIsNone(chat.sanitize_command(["rm", "-rf", "/"], proj)[0])
        self.assertIsNone(chat.sanitize_command(["config", "set", "lead", "x"], proj)[0])
        self.assertEqual(chat.sanitize_command(["config", "get", "lead"], proj)[0], ["config", "get", "lead"])
        self.assertEqual(chat.sanitize_command(["runs"], proj)[0], ["runs"])  # no -d for runs

    def test_default_project_never_home(self):
        self.assertIsNone(chat.default_project(Path.home()))
        self.assertEqual(chat.default_project(Path("/tmp")), Path("/tmp").resolve())


class TestSession(ChatBase):
    def test_plain_chat_and_exit(self):
        s = self.session(["hello there", "/exit", "never reached"])
        self.assertEqual(s.loop(), 0)
        self.assertIn("You said: hello there", self.text())
        self.assertEqual(s.history[-1], ("assistant", "You said: hello there"))

    def test_memory_across_messages_and_clear(self):
        s = self.session(["my name is Kai", "do you remember?", "/clear", "do you remember?"])
        s.loop()
        lines = [l for l in self.out_lines if "FIRST-SEEN" in str(l) or "NO-MEMORY" in str(l)]
        self.assertIn("FIRST-SEEN", str(lines[0]))
        self.assertIn("NO-MEMORY", str(lines[1]))

    def test_build_action_confirms_then_runs_team_in_new_folder(self):
        s = self.session(["please build a hello project", "y"])
        s.loop()
        self.assertEqual(len(FakeConductor.instances), 1)
        cfg = FakeConductor.instances[0].cfg
        self.assertEqual(cfg.goal, "create hello.txt saying hi")
        self.assertEqual(cfg.project_dir, (self.tmp / "hello-proj").resolve())
        self.assertFalse(cfg.resume)
        self.assertEqual(s.project, (self.tmp / "hello-proj").resolve())

    def test_build_declined_does_nothing(self):
        s = self.session(["build it", "n"])
        s.loop()
        self.assertEqual(FakeConductor.instances, [])
        self.assertIn("not started", self.text())

    def test_unfinished_run_offers_continue(self):
        proj = self.tmp / "existing"
        st = proj / ".omnicli" / "team"
        st.mkdir(parents=True)
        (st / "state.json").write_text('{"goal": "old goal", "phase": "stopped", "pid": 0, "tasks": []}')
        s = self.session(["/build add a feature", "y", "y"], project=proj)
        s.loop()
        cfg = FakeConductor.instances[0].cfg
        self.assertTrue(cfg.resume)
        self.assertEqual(cfg.project_dir, proj.resolve())

    def test_new_plan_on_complete_project_keeps_old_state(self):
        proj = self.tmp / "done"
        st = proj / ".omnicli" / "team"
        st.mkdir(parents=True)
        (st / "state.json").write_text('{"goal": "v1", "phase": "complete", "pid": 0, "tasks": []}')
        s = self.session(["/build add dark mode", "y"], project=proj)
        s.loop()
        self.assertFalse(FakeConductor.instances[0].cfg.resume)
        self.assertEqual(FakeConductor.instances[0].cfg.goal, "add dark mode")
        self.assertEqual(len(list(st.glob("state.prev-*.json"))), 1)

    def test_ai_command_runs_with_project_and_without_yes(self):
        proj = self.tmp / "p1"
        proj.mkdir()
        s = self.session(["what's the progress?"], project=proj)
        s.loop()
        self.assertEqual(self.cli_calls, [["status", "-d", str(proj.resolve())]])

    def test_ai_cannot_run_arbitrary_commands(self):
        s = self.session(["wipe everything"])
        s.loop()
        self.assertEqual(self.cli_calls, [])
        self.assertIn("Skipped the suggested command", self.text())

    def test_slash_commands_are_local(self):
        proj = self.tmp / "p2"
        proj.mkdir()
        s = self.session(["/status", "/logs t2", "/runs", "/usage", "/bogus", "/help"], project=proj)
        s.loop()
        d = str(proj.resolve())
        self.assertEqual(self.cli_calls, [["status", "-d", d], ["logs", "t2", "-d", d], ["runs"], ["usage", "-d", d]])
        self.assertIn("Unknown command /bogus", self.text())
        self.assertIn("/build <goal>", self.text())

    def test_typo_suggests_and_runs_on_yes(self):
        proj = self.tmp / "typo"
        proj.mkdir()
        s = self.session(["/ussage", "y", "/stauts", "n", "/zzzz"], project=proj)
        s.loop()
        self.assertEqual(self.cli_calls, [["usage", "-d", str(proj.resolve())]])  # /stauts declined
        self.assertIn("Did you mean /usage?", self.text())
        self.assertIn("Did you mean /status?", self.text())
        self.assertIn("Unknown command /zzzz", self.text())  # nothing close: plain message

    def test_suggest(self):
        self.assertEqual(chat.suggest("ussage", {"usage", "status"}), "usage")
        self.assertEqual(chat.suggest("modle", chat.LOCAL_COMMANDS), "model")
        self.assertIsNone(chat.suggest("xyzzy", {"usage"}))

    def test_cli_typo_is_not_passed_to_the_shell(self):
        import contextlib, io
        from omnicli import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["ussage", "--by", "run"])
        self.assertEqual(rc, 2)
        self.assertIn("Did you mean: omnicli usage --by run", buf.getvalue())

    def _image(self, name="Screenshot 2026 at 11.19 PM.png"):
        p = self.tmp / name
        p.write_bytes(b"\x89PNG fake")
        return p

    def seeing_spec(self, name):
        s = self.spec(name)
        s.update({"image_args": ["--img", "{image}"], "sees_images": True})
        return s

    def test_dropped_image_becomes_placeholder_and_is_attached(self):
        img = self._image()
        dropped = str(img).replace(" ", "\\ ")
        s = self.session([f"what is in {dropped} ?"], specs={"brain": self.seeing_spec("brain")})
        s.loop()
        self.assertEqual(s.history[0][1], "what is in [Image #1] ?")
        stored = Path(s.attachments[1])
        self.assertTrue(stored.exists())
        self.assertNotIn(" ", stored.name)  # safe copy, no spaces
        self.assertIn("SAW-IMAGE " + stored.name, self.text())

    def test_image_routed_to_an_ai_that_can_see(self):
        img = self._image("a.png")
        specs = {"agy": self.spec("agy"), "codex": self.seeing_spec("codex")}  # agy first, can't see
        s = self.session([f"look {img}"], specs=specs, lead="agy")
        s.loop()
        self.assertIn("agy can't view images headlessly, so codex will answer", self.text())
        self.assertIn("codex", "\n".join(map(str, self.out_lines)))
        self.assertIn("SAW-IMAGE", self.text())
        self.assertEqual(s.lead, "agy")  # the lead itself doesn't change

    def test_images_numbered_across_messages_and_reach_the_team(self):
        a, b = self._image("one.png"), self._image("two.png")
        proj = self.tmp / "imgp"
        proj.mkdir()
        s = self.session([f"first {a}", f"/build a page like [Image #1] and {b}", "y"],
                         specs={"brain": self.seeing_spec("brain")}, project=proj)
        s.loop()
        self.assertEqual(sorted(s.attachments), [1, 2])
        cfg = FakeConductor.instances[0].cfg
        self.assertEqual(sorted(cfg.images), sorted(s.attachments.values()))
        self.assertIn("[Image #1]", cfg.goal)
        self.assertIn("Reference images from the user", cfg.goal)

    def test_ai_build_referencing_image_passes_it(self):
        img = self._image("ui.png")
        s = self.session([f"copy it {img}", "y"], specs={"brain": self.seeing_spec("brain")})
        s.loop()
        self.assertEqual(FakeConductor.instances[0].cfg.images, [s.attachments[1]])

    def test_non_image_paths_left_alone(self):
        s = self.session(["what is /usr/bin/env ?"])
        s.loop()
        self.assertEqual(s.attachments, {})
        self.assertEqual(s.history[0][1], "what is /usr/bin/env ?")

    def test_project_and_new(self):
        (self.tmp / "exists").mkdir()
        s = self.session(["/project exists", "/new brand-new", "/project nope"])
        s.loop()
        self.assertEqual(s.project, (self.tmp / "brand-new").resolve())
        self.assertIn("doesn't exist", self.text())

    def test_capped_ai_hands_over_to_next(self):
        specs = {"claude": self.spec("claude", "capped"), "agy": self.spec("agy")}
        s = self.session(["hi"], specs=specs)
        s.loop()
        self.assertIn("usage limit reached", self.text())
        self.assertIn("You said: hi", self.text())
        self.assertEqual(s.lead, "agy")
        self.assertIn("claude", s.down)

    def test_refuses_to_build_in_home(self):
        s = self.session([])
        s.project = None
        rc = s.start_build("x", str(Path.home()))
        self.assertEqual(rc, 1)
        self.assertIn("won't build directly", self.text())

    def test_shell_escape_runs_in_project(self):
        proj = self.tmp / "sh"
        proj.mkdir()
        s = self.session(["!touch made-by-shell.txt"], project=proj)
        s.loop()
        self.assertTrue((proj / "made-by-shell.txt").exists())

    def test_registered_and_plain_omnicli_opens_chat_only_in_terminal(self):
        from omnicli import cli
        self.assertIn("chat", cli._known_subcommands())
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main([])  # not a TTY here -> help, not chat
        self.assertEqual(rc, 0)
        self.assertIn("usage: omnicli", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
