"""Tasks: the civil intents gisc knows how to compile."""

from gisc.tasks import corridor

TASKS = {corridor.TASK: corridor}

__all__ = ["TASKS", "corridor"]
