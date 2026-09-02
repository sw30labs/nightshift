"""Nightshift — local overnight coding agent.

Pick a git repo, press Run, go to sleep. Two heterogeneous local LLMs
run a Ralph loop against a frozen three-item brief until remaining_count
hits zero or the clock halt. Morning: a night/* branch, a LoopScope
replay, and summary.md.

This is not a chatbot. The product is a branch you can `git diff`.

Author: Nicolas Cravino / sw30labs
"""

from .config import Settings

__version__ = "0.1.0"

__all__ = ["Settings", "__version__"]
