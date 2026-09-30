"""Fragment: adding voiceToll to an existing Pipecat bot. A complete, runnable bot: examples/test_agent/pipecat_agent.py.

Illustrative: build `pipeline` exactly as in your bot or a Pipecat example, then attach the observer.
    uv pip install pipecat-ai voicetoll
"""

from __future__ import annotations

import voicetoll
from pipecat.pipeline.worker import (  # Pipecat 1.x (0.0.x: pipeline.task.PipelineTask)
    PipelineParams,
    PipelineWorker,
)


def make_worker(pipeline, room_name: str, learner_id: str, lesson_kind: str) -> PipelineWorker:
    voicetoll.configure()  # reads VOICETOLL_* from the environment
    observer = voicetoll.pipecat.Observer(
        tenant="tutortalk",
        user=learner_id,  # pseudonymized with VOICETOLL_HMAC_KEY before it leaves the process
        session_id=room_name,
        tags={"feature": lesson_kind},
    )
    return PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[observer],
    )
