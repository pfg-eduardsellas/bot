from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, JSON, Boolean
from sqlalchemy.orm import relationship
from database import Base


class Scan(Base):
    """Represents one complete bot crawl run."""
    __tablename__ = "scans"

    id = Column(Integer, primary_key=True, index=True)
    target_url = Column(String, nullable=False)
    status = Column(String, default="pending")  # pending | running | done | error
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    form_data = Column(JSON, nullable=True)
    in_domain = Column(Boolean, default=False, nullable=False)
    max_pages = Column(Integer, default=15, nullable=False)
    max_depth = Column(Integer, default=3, nullable=False)
    max_actions = Column(Integer, default=50, nullable=False)

    actions = relationship("ActionRecord", back_populates="scan", cascade="all, delete-orphan")


class ActionRecord(Base):
    """Represents a single action/node in the crawl graph."""
    __tablename__ = "actions"

    id = Column(Integer, primary_key=True, index=True)
    scan_id = Column(Integer, ForeignKey("scans.id"), nullable=False)

    # Fields from the original Action model
    action_id = Column(Integer, nullable=False)       # original id from the engine
    custom_id = Column(String, default="")
    type = Column(String, nullable=False)             # URL | BUTTON | FORM | LINK
    selector = Column(String, default="")
    value = Column(Text, default="")
    depth = Column(Integer, nullable=True)            # Only applies for URL Actions
    predecessors = Column(Text, default="[]")         # stored as JSON string
    successors = Column(Text, default="[]")           # stored as JSON string
    errors = Column(Text, default="[]")               # stored as JSON string

    scan = relationship("Scan", back_populates="actions")


class User(Base):
    """Local user account for authentication.""" 
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
