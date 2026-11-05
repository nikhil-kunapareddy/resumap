"""The use cases: everything the app does, with no HTTP in sight.

Each service takes its collaborators in its constructor (the Store, the
ModelGateway, the Paths, and any service it builds on) and raises
``errors.ServiceError`` subclasses for requests it refuses. ``Services`` wires
one of each; the API layer is handed it and only translates HTTP to calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Paths
from ..llm import ModelGateway
from ..storage import Store
from .corrections import CorrectionService
from .exports import ExportService
from .jobs import JobService
from .lifecycle import LifecycleService
from .outreach import OutreachService
from .resumes import ExperienceService, ResumeService
from .rewrites import RewriteService
from .settings import SettingsService


@dataclass(frozen=True)
class Services:
    store: Store
    gateway: ModelGateway
    paths: Paths
    lifecycle: LifecycleService
    settings: SettingsService
    resumes: ResumeService
    experiences: ExperienceService
    jobs: JobService
    rewrites: RewriteService
    exports: ExportService
    corrections: CorrectionService
    outreach: OutreachService

    @classmethod
    def build(
        cls,
        store: Store,
        gateway: ModelGateway,
        paths: Paths,
        *,
        default_backend: str,
        default_first_name: str,
    ) -> Services:
        lifecycle = LifecycleService()
        settings = SettingsService(
            store,
            gateway,
            lifecycle,
            default_backend=default_backend,
            default_first_name=default_first_name,
        )
        exports = ExportService(store, paths, settings)
        return cls(
            store=store,
            gateway=gateway,
            paths=paths,
            lifecycle=lifecycle,
            settings=settings,
            resumes=ResumeService(store, paths),
            experiences=ExperienceService(store),
            jobs=JobService(store, gateway),
            rewrites=RewriteService(store, gateway),
            exports=exports,
            corrections=CorrectionService(store, gateway, exports),
            outreach=OutreachService(store, gateway),
        )


__all__ = [
    "Services",
    "CorrectionService",
    "ExperienceService",
    "ExportService",
    "JobService",
    "LifecycleService",
    "OutreachService",
    "ResumeService",
    "RewriteService",
    "SettingsService",
]
