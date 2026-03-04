import os
import sys
import logging
import tkinter as tk

# Add the parent directory to sys.path to resolve relative import issues when run directly.
# This makes 'sherlock' a known package so that 'from .gui.app' and others work correctly.
if __name__ == "__main__" and (__package__ is None or __package__ == ""):
    # Use the absolute path to ensure it's correct regardless of the current working directory.
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    __package__ = "sherlock"

from .gui.app import SherlockApp

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sherlock.log", encoding='utf-8')
        ]
    )

def main():
    setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("Starting Sherlock Media Organiser...")

    root = tk.Tk()
    # Set icon if available
    # root.iconbitmap('icon.ico')

    app = SherlockApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
