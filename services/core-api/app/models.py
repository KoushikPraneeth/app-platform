from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime
from app.database import Base


class Item(Base):
    """
    Social Post / Operational Thread Model.
    Represents community discussions, cluster status updates, and GitOps events.
    """
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), nullable=False, default="@anonymous")
    community = Column(String(100), nullable=False, default="r/general")
    content = Column(Text, nullable=False)
    upvotes = Column(Integer, nullable=False, default=0)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Backward compatibility with initial schema
    title = Column(String(255), nullable=True)
    author = Column(String(100), nullable=True)
