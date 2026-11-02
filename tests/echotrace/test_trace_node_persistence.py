from echotrace.domain import (
    NodeClaim,
    NodeDraft,
    NodeEvidence,
    NodeReview,
    NodeRevision,
    TraceTemplate,
    get_trace_template,
)
from echotrace.domain.models import Document, Event, InvestigationRun
from echotrace.infrastructure.persistence.mysql import SqlAlchemyUnitOfWork, create_database


def _run():
    document = Document(
        source="official",
        title="官方材料",
        content="监管部门发布了处置说明。",
        url="https://example.test/node-source",
    )
    return InvestigationRun(
        objective="研究公共事件",
        event=Event(title="公共事件", objective="研究公共事件"),
        investigation_type="public_event",
        subject="公共事件",
        documents=[document],
        scope={"template_id": "public_event.trace", "template_version": "trace-template.v1"},
    )


def _draft(revision: int, *, node_id: str, document_id: str) -> NodeDraft:
    claim = NodeClaim(
        claim_id=f"claim-node-{revision}",
        claim_type="action",
        text="监管部门发布了处置说明。",
        source_document_ids=[document_id],
        evidence_ids=[f"evidence-node-{revision}"],
    )
    evidence = NodeEvidence(
        evidence_id=f"evidence-node-{revision}",
        claim_id=claim.claim_id,
        document_id=document_id,
        excerpt="监管部门发布了处置说明。",
        locator={"quote": "监管部门发布了处置说明。"},
    )
    return NodeDraft(
        node_id=node_id,
        slot_id="response_treatment",
        revision=revision,
        claims=[claim],
        evidence=[evidence],
        conclusion="现有材料显示监管部门已经发布处置说明。",
        media=[],
    )


def test_node_repository_persists_template_revisions_reviews_and_freeze():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    try:
        run = _run()
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
            nodes = uow.trace_nodes.initialize_nodes(run_id=run.run_id, template=get_trace_template("public_event"))
            target = next(item for item in nodes if item["slot_id"] == "response_treatment")
            document_id = run.documents[0].document_id
            for revision in range(4):
                draft = _draft(revision, node_id=target["node_id"], document_id=document_id)
                review = None
                if revision:
                    review = NodeReview(
                        review_id=f"review-node-{revision}",
                        node_id=target["node_id"],
                        slot_id=target["slot_id"],
                        revision=revision,
                        status="finalize" if revision == 3 else "revise",
                        covered_aspects=["主体"],
                        missing_aspects=[] if revision == 3 else ["结果"],
                        redundant_claims=[],
                        unclear_claims=[],
                        evidence_followups=[] if revision == 3 else ["补充"],
                        rewrite_instructions=["编辑"],
                        next_research_tasks=[] if revision == 3 else ["检索"],
                        final_editor_note="定稿" if revision == 3 else "继续优化。",
                    )
                uow.trace_nodes.save_revision(NodeRevision(target["node_id"], target["slot_id"], revision, draft, review, revision - 1 if revision else None))
            frozen = uow.trace_nodes.freeze_node(target["node_id"])
            assert frozen["status"] == "frozen"

        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            assert len(uow.trace_nodes.list_nodes(run.run_id)) == 6
            restored = uow.trace_nodes.get_revision(target["node_id"], revision_no=3)
            assert restored is not None
            assert restored["draft"]["conclusion"] == "现有材料显示监管部门已经发布处置说明。"
            assert restored["review"]["status"] == "finalize"
    finally:
        database.close()


def test_research_lifecycle_is_visible_and_duplicate_revision_does_not_reset_node():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    try:
        run = _run()
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
            target = next(item for item in uow.trace_nodes.initialize_nodes(
                run_id=run.run_id, template=get_trace_template("public_event")
            ) if item["slot_id"] == "response_treatment")
            assert uow.trace_nodes.begin_research(target["node_id"], 0) is True
            assert uow.trace_nodes.begin_research(target["node_id"], 0) is False
            assert next(item for item in uow.trace_nodes.list_nodes(run.run_id) if item["node_id"] == target["node_id"])["status"] == "researching"
            document_id = run.documents[0].document_id
            draft = _draft(0, node_id=target["node_id"], document_id=document_id)
            uow.trace_nodes.save_revision(NodeRevision(target["node_id"], target["slot_id"], 0, draft))
            assert uow.trace_nodes.begin_research(target["node_id"], 0) is False
            saved = next(item for item in uow.trace_nodes.list_nodes(run.run_id) if item["node_id"] == target["node_id"])
            assert saved["status"] == "reviewing"
    finally:
        database.close()


def test_review_is_an_independent_recovery_checkpoint_and_freeze_is_idempotent():
    database = create_database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    try:
        run = _run()
        with SqlAlchemyUnitOfWork(database.session_factory) as uow:
            uow.runs.save(run)
            target = next(item for item in uow.trace_nodes.initialize_nodes(
                run_id=run.run_id, template=get_trace_template("public_event")
            ) if item["slot_id"] == "response_treatment")
            document_id = run.documents[0].document_id
            uow.trace_nodes.save_revision(NodeRevision(
                target["node_id"], target["slot_id"], 0,
                _draft(0, node_id=target["node_id"], document_id=document_id),
            ))
            review = NodeReview(
                review_id="recovery-review-1", node_id=target["node_id"], slot_id=target["slot_id"],
                revision=1, status="revise", covered_aspects=["主体"], missing_aspects=["结果"],
                redundant_claims=[], unclear_claims=[], evidence_followups=["补充结果"],
                rewrite_instructions=["补充处置结果"], next_research_tasks=["检索结果"],
                final_editor_note="需要继续补充。",
            )
            assert uow.trace_nodes.save_review(review) == review.review_id
            assert uow.trace_nodes.save_review(review) == review.review_id
            saved = uow.trace_nodes.get_review(target["node_id"], 1)
            assert saved is not None and saved["review"]["review_id"] == review.review_id
            with __import__("pytest").raises(ValueError, match="immutable"):
                uow.trace_nodes.save_review(NodeReview(
                    review_id="different-review", node_id=target["node_id"], slot_id=target["slot_id"],
                    revision=1, status="revise", covered_aspects=["其他"], missing_aspects=["结果"],
                    redundant_claims=[], unclear_claims=[], evidence_followups=["补充结果"],
                    rewrite_instructions=["补充处置结果"], next_research_tasks=["检索结果"],
                    final_editor_note="不同意见。",
                ))
            uow.trace_nodes.save_revision(NodeRevision(
                target["node_id"], target["slot_id"], 1,
                _draft(1, node_id=target["node_id"], document_id=document_id), review, 0,
            ))
            for revision in (2, 3):
                final = revision == 3
                next_review = NodeReview(
                    review_id=f"recovery-review-{revision}", node_id=target["node_id"], slot_id=target["slot_id"],
                    revision=revision, status="finalize" if final else "revise", covered_aspects=["主体"],
                    missing_aspects=[] if final else ["结果"], redundant_claims=[], unclear_claims=[],
                    evidence_followups=[] if final else ["补充结果"], rewrite_instructions=["编辑"],
                    next_research_tasks=[] if final else ["检索结果"], final_editor_note="定稿" if final else "继续。",
                )
                uow.trace_nodes.save_revision(NodeRevision(
                    target["node_id"], target["slot_id"], revision,
                    _draft(revision, node_id=target["node_id"], document_id=document_id), next_review, revision - 1,
                ))
            first = uow.trace_nodes.freeze_node(target["node_id"])
            second = uow.trace_nodes.freeze_node(target["node_id"])
            assert first["status"] == second["status"] == "frozen"
    finally:
        database.close()
