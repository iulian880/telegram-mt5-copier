# PyInstaller runtime hook for the portable build: tells tgmt5 to keep its data next to the .exe.
import os

os.environ["TGMT5_PORTABLE"] = "1"
