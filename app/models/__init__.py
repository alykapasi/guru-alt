"""SQLAlchemy ORM models.

Importing this package registers every model on ``Base.metadata`` (used by Alembic
autogenerate and by the app). Add new model modules to the imports below.
"""

from app.models.assessment import Item, ItemKC, ItemOrigin, Rubric
from app.models.auth import LearnerSession
from app.models.chat import Conversation, LLMCall, Message
from app.models.content import ContentBlock, ContentType
from app.models.knowledge import KC, KCEdge, Subject, Topic
from app.models.learner import Learner
from app.models.learning import LearnerKCState, LearningEvent
from app.models.lesson_plan import LessonPlan
from app.models.memory import Memory, MemoryKind
from app.models.note import Note, NoteRender, NoteRevision
from app.models.ops import AlertTransition
from app.models.profile import LearnerProfile, ProfileDimension
from app.models.publication import CurriculumProposal, Publication, PublicationStatus
from app.models.source import Chunk, ChunkKC, Source, SourceKind, SourceStatus

__all__ = [
    "KC",
    "AlertTransition",
    "Chunk",
    "ChunkKC",
    "ContentBlock",
    "ContentType",
    "Conversation",
    "CurriculumProposal",
    "Item",
    "ItemKC",
    "ItemOrigin",
    "KCEdge",
    "LLMCall",
    "Learner",
    "LearnerKCState",
    "LearnerProfile",
    "LearnerSession",
    "LearningEvent",
    "LessonPlan",
    "Memory",
    "MemoryKind",
    "Message",
    "Note",
    "NoteRender",
    "NoteRevision",
    "ProfileDimension",
    "Publication",
    "PublicationStatus",
    "Rubric",
    "Source",
    "SourceKind",
    "SourceStatus",
    "Subject",
    "Topic",
]
