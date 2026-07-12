"""SQLAlchemy ORM models.

Importing this package registers every model on ``Base.metadata`` (used by Alembic
autogenerate and by the app). Add new model modules to the imports below.
"""

from app.models.assessment import Item, ItemKC, Rubric
from app.models.chat import Conversation, LLMCall, Message
from app.models.content import ContentBlock, ContentType
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.memory import Memory, MemoryKind
from app.models.profile import LearnerProfile, ProfileDimension
from app.models.source import Chunk, ChunkKC, Source, SourceKind, SourceStatus

__all__ = [
    "KC",
    "Chunk",
    "ChunkKC",
    "ContentBlock",
    "ContentType",
    "Conversation",
    "Item",
    "ItemKC",
    "KCEdge",
    "LLMCall",
    "Learner",
    "LearnerKCState",
    "LearnerProfile",
    "LearningEvent",
    "LessonPlan",
    "Memory",
    "MemoryKind",
    "Message",
    "ProfileDimension",
    "Rubric",
    "Source",
    "SourceKind",
    "SourceStatus",
    "Subject",
    "Topic",
]
