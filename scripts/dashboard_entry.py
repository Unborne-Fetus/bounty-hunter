"""PyInstaller entry point for the unified Windows workbench.

This intentionally saves a local diagnostic and displays a message box if
startup fails; a --windowed EXE has no console in which to show a traceback.
No account credentials or environment variable values are logged.
"""
import os
import sys
import traceback
from pathlib import Path


def launch():
    try:
        from bounty_hunter.desktop import main
        main()
    except Exception as error:
        # Use exception type and stack locations; avoid dumping settings,
        # secrets, environment variables, or request payloads.
        folder = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BountyHunter"
        log_path = folder / "startup-error.log"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            frames = traceback.extract_tb(error.__traceback__)
            lines = [
                "Bounty Hunter startup error",
                "Exception type: " + type(error).__name__,
                "Traceback locations:",
            ]
            for frame in frames[-15:]:
                lines.append(f"{frame.filename}:{frame.lineno} ({frame.name})")
            log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            destination = str(log_path)
        except OSError:
            destination = "Unable to write startup-error.log"
        message = (
            "Bounty Hunter could not start.\n\n"
            + type(error).__name__
            + "\n\nDiagnostic file: "
            + destination
            + "\n\nPlease share the diagnostic file when requesting a fix."
        )
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, message, "Bounty Hunter startup error", 0x10)
            except Exception:
                print(message, file=sys.stderr)
        else:
            print(message, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    launch()
