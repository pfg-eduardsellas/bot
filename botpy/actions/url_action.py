from typing import Any
from botpy.models.action import Action, ActionType

class URLAction(Action):
    def __init__(self, id: int, url: str, depth: int = 0):
        super().__init__(id, ActionType.URL, value=url)
        self.depth = depth

    def to_dict(self):
        data = super().to_dict()
        data["depth"] = self.depth
        return data

    async def execute(self, page: Any):
        print(f"Executing URLAction: Navigating to {self.value}")
        await page.goto(self.value)
