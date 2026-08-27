from typing import Any
from botpy.models.action import Action, ActionType


class URLAction(Action):
    """Action that navigates the browser straight to a URL."""

    def __init__(self, id: int, url: str, depth: int = 0):
        super().__init__(id, ActionType.URL, value=url)
        self.depth = depth
        self.name = url

    def to_dict(self):
        """Serialize the action together with its crawl depth."""
        data = super().to_dict()
        data["depth"] = self.depth
        return data

    async def execute(self, page: Any):
        """Navigate the page to this action's URL."""
        self.log_fn(f"Executing URLAction: Navigating to {self.value}")

        await page.goto(self.value)
