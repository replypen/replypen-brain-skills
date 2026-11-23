"""made_by.py: the build check passes on an intact site and fails on each way to drop or restyle the line."""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "made_by.py"
SPEC = importlib.util.spec_from_file_location("made_by", SCRIPT)
assert SPEC and SPEC.loader
mb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mb)

PAGE = f"<html><head>{mb.LINK}</head><body><footer></footer>\n{mb.HTML}\n</body></html>\n"


class MadeByCheckTest(unittest.TestCase):
    def check(self, page=PAGE, css=mb.CSS, extra_css="", exempt=False):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "_site/assets").mkdir(parents=True)
            (root / "_site/index.html").write_text(page)
            (root / "_site/assets/made-by.css").write_text(css)
            (root / "_site/assets/style.css").write_text("body{margin:0}\n" + extra_css)
            if exempt:
                (root / ".made-by-exempt").write_text("test\n")
            (root / "check.sh").write_text("set -euo pipefail\n" + mb.CHECK)
            return subprocess.run(["bash", "check.sh"], cwd=root, capture_output=True, text=True)

    def test_intact_site_passes(self):
        r = self.check()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok op 1 pagina's", r.stdout)

    def test_each_way_to_drop_or_restyle_fails(self):
        cases = {
            "removed": PAGE.replace(mb.HTML, ""),
            "reworded": PAGE.replace("via <a", "met <a"),
            "no stylesheet": PAGE.replace(mb.LINK, ""),
            "inline override": PAGE.replace("</head>", "<style>.made-by{display:none}</style></head>"),
        }
        for name, page in cases.items():
            with self.subTest(name):
                r = self.check(page=page)
                self.assertEqual(r.returncode, 1, name)
                self.assertIn("index.html", r.stderr)
        r = self.check(css=mb.CSS.replace("#f3f4f6", "#fff"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("assets/made-by.css", r.stderr)
        r = self.check(extra_css=".made-by{display:none}\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("assets/style.css", r.stderr)

    def test_exempt_site_skips_the_check(self):
        self.assertEqual(self.check(page=PAGE.replace(mb.HTML, ""), exempt=True).returncode, 0)

    def test_apply_is_idempotent_and_keeps_the_include_style(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "_layouts").mkdir()
            (root / "_layouts/default.html").write_text("<head>\n</head>\n<body>\n  {% include footer.html %}\n</body>\n")
            (root / "build.sh").write_text("pnpm exec eleventy\n")
            (root / "AGENTS.md").write_text("# Site\n")
            self.assertEqual(len(mb.apply(root)), 5)
            self.assertEqual(mb.apply(root), [])
            layout = (root / "_layouts/default.html").read_text()
            self.assertIn("  {% include footer.html %}\n  {% include made-by.html %}\n", layout)

            # A changed source re-applied refreshes the build.sh reference too, keeping the commands around it.
            (root / "build.sh").write_text((root / "build.sh").read_text() + "pnpm exec pagefind --site _site\n")
            # A reworded rule refreshes the AGENTS.md section in place, keeping later sections.
            (root / "AGENTS.md").write_text((root / "AGENTS.md").read_text() + "\n## Later\n\nblijft.\n")
            self.assertEqual(mb.apply(root), [])
            with unittest.mock.patch.object(mb, "AGENTS", mb.AGENTS.replace(mb.TEXT, "Nieuwe tekst")):
                self.assertEqual(mb.apply(root), ["AGENTS.md"])
            agents = (root / "AGENTS.md").read_text()
            self.assertEqual(agents.count(mb.AGENTS_HEADING), 1)
            self.assertIn("Nieuwe tekst", agents)
            self.assertTrue(agents.endswith("\n## Later\n\nblijft.\n"))
            mb.apply(root)
            self.assertNotIn("Nieuwe tekst", (root / "AGENTS.md").read_text())

            new_css = mb.CSS.replace("#f3f4f6", "#eeeeee")
            new_check = mb.CHECK.replace(mb.CSS.rstrip(), new_css.rstrip())  # before CSS is patched
            with unittest.mock.patch.object(mb, "CSS", new_css), unittest.mock.patch.object(mb, "CHECK", new_check):
                self.assertEqual(sorted(mb.apply(root)), ["assets/made-by.css", "build.sh"])
                build = (root / "build.sh").read_text()
                self.assertEqual(build.count(mb.MARKER + " "), 1)
                self.assertIn("#eeeeee", build)
                self.assertTrue(build.startswith("pnpm exec eleventy\n"))
                self.assertTrue(build.endswith("pnpm exec pagefind --site _site\n"))


if __name__ == "__main__":
    unittest.main()
