"""Tasks: the civil intents gisc knows how to compile."""

from gisc.tasks import corridor, crossings

TASKS = {corridor.TASK: corridor, crossings.TASK: crossings}

__all__ = ["TASKS", "corridor", "crossings"]
