"""Windows desktop GUI smoke check: create a real Tk root and open every screen."""
import os
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from bounty_hunter.desktop import Workbench


@unittest.skipUnless(os.name == "nt", "Native Windows UI smoke test")
class DesktopGuiSmokeTests(unittest.TestCase):
    def test_all_main_pages_render(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(os.environ, {"APPDATA": folder, "LOCALAPPDATA": folder}):
                try:
                    app = Workbench()
                except tk.TclError as error:
                    self.skipTest(f"Runner has no interactive Windows desktop: {error}")
                try:
                    app.withdraw()
                    for page in ("bounties", "work", "overview", "payouts",
                                 "activity", "settings", "bounties"):
                        with self.subTest(page=page):
                            app.navigate(page)
                            app.update_idletasks()
                finally:
                    app.destroy()


if __name__ == "__main__":
    unittest.main()
