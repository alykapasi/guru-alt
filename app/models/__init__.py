"""SQLAlchemy ORM models.

Importing this package registers every model on ``Base.metadata`` (used by Alembic
autogenerate and by the app). Add new model modules to the imports below.
"""

from app.models.assessment import Item, ItemKC, Rubric
from app.models.chat import Conversation, LLMCall, Message
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent

__all__ = [
    "KC",
    "Conversation",
    "Item",
    "ItemKC",
    "KCEdge",
    "LLMCall",
    "Learner",
    "LearnerKCState",
    "LearningEvent",
    "Message",
    "Rubric",
    "Subject",
    "Topic",
]
