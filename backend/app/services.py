"""Module-level service singletons, wired by app.main at startup.

Accessed late (inside request handlers) so imports always see current values.
"""
from typing import Any

telegram_service: Any = None   # app.ingestion.telegram_listener.TelegramService
tracker_service: Any = None    # app.execution.tracker.TrackerService
notifier_service: Any = None   # app.reporting.notifier.NotifierService
youtube_manager: Any = None    # app.youtube.manager.YouTubeManager
