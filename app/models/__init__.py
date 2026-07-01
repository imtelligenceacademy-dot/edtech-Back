"""SQLAlchemy models. Importing this package registers every table on Base."""

from app.models.enums import (
    Language,
    LessonStatus,
    ReportScope,
    ReportStatus,
    Role,
    SecurityEvent,
    SecurityStatus,
    UserStatus,
    WatchdogStatus,
)
from app.models.access_request import AccessRequest
from app.models.ai_usage import AiUsage
from app.models.lesson import Lesson, LessonAssignment, Slide
from app.models.progress import Progress
from app.models.report import Report
from app.models.school import School
from app.models.security_log import SecurityLog
from app.models.token import RefreshToken
from app.models.uploaded_file import UploadedFile
from app.models.user import User

__all__ = [
    "Role",
    "Language",
    "UserStatus",
    "LessonStatus",
    "WatchdogStatus",
    "ReportStatus",
    "ReportScope",
    "SecurityEvent",
    "SecurityStatus",
    "User",
    "School",
    "Lesson",
    "Slide",
    "LessonAssignment",
    "AccessRequest",
    "Progress",
    "Report",
    "AiUsage",
    "SecurityLog",
    "RefreshToken",
    "UploadedFile",
]
