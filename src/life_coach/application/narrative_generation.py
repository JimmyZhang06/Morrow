"""Governed life-line and memoir generation from user-approved memories."""
# ruff: noqa: RUF001

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from life_coach.ai.contracts import ModelInputKind, ModelInputRef
from life_coach.ai.provider import ModelGatewayError
from life_coach.application.model_gateway import ModelInvocationDenied, ModelTaskContextSnapshot
from life_coach.application.model_runtime import (
    ModelResultContext,
    ModelResultRejected,
    ModelRuntimeError,
)
from life_coach.modules.knowledge.enums import ClaimVersionOrigin, EvidenceRelation, VerdictType
from life_coach.modules.knowledge.models import (
    ClaimVersion,
    DerivedObject,
    EvidenceLink,
    MemoryClaim,
    UserVerdict,
)
from life_coach.modules.knowledge.reducer import reduce_verdicts
from life_coach.modules.model_runs.contracts import (
    ModelRunArtifactKind,
    ModelRunArtifactRef,
    ModelRunArtifactSpec,
)
from life_coach.modules.narrative.models import (
    NarrativeCitation,
    NarrativeGeneration,
    NarrativeGenerationKind,
    NarrativeProject,
    NarrativeTheme,
)
from life_coach.modules.narrative.service import NarrativeGenerationView, NarrativeService
from life_coach.platform.auth import (
    AuthenticationDenied,
    ProductionSessionFactory,
    VaultMembershipDenied,
)
from life_coach.platform.database import VaultAsyncSession
from life_coach.shared.database import utc_now

LIFE_LINE_TASK_TYPE = "life_line_synthesis"
MEMOIR_CHAPTER_TASK_TYPE = "memoir_chapter"
NARRATIVE_PROMPT_TEMPLATE_VERSION = "evidence-narrative-v1"
MAX_NARRATIVE_MATERIALS = 24

_Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]
_Medium = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class LifeLineThemeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: _Short
    interpretation: _Medium
    supporting_ordinals: list[int] = Field(min_length=1, max_length=12)
    counterexample_ordinals: list[int] = Field(default_factory=list, max_length=12)
    counterpoint: _Medium
    uncovered_period: _Medium

    @model_validator(mode="after")
    def unique_materials(self) -> LifeLineThemeOutput:
        if len(set(self.supporting_ordinals)) != len(self.supporting_ordinals):
            raise ValueError("supporting material ordinals must be unique")
        if len(set(self.counterexample_ordinals)) != len(self.counterexample_ordinals):
            raise ValueError("counterexample material ordinals must be unique")
        return self


class LifeLineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    overview: _Medium
    themes: list[LifeLineThemeOutput] = Field(min_length=1, max_length=3)


class MemoirChapterOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: _Short
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=80, max_length=5000)]
    uncertainty: _Medium
    citation_ordinals: list[int] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def reject_uncited_certainty_and_diagnosis(self) -> MemoirChapterOutput:
        if len(set(self.citation_ordinals)) != len(self.citation_ordinals):
            raise ValueError("memoir citation ordinals must be unique")
        folded = self.body.casefold()
        forbidden = ("诊断为", "人格障碍", "心理疾病", "you are diagnosed", "personality disorder")
        if any(value in folded for value in forbidden):
            raise ValueError("memoir output cannot contain diagnostic claims")
        return self


@dataclass(frozen=True, slots=True)
class NarrativeMaterial:
    ordinal: int
    memory_id: uuid.UUID
    derived_object_id: uuid.UUID
    statement: str
    kind: str
    valid_from: str
    uncertainty: str
    fragment_ids: tuple[uuid.UUID, ...]


class NarrativeRuntime(Protocol):
    async def run(
        self,
        *,
        authorization: str | None,
        vault_id: uuid.UUID,
        task_type: str,
        fragment_ids: tuple[uuid.UUID, ...],
        idempotency_key: str,
        context_id: uuid.UUID | None = None,
    ) -> ModelRunArtifactRef: ...


class NarrativeGenerationUnavailable(RuntimeError):
    pass


class NarrativeProjectUnavailable(RuntimeError):
    pass


class NarrativeMaterialUnavailable(RuntimeError):
    pass


class NarrativeContextAuthority:
    """Freeze an ordered set of current user-approved memories and evidence."""

    def materials(
        self, *, session: Session, vault_id: uuid.UUID, project_id: uuid.UUID
    ) -> tuple[NarrativeMaterial, ...]:
        project = session.scalar(
            select(NarrativeProject).where(
                NarrativeProject.vault_id == vault_id,
                NarrativeProject.id == project_id,
                NarrativeProject.state == "active",
            )
        )
        if project is None:
            raise ModelInvocationDenied("narrative project is unavailable")
        rows = session.execute(
            select(MemoryClaim, ClaimVersion)
            .join(
                ClaimVersion,
                (ClaimVersion.vault_id == MemoryClaim.vault_id)
                & (ClaimVersion.claim_id == MemoryClaim.id),
            )
            .join(
                DerivedObject,
                (DerivedObject.vault_id == ClaimVersion.vault_id)
                & (DerivedObject.id == ClaimVersion.derived_object_id),
            )
            .where(
                MemoryClaim.vault_id == vault_id,
                MemoryClaim.deleted_at.is_(None),
                ClaimVersion.system_to.is_(None),
                DerivedObject.deleted_at.is_(None),
            )
            .order_by(ClaimVersion.valid_from, ClaimVersion.claim_id)
        ).all()
        approved: list[tuple[MemoryClaim, ClaimVersion]] = []
        for claim, version in rows:
            if project.scope_from is not None and version.valid_from < project.scope_from:
                continue
            if project.scope_to is not None and version.valid_from >= project.scope_to:
                continue
            events = session.scalars(
                select(UserVerdict).where(
                    UserVerdict.vault_id == vault_id,
                    UserVerdict.target_derived_object_id == version.derived_object_id,
                )
            ).all()
            reduced = reduce_verdicts(version.initial_lifecycle_state, events, can_activate=False)
            corrected = (
                version.origin is ClaimVersionOrigin.USER_CORRECTION
                and version.origin_verdict_id is not None
            )
            if reduced.last_decisive_verdict is VerdictType.CONFIRM or corrected:
                approved.append((claim, version))
        approved = approved[-MAX_NARRATIVE_MATERIALS:]
        materials: list[NarrativeMaterial] = []
        for claim, version in approved:
            fragment_ids = tuple(
                dict.fromkeys(
                    session.scalars(
                        select(EvidenceLink.source_fragment_id)
                        .where(
                            EvidenceLink.vault_id == vault_id,
                            EvidenceLink.target_derived_object_id == version.derived_object_id,
                            EvidenceLink.relation.in_(
                                (EvidenceRelation.SUPPORTS, EvidenceRelation.CONTRADICTS)
                            ),
                            EvidenceLink.deleted_at.is_(None),
                            EvidenceLink.invalidated_reason.is_(None),
                        )
                        .order_by(EvidenceLink.created_at, EvidenceLink.id)
                    ).all()
                )
            )
            if not fragment_ids:
                continue
            materials.append(
                NarrativeMaterial(
                    ordinal=len(materials) + 1,
                    memory_id=claim.id,
                    derived_object_id=version.derived_object_id,
                    statement=version.canonical_text,
                    kind=claim.kind.value,
                    valid_from=version.valid_from.isoformat(),
                    uncertainty=version.uncertainty_text or "用户已确认；仍只代表用户当前理解。",
                    fragment_ids=fragment_ids,
                )
            )
        if not materials:
            raise ModelInvocationDenied("narrative materials are unavailable")
        return tuple(materials)

    def prepare(
        self, *, session: Session, vault_id: uuid.UUID, context_id: uuid.UUID
    ) -> ModelTaskContextSnapshot:
        materials = self.materials(session=session, vault_id=vault_id, project_id=context_id)
        fragment_ids = tuple(
            dict.fromkeys(fragment for material in materials for fragment in material.fragment_ids)
        )
        data = {
            "project": {"purpose": "life narrative candidate; not diagnosis or objective fact"},
            "materials": [
                {
                    "ordinal": material.ordinal,
                    "kind": material.kind,
                    "statement": material.statement,
                    "valid_from": material.valid_from,
                    "uncertainty": material.uncertainty,
                }
                for material in materials
            ],
        }
        canonical = json.dumps(
            {
                "project_id": str(context_id),
                "materials": [
                    {
                        "ordinal": item.ordinal,
                        "memory_id": str(item.memory_id),
                        "derived_object_id": str(item.derived_object_id),
                        "statement": item.statement,
                        "fragment_ids": [str(value) for value in item.fragment_ids],
                    }
                    for item in materials
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return ModelTaskContextSnapshot(
            context_id=context_id,
            input_ref=ModelInputRef(
                vault_id=str(vault_id),
                kind=ModelInputKind.DERIVED_OBJECT,
                # The receipt must point at a real governed object. The project
                # scopes the snapshot hash, while the first accepted memory is
                # the concrete DerivedObject anchor for the generic ModelRun API.
                object_id=str(materials[0].derived_object_id),
            ),
            content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            source_fragment_ids=fragment_ids,
            data=cast(JsonValue, data),
        )

    def assert_current(self, *, session: Session, snapshot: ModelTaskContextSnapshot) -> None:
        try:
            current = self.prepare(
                session=session,
                vault_id=uuid.UUID(snapshot.input_ref.vault_id),
                context_id=snapshot.context_id,
            )
        except (ValueError, ModelInvocationDenied):
            raise ModelInvocationDenied("narrative materials changed during generation") from None
        if current != snapshot:
            raise ModelInvocationDenied("narrative materials changed during generation")


class NarrativeResultPersister:
    def __init__(self, authority: NarrativeContextAuthority) -> None:
        self._authority = authority

    async def persist(
        self, session: VaultAsyncSession, *, context: ModelResultContext, result: BaseModel
    ) -> ModelRunArtifactSpec:
        snapshot = context.prepared.context_snapshot
        if snapshot is None:
            raise ModelResultRejected("narrative context is unavailable")
        materials = await session.run_sync(
            lambda sync_session: self._authority.materials(
                session=sync_session,
                vault_id=context.vault_id,
                project_id=snapshot.context_id,
            )
        )
        by_ordinal = {item.ordinal: item for item in materials}
        output: LifeLineOutput | MemoirChapterOutput
        if (
            context.prepared.task.task_type == LIFE_LINE_TASK_TYPE
            and type(result) is LifeLineOutput
        ):
            output = result
            generation_kind = NarrativeGenerationKind.LIFE_LINE
            title = "当前可能的人生主线"
            body = output.overview
            uncertainty = "这些是基于已确认材料形成的候选解释，可以并存、冲突或被完全拒绝。"
        elif (
            context.prepared.task.task_type == MEMOIR_CHAPTER_TASK_TYPE
            and type(result) is MemoirChapterOutput
        ):
            output = result
            generation_kind = NarrativeGenerationKind.MEMOIR_CHAPTER
            title = output.title
            body = output.body
            uncertainty = output.uncertainty
        else:
            raise ModelResultRejected("narrative result binding is unavailable")

        generation = NarrativeGeneration(
            vault_id=context.vault_id,
            project_id=snapshot.context_id,
            model_run_id=context.run_id,
            kind=generation_kind,
            title=title,
            body=body,
            uncertainty=uncertainty,
            state="proposed",
            created_at=utc_now(),
        )
        session.add(generation)
        await session.flush()
        if isinstance(output, LifeLineOutput):
            for position, theme_output in enumerate(output.themes, 1):
                ordinals = (
                    *theme_output.supporting_ordinals,
                    *theme_output.counterexample_ordinals,
                )
                if any(value not in by_ordinal for value in ordinals):
                    raise ModelResultRejected(
                        "narrative citation is outside authoritative materials"
                    )
                theme = NarrativeTheme(
                    vault_id=context.vault_id,
                    generation_id=generation.id,
                    position=position,
                    title=theme_output.title,
                    interpretation=theme_output.interpretation,
                    counterpoint=theme_output.counterpoint,
                    uncovered_period=theme_output.uncovered_period,
                )
                session.add(theme)
                await session.flush()
                for relation, values in (
                    ("supports", theme_output.supporting_ordinals),
                    ("counterexample", theme_output.counterexample_ordinals),
                ):
                    for ordinal in values:
                        material = by_ordinal[ordinal]
                        session.add(
                            NarrativeCitation(
                                vault_id=context.vault_id,
                                generation_id=generation.id,
                                theme_id=theme.id,
                                memory_claim_id=material.memory_id,
                                derived_object_id=material.derived_object_id,
                                material_ordinal=ordinal,
                                relation=relation,
                            )
                        )
        else:
            if any(value not in by_ordinal for value in output.citation_ordinals):
                raise ModelResultRejected("memoir citation is outside authoritative materials")
            for ordinal in output.citation_ordinals:
                material = by_ordinal[ordinal]
                session.add(
                    NarrativeCitation(
                        vault_id=context.vault_id,
                        generation_id=generation.id,
                        theme_id=None,
                        memory_claim_id=material.memory_id,
                        derived_object_id=material.derived_object_id,
                        material_ordinal=ordinal,
                        relation="supports",
                    )
                )
        await session.flush()
        return ModelRunArtifactSpec(
            vault_id=context.vault_id,
            artifact_kind=ModelRunArtifactKind.NARRATIVE,
            narrative_generation_id=generation.id,
        )


class GovernedNarrativeCreator:
    def __init__(
        self,
        *,
        sessions: ProductionSessionFactory,
        runtime: NarrativeRuntime,
        authorization: str | None,
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime
        self._authorization = authorization

    async def generate(
        self,
        *,
        vault_id: uuid.UUID,
        project_id: uuid.UUID,
        kind: Literal["life_line", "memoir_chapter"],
        idempotency_key: uuid.UUID,
    ) -> NarrativeGenerationView:
        task_type = LIFE_LINE_TASK_TYPE if kind == "life_line" else MEMOIR_CHAPTER_TASK_TYPE
        try:
            artifact = await self._runtime.run(
                authorization=self._authorization,
                vault_id=vault_id,
                task_type=task_type,
                fragment_ids=(),
                idempotency_key=str(idempotency_key),
                context_id=project_id,
            )
        except (AuthenticationDenied, VaultMembershipDenied):
            raise NarrativeProjectUnavailable("narrative project is unavailable") from None
        except ModelInvocationDenied:
            raise NarrativeMaterialUnavailable(
                "approved narrative material is unavailable"
            ) from None
        except (ModelGatewayError, ModelRuntimeError):
            raise NarrativeGenerationUnavailable("narrative generation is unavailable") from None
        if (
            artifact.artifact_kind is not ModelRunArtifactKind.NARRATIVE
            or artifact.narrative_generation_id is None
        ):
            raise NarrativeGenerationUnavailable("narrative artifact is unavailable")
        try:
            async with self._sessions.open(
                authorization=self._authorization,
                vault_id=vault_id,
            ) as authorized:
                return await NarrativeService(authorized.session).get_generation(
                    vault_id=vault_id,
                    generation_id=artifact.narrative_generation_id,
                )
        except (AuthenticationDenied, VaultMembershipDenied):
            raise NarrativeProjectUnavailable("narrative project is unavailable") from None


__all__ = [
    "LIFE_LINE_TASK_TYPE",
    "MEMOIR_CHAPTER_TASK_TYPE",
    "NARRATIVE_PROMPT_TEMPLATE_VERSION",
    "GovernedNarrativeCreator",
    "LifeLineOutput",
    "MemoirChapterOutput",
    "NarrativeContextAuthority",
    "NarrativeGenerationUnavailable",
    "NarrativeMaterialUnavailable",
    "NarrativeProjectUnavailable",
    "NarrativeResultPersister",
    "NarrativeRuntime",
]
