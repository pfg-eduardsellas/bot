from datetime import datetime, timezone
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    Text,
    JSON,
    Boolean,
)
from sqlalchemy.orm import relationship
from database import Base


class Scan(Base):
    """Represents one complete bot crawl run."""

    __tablename__ = "scans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    target_url = Column(String, nullable=False)
    status = Column(String, default="pending")  # pending | running | done | error
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    form_data = Column(JSON, nullable=True)
    objective = Column(Text, nullable=True)
    in_domain = Column(Boolean, default=False, nullable=False)
    max_pages = Column(Integer, default=15, nullable=False)
    max_depth = Column(Integer, default=3, nullable=False)
    max_actions = Column(Integer, default=50, nullable=False)
    accessibility = Column(Boolean, default=True, nullable=False)

    owner = relationship("User", back_populates="scans")
    actions = relationship(
        "ActionRecord", back_populates="scan", cascade="all, delete-orphan"
    )
    logs = relationship("ScanLog", back_populates="scan", cascade="all, delete-orphan")
    test_paths = relationship(
        "TestPath", back_populates="scan", cascade="all, delete-orphan"
    )
    accessibility_violations = relationship(
        "AccessibilityViolation", back_populates="scan", cascade="all, delete-orphan"
    )


class ActionRecord(Base):
    """Represents a single action/node in the crawl graph."""

    __tablename__ = "actions"

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(Integer, ForeignKey("scans.id"), nullable=False)
    action_id = Column(Integer, nullable=False)
    custom_id = Column(String, default="")
    type = Column(String, nullable=False)  # URL | BUTTON | FORM | LINK
    selector = Column(String, default="")
    value = Column(Text, default="")
    name = Column(String, default="", nullable=True)
    depth = Column(Integer, nullable=True)
    predecessors = Column(JSON, default=list)
    successors = Column(JSON, default=list)
    errors = Column(JSON, default=list)

    scan = relationship("Scan", back_populates="actions")
    accessibility_violations = relationship(
        "AccessibilityViolation",
        back_populates="action_record",
        cascade="all, delete-orphan",
    )

    @classmethod
    def from_action(cls, data: dict, scan_id: int) -> "ActionRecord":
        """Build a row from a serialized Action (the output of Action.to_dict())."""
        return cls(
            scan_id=scan_id,
            action_id=data["id"],
            custom_id=data.get("custom_id", ""),
            type=data["type"],
            selector=data.get("selector", ""),
            value=data.get("value", ""),
            name=data.get("name", ""),
            depth=data.get("depth"),
            predecessors=data.get("predecessors", []),
            successors=data.get("successors", []),
            errors=data.get("errors", []),
        )

    def to_action(self, form_data: dict = None):
        """Rebuild the runtime Action object this record was persisted from."""
        from botpy.actions.url_action import URLAction
        from botpy.actions.button_action import ButtonAction
        from botpy.actions.link_action import LinkAction
        from botpy.actions.form_action import FormAction

        stored_name = self.name or ""
        if self.type == "URL":
            action = URLAction(id=self.action_id, url=self.value)
        elif self.type == "BUTTON":
            action = ButtonAction(
                id=self.action_id, selector=self.selector, custom_id=self.custom_id, name=stored_name
            )
        elif self.type == "LINK":
            action = LinkAction(
                id=self.action_id,
                selector=self.selector,
                href=self.value,
                custom_id=self.custom_id,
            )
        elif self.type == "FORM":
            action = FormAction(
                id=self.action_id,
                selector=self.selector,
                form_data=form_data,
                custom_id=self.custom_id,
                name=stored_name,
            )
        else:
            raise ValueError(f"Unknown action type: {self.type}")
        if stored_name:
            action.name = stored_name
        return action


class ScanLog(Base):
    """One log line emitted while a scan was running."""

    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(Integer, ForeignKey("scans.id"), nullable=False)
    message = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    scan = relationship("Scan", back_populates="logs")


class TestPath(Base):
    """A saved path through a scan graph, optionally scheduled to run on its own."""

    __tablename__ = "test_paths"

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(Integer, ForeignKey("scans.id"), nullable=False)
    name = Column(String, nullable=True, default="")
    path = Column(Text, nullable=False)
    enabled = Column(Boolean, default=False, nullable=False)
    days_of_week = Column(String, nullable=True)
    hours = Column(String, nullable=True)
    assertions = Column(JSON, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    scan = relationship("Scan", back_populates="test_paths")
    runs = relationship(
        "TestPathRun", back_populates="test_path", cascade="all, delete-orphan"
    )


class TestPathRun(Base):
    """One execution of a test path and its result."""

    __tablename__ = "test_path_runs"

    id = Column(Integer, primary_key=True, index=True)
    test_path_id = Column(Integer, ForeignKey("test_paths.id"), nullable=False)
    status = Column(String, default="pending")
    triggered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    result = Column(JSON, nullable=True)

    test_path = relationship("TestPath", back_populates="runs")


class AccessibilityViolation(Base):
    """One accessibility violation reported by axe-core on an action."""

    __tablename__ = "accessibility_violations"

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(Integer, ForeignKey("scans.id"), nullable=False)
    action_record_id = Column(Integer, ForeignKey("actions.id"), nullable=True)
    action_id = Column(Integer, nullable=False)
    rule_id = Column(String, nullable=False)
    impact = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    help_url = Column(String, nullable=True)
    nodes = Column(JSON, default=list)

    scan = relationship("Scan", back_populates="accessibility_violations")
    action_record = relationship(
        "ActionRecord", back_populates="accessibility_violations"
    )

    @classmethod
    def from_violation(
        cls, data: dict, scan_id: int, action_record_id: int, action_id: int
    ) -> "AccessibilityViolation":
        """Build a row from an axe-core violation dict."""
        return cls(
            scan_id=scan_id,
            action_record_id=action_record_id,
            action_id=action_id,
            rule_id=data.get("rule_id", ""),
            impact=data.get("impact"),
            description=data.get("description"),
            help_url=data.get("help_url"),
            nodes=data.get("nodes", []),
        )


class User(Base):
    """Local user account for authentication."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    api_token = Column(String, unique=True, nullable=False)
    google_id = Column(String, unique=True, nullable=True, index=True)

    scans = relationship("Scan", back_populates="owner")
