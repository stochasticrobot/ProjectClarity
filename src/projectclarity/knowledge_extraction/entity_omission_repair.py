"""Repair missing object entities without rewriting accepted source assertions.

Every accepted assertion with a null object is a review candidate, not a proven
omission. One bounded model review distinguishes literal attributes from missing
entity links. Source-name and structural checks constrain patches but do not prove
semantic entailment. Original records and per-assertion decisions remain available.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict
from projectclarity.knowledge_extraction.knowledge_extraction import Entity, validate_response
from projectclarity.model_access.model_ledger import mark_validation


class LinkDecision(BaseModel):
    """A single candidate's explicit link, literal-attribute or uncertain outcome."""
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str
    action: Literal['link', 'keep', 'unresolved']
    object_id: str | None
    reason: str


class LinkReview(BaseModel):
    """Allow only new entities and decisions, never replacement fact text."""
    model_config = ConfigDict(extra='forbid', strict=True)
    entities: list[Entity]
    decisions: list[LinkDecision]


def candidates(parsed):
    """Identify unlinked assertions without guessing their semantics from predicates.

    Args: parsed: Structurally accepted entities and facts for one source chunk.
    Returns: All null-object facts with stable accepted-list IDs. This includes
        legitimate literal attributes; no record is declared erroneous here.
    """
    return [{'id': f'A{i+1}', 'fact': deepcopy(f)} for i, f in enumerate(parsed['facts'])
            if f['object_id'] is None]


def named_in(name, text):
    """Require a complete explicitly written name, not an incidental substring.

    Args: name: Proposed entity name; text: Existing statement or source quotation.
    Returns: True for a case-sensitive occurrence bounded by non-word characters;
        no alias expansion, fuzzy match or external geographic lookup occurs.
    """
    return bool(name.strip() and re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text))


def repair_omissions(parsed, source, caller, sid, stage, stopped):
    """Review null-object facts once and apply only source-bound object references.

    Args:
        parsed: Accepted extraction; never mutated by this function.
        source: Exact saved chunk supporting the assertions.
        caller: Budgeted model transport with a model name and optional ledger hook.
        sid: Owning research session identifier.
        stage: Unique receipt label for this chunk's omission review.
        stopped: Predicate preventing a new review call after cancellation.
    Returns:
        Derived parsed records and an audit containing originals, candidates, model
        request/reply, accepted patches and unresolved decisions. No candidates
        means no call. Stop, malformed replies and provider failures retain original
        evidence; failures remain explicit and are never retried. At most one call
        is made, with no search, acquisition or direct filesystem writes.
    """
    output = deepcopy(parsed)
    pending = candidates(parsed)
    audit = {'status': 'not_needed', 'original_parsed': deepcopy(parsed),
             'candidates': pending, 'patches': [], 'decisions': [], 'unresolved': []}
    if not pending:
        return output, audit
    audit['unresolved'] = [p['id'] for p in pending]
    if stopped():
        audit['status'] = 'skipped_cancelled'
        return output, audit
    schema = LinkReview.model_json_schema()
    for definition in [schema, *schema.get('$defs', {}).values()]:
        definition.pop('description', None)
        definition.pop('title', None)
    request = {'model': caller.model, 'max_tokens': 8192,
               'system': (Path(__file__).resolve().parents[1] / 'prompts/repair_entity_omissions.txt').read_text(),
               'output_config': {'format': {'type': 'json_schema', 'schema': schema}},
               'messages': [{'role': 'user', 'content': json.dumps({
                   'document_chunk': source, 'entities': parsed['entities'],
                   'assertions': pending}, ensure_ascii=False)}]}
    audit.update(status='failed', request=request)
    try:
        raw = caller(stage, request, sid)
        audit['raw_text'] = raw
        review = LinkReview.model_validate_json(raw)
        ids = [d.id for d in review.decisions]
        if len(ids) != len(set(ids)) or set(ids) != {p['id'] for p in pending}:
            raise ValueError('Review must address every candidate exactly once.')
        existing = {e['id']: e for e in parsed['entities']}
        extra = {}
        for entity in review.entities:
            value = entity.model_dump()
            if entity.id in existing or entity.id in extra or not all(
                    v.strip() for v in (entity.id, entity.name, entity.type)):
                raise ValueError('Review redefined, duplicated or supplied a blank entity.')
            extra[entity.id] = value
        available = {**existing, **extra}
        audit['unresolved'] = []
        for decision in review.decisions:
            record = decision.model_dump()
            index = int(decision.id[1:]) - 1
            fact = output['facts'][index]
            try:
                if not decision.reason.strip():
                    raise ValueError('Decision needs a reason.')
                if decision.action != 'link':
                    if decision.object_id is not None:
                        raise ValueError('Non-link decision supplied an object.')
                    if decision.action == 'unresolved':
                        audit['unresolved'].append(decision.id)
                else:
                    # TODO: Resolve ambiguous locations using retained page context,
                    # not only the isolated assertion. A page title or section heading
                    # about Canadian companies, together with surrounding source text,
                    # may help identify the country for a city/province when the claim
                    # does not repeat it. Pass that context to the resolver with its
                    # provenance; distinguish explicit location evidence from contextual
                    # inference, and retain ambiguity when context conflicts or is weak.
                    # A Canadian-company listing alone does not establish that every
                    # office or event it mentions is in Canada. Preserve the original
                    # quote and record which contextual evidence supports any resolution.
                    entity = available.get(decision.object_id)
                    if not entity or not named_in(entity['name'], fact['quote']) or not named_in(entity['name'], fact['statement']):
                        raise ValueError('Object name is not explicit in both statement and quote.')
                    patched = {**fact, 'object_id': entity['id']}
                    validate_response(json.dumps({'entities': list(available.values()), 'facts': [patched]}), source)
                    output['facts'][index] = patched
                    if entity['id'] not in {e['id'] for e in output['entities']}:
                        output['entities'].append(entity)
                    audit['patches'].append({'id': decision.id, 'before': deepcopy(fact), 'after': deepcopy(patched)})
            except Exception as error:
                record['validation_error'] = str(error)[:500]
                audit['unresolved'].append(decision.id)
            audit['decisions'].append(record)
        audit['unused_entity_ids'] = sorted(set(extra) - {e['id'] for e in output['entities']})
        audit['status'] = 'partial' if audit['unresolved'] or audit['unused_entity_ids'] else 'checks_pass'
        mark_validation(caller, stage, sid, not audit['unresolved'] and not audit['unused_entity_ids'])
    except Exception as error:
        audit['error'] = str(error)[:1000]
        mark_validation(caller, stage, sid, False)
    return output, audit
