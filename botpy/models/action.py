from enum import Enum
from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod

# Avoid circular imports if we need type hinting for 'Page' later
# from playwright.async_api import Page

class ActionType(Enum):
    URL = "URL"
    BUTTON = "BUTTON"
    FORM = "FORM"
    LINK = "LINK"

class Action(ABC):
    def __init__(
        self,
        id: int,
        type: ActionType,
        selector: str = "",
        value: str = "",
        custom_id: str = ""
    ):
        self.id = id
        self.type = type
        self.selector = selector
        self.value = value
        self.custom_id = custom_id
        self.predecessors: List[int] = []
        self.successors: List[int] = []
        self.errors: List[str] = []

    def add_predecessor(self, action_id: int):
        if action_id not in self.predecessors:
            self.predecessors.append(action_id)

    def add_successor(self, action_id: int):
        if action_id not in self.successors:
            self.successors.append(action_id)

    @abstractmethod
    async def execute(self, page: Any):
        """
        Executes the action on the given Playwright page.
        :param page: playwright.async_api.Page
        """
        pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "custom_id": self.custom_id,
            "type": self.type.value,
            "selector": self.selector,
            "value": self.value,
            "predecessors": self.predecessors,
            "successors": self.successors,
            "errors": self.errors
        }
