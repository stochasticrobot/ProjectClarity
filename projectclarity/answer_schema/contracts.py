"""Define the validated response format for the model that designs the answer.

The answer-design step asks for report sections, fields that identify each entry,
and explicit evidence requirements. These Pydantic models check that the proposed
structure is internally consistent before it controls research and reporting.
For example, an identity field must name an actual field in its report section.
The models describe the requested answer, not findings or estimated result counts.
Class docstrings and field descriptions are included in the JSON schema sent to
the model; ordinary comments and this file introduction are developer documentation.
"""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError
from projectclarity.research_schemas import CompletionCriterion


class AnswerCompletionCheck(CompletionCriterion):
    """An answer requirement with an explicit counting unit and optional report-inventory binding."""

    unit: str = Field(min_length=1, max_length=200)
    result_goal_id: str | None
    evidence_required: list[str] = Field(min_length=1, max_length=6)


class ReportField(BaseModel):
    """One required answer field, with explicit meaning and representation rules."""

    model_config = ConfigDict(extra='forbid')
    name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    meaning: str = Field(min_length=1, max_length=1000)
    value_kind: Literal['person_name', 'organization_name', 'text', 'date', 'number']
    absence_meaning: str = Field(min_length=1, max_length=500)
    multiplicity: Literal['single', 'multiple'] = Field(default='single',
        description='single requires one resolved value; multiple collects compatible supported values. Identity fields must be single.')


class ReportSection(BaseModel):
    """A report section linked to one research goal, not an inferred finding."""

    model_config = ConfigDict(extra='forbid')
    goal_id: str = Field(pattern=r'^G[1-9][0-9]*$')
    title: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=2000)
    format: Literal['records', 'timeline', 'comparison', 'narrative']
    unit: str = Field(min_length=1, max_length=200)
    fields: list[ReportField] = Field(max_length=8)
    identity_fields: list[str] = Field(max_length=8)
    evidence_requirements: list[str] = Field(min_length=1, max_length=6)

    @model_validator(mode='after')
    def check_fields(self):
        """Reject ambiguous identities before they can control grouping.

        Args:
            self: Proposed section, without any extracted answers.
        Returns:
            This section; raises ValueError for duplicate/unknown identity fields,
            reserved provenance fields, multi-valued identities, or a tabular
            section without an identity.
            Narrative sections use evidence requirements rather than row fields.
        """
        from projectclarity.result_contract import PROVENANCE_FIELDS
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names) or any(n.casefold() in PROVENANCE_FIELDS for n in names):
            raise PydanticCustomError('report_fields_invalid', 'Report fields must be unique and separate from provenance')
        if len(set(self.identity_fields)) != len(self.identity_fields) or set(self.identity_fields) - set(names):
            raise PydanticCustomError('report_identity_invalid', 'Identity fields must be unique report field names')
        if any(f.name in self.identity_fields and f.multiplicity != 'single' for f in self.fields):
            raise PydanticCustomError('report_identity_multiplicity', 'Identity fields must be single-valued')
        if self.format == 'narrative':
            if names or self.identity_fields:
                raise PydanticCustomError('narrative_fields_invalid', 'Narrative sections use evidence requirements, not row fields')
        elif not names or not self.identity_fields:
            raise PydanticCustomError('report_identity_missing', 'Record sections require fields and explicit identity')
        return self


class AnswerSchema(BaseModel):
    """The requested report structure; contains no answers or population estimates."""

    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=2000)
    as_of: str | None
    sections: list[ReportSection] = Field(min_length=1, max_length=8)
    interpretation_notes: list[str] = Field(max_length=8)
    # Empty retains older saved contracts; new design calls require explicit checks.
    completion_checks: list[AnswerCompletionCheck] = Field(default_factory=list, max_length=12)

    @model_validator(mode='after')
    def unique_sections(self):
        """Keep section-to-goal binding unambiguous.

        Args:
            self: Proposed report contract.
        Returns:
            This contract; raises ValueError for duplicate section/check IDs,
            uncovered sections, unknown goals, nonnumeric checks with a count,
            or a report binding whose unit or scope differs from that section.
            Empty completion_checks retain the legacy saved-contract behavior.
        """
        if len({s.goal_id for s in self.sections}) != len(self.sections):
            raise PydanticCustomError('report_goals_duplicate', 'Report sections must have unique goal identifiers')
        if self.completion_checks:
            sections = {s.goal_id: s for s in self.sections}
            ids = [c.criterion_id for c in self.completion_checks]
            if len(ids) != len(set(ids)):
                raise PydanticCustomError('answer_checks_duplicate', 'Answer completion checks must have unique identifiers')
            covered = {gid for c in self.completion_checks for gid in c.goal_ids}
            if covered != set(sections):
                raise PydanticCustomError('answer_goals_mismatch', 'Completion checks must cover exactly the report goals')
            for check in self.completion_checks:
                if check.kind != 'enumeration' and check.target_basis != 'not_applicable':
                    raise PydanticCustomError('nonnumeric_target', 'Nonnumeric answer checks cannot impose a population target')
                if check.kind == 'enumeration' and check.target_basis == 'not_applicable':
                    raise PydanticCustomError('enumeration_target_missing', 'Enumeration needs a requested or researched target basis')
                if check.result_goal_id is not None:
                    section = sections.get(check.result_goal_id)
                    if (section is None or check.goal_ids != [check.result_goal_id]
                            or check.kind != 'enumeration' or not section.fields
                            or check.unit != section.unit):
                        raise PydanticCustomError('answer_inventory_mismatch', 'Completion check and report inventory have different units or scope')
        return self

