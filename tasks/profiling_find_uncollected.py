from datetime import timedelta

from shared.celery_config import profiling_finding_task_name
from sqlalchemy import func

from app import celery_app
from database.models.profiling import ProfilingCommit, ProfilingUpload
from helpers.clock import get_utc_now
from tasks.crontasks import CodecovCronTask
from tasks.profiling_collection import profiling_collection_task

MIN_INTERVAL_PROFILINGS_HOURS = 2


class FindUncollectedProfilingsTask(CodecovCronTask):

    name = profiling_finding_task_name

    @classmethod
    def get_min_seconds_interval_between_executions(cls):
        return 800

    async def run_cron_task(self, db_session, *args, **kwargs):
        min_interval_profilings = timedelta(hours=MIN_INTERVAL_PROFILINGS_HOURS)
        now = get_utc_now()
        query = (
            db_session.query(ProfilingCommit.id, func.count())
            .join(
                ProfilingUpload,
                ProfilingUpload.profiling_commit_id == ProfilingCommit.id,
            )
            .filter(
                (
                    ProfilingCommit.last_joined_uploads_at.is_(None)
                    & (ProfilingCommit.created_at <= now - min_interval_profilings)
                )
                | (
                    (
                        ProfilingCommit.last_joined_uploads_at
                        < ProfilingUpload.normalized_at
                    )
                    & (
                        ProfilingCommit.last_joined_uploads_at
                        <= now - min_interval_profilings
                    )
                )
            )
            .group_by(ProfilingCommit.id)
        )
        delayed_pids = []
        for pid, count in query:
            res = profiling_collection_task.delay(profiling_id=pid)
            delayed_pids.append((pid, count, res.as_tuple()))
        return {
            "delayed_profiling_ids": delayed_pids[:100],
            "delayed_profiling_ids_count": len(delayed_pids),
        }


RegisteredFindUncollectedProfilingsTask = celery_app.register_task(
    FindUncollectedProfilingsTask()
)
find_untotalized_profilings_task = celery_app.tasks[
    RegisteredFindUncollectedProfilingsTask.name
]
