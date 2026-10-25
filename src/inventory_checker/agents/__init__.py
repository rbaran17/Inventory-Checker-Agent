"""Microsoft Agent Framework agents and workflows for inventory observation."""

from .client import build_chat_client, model_name
from .closeout import build_closeout_workflow, run_closeout
from .copilot import build_observer_copilot
from .evidence_reader import EvidenceExtract, read_evidence
from .memo import draft_memo

__all__ = [
    "EvidenceExtract",
    "build_chat_client",
    "build_closeout_workflow",
    "build_observer_copilot",
    "draft_memo",
    "model_name",
    "read_evidence",
    "run_closeout",
]
