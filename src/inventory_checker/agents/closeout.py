"""Close-out workflow (PRD workflow steps 8 "Close" and 9 "Review"/export), as a MAF Workflow.

    open-items check + lock  -->  memo draft  -->  export package

If open items remain, the first executor yields a ``blocked`` result and the workflow ends
without locking the session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_framework import Executor, WorkflowBuilder, WorkflowContext, handler

from .. import export
from ..service import Conflict, ObservationService


@dataclass
class CloseoutRequest:
    session_id: str
    user: str
    explanations: dict[str, str] = field(default_factory=dict)


@dataclass
class _Locked:
    session_id: str


@dataclass
class _Memo:
    session_id: str
    markdown: str


@dataclass
class CloseoutResult:
    status: str  # "blocked" | "ready"
    session_id: str
    open_items: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    memo_markdown: str = ""
    schedule_csv: str = ""
    schedule_xlsx: bytes = b""
    evidence_package: bytes = b""


class OpenItemsGate(Executor):
    def __init__(self, service: ObservationService):
        super().__init__(id="open_items_gate")
        self.service = service

    @handler
    async def run(self, req: CloseoutRequest, ctx: WorkflowContext[_Locked, CloseoutResult]) -> None:
        try:
            self.service.close_session(req.user, req.session_id, req.explanations)
        except Conflict as exc:
            items = [i.model_dump() for i in self.service.open_items(req.session_id)]
            await ctx.yield_output(CloseoutResult("blocked", req.session_id, items, str(exc)))
            return
        await ctx.send_message(_Locked(req.session_id))


class MemoDrafter(Executor):
    def __init__(self, service: ObservationService, client: Any | None, knowledge: Any | None = None):
        super().__init__(id="memo_drafter")
        self.service, self.client, self.knowledge = service, client, knowledge

    @handler
    async def run(self, msg: _Locked, ctx: WorkflowContext[_Memo]) -> None:
        from .memo import draft_memo

        await ctx.send_message(_Memo(msg.session_id, await draft_memo(self.service, msg.session_id, self.client, self.knowledge)))


class ExportPackager(Executor):
    def __init__(self, service: ObservationService):
        super().__init__(id="export_packager")
        self.service = service

    @handler
    async def run(self, msg: _Memo, ctx: WorkflowContext[Any, CloseoutResult]) -> None:
        sid = msg.session_id
        await ctx.yield_output(CloseoutResult(
            "ready", sid, message="Session closed and locked; exports ready for the workpapers.",
            memo_markdown=msg.markdown, schedule_csv=export.schedule_csv(self.service, sid),
            schedule_xlsx=export.schedule_xlsx(self.service, sid),
            evidence_package=export.evidence_package(self.service, sid, msg.markdown),
        ))


def build_closeout_workflow(service: ObservationService, client: Any | None = None, knowledge: Any | None = None):
    gate, memo, packager = OpenItemsGate(service), MemoDrafter(service, client, knowledge), ExportPackager(service)
    return (WorkflowBuilder(name="closeout", start_executor=gate, output_from=[gate, packager])
            .add_edge(gate, memo).add_edge(memo, packager).build())


async def run_closeout(service: ObservationService, request: CloseoutRequest, client: Any | None = None,
                       knowledge: Any | None = None) -> CloseoutResult:
    result = await build_closeout_workflow(service, client, knowledge).run(request)
    outputs = result.get_outputs()
    if not outputs:
        raise RuntimeError("Close-out workflow produced no output")
    return outputs[-1]
