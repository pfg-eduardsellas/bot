from enum import Enum
from typing import List, Optional, Dict, Any
from abc import ABC, abstractmethod

# Avoid circular imports if we need type hinting for 'Page' later
# from playwright.async_api import Page

class ActionRetryError(Exception):
    """Exception raised when an action fails but should be retried."""
    pass

class ActionType(Enum):
    """The kinds of action the bot can perform on a page."""

    URL = "URL"
    BUTTON = "BUTTON"
    FORM = "FORM"
    LINK = "LINK"

class Action(ABC):
    """Base class for every action the bot executes and stores in the graph."""

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
        self.name: str = ""
        self.predecessors: List[int] = []
        self.successors: List[int] = []
        self.errors: List[str] = []
        self.accessibility_violations: List[Dict[str, Any]] = []
        self.retry_count: int = 0
        self.log_fn = print

    def add_predecessor(self, action_id: int):
        """Link another action as a parent of this one in the graph."""
        if action_id not in self.predecessors:
            self.predecessors.append(action_id)

    def add_successor(self, action_id: int):
        """Link another action as a child of this one in the graph."""
        if action_id not in self.successors:
            self.successors.append(action_id)

    def get_locator(self, page: Any):
        """Build the Playwright locator for this action's element."""
        return page.locator(self.selector)

    @abstractmethod
    async def execute(self, page: Any):
        """Perform this action on the page."""
        pass

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the action into a plain dict for storage."""
        return {
            "id": self.id,
            "custom_id": self.custom_id,
            "type": self.type.value,
            "selector": self.selector,
            "value": self.value,
            "name": self.name,
            "predecessors": self.predecessors,
            "successors": self.successors,
            "errors": self.errors,
            "accessibility_violations": self.accessibility_violations,
        }
