"""Run an optional short source study before the main research loop.

The study helps describe what evidence the question requires and, where appropriate,
estimate the likely scope. Up to three logical model tasks plan the sample, select
pages and assess the retrieved excerpts. The sample is limited to two queries and
four pages. With temporary-error retries there can be up to nine actual model
attempts, all charged to the existing run allowance.

The node reuses ordinary search and retrieval code and saves study progress so an
interruption is visible. Its findings are preliminary context, not reviewed answer
claims or a decision to stop research. Acquired pages can be reused in the later
research loop.
"""
import json
import time

from projectclarity.nodes.study_coverage.contracts import CoverageStudyPlan, CoverageStudyAssessment
from projectclarity.nodes.study_coverage.evidence import collect_probe_text, validate_estimates
from projectclarity.nodes.study_coverage.selection import select_probe_sources
from projectclarity.coverage.readiness import retain_readiness_profile, checked_coverage_outcome
from projectclarity.nodes.plan_research.research_planner import read_api_key
from projectclarity.nodes.retrieve_sources.node import retrieve_sources
from projectclarity.nodes.web_search.node import web_search
from projectclarity.prompt_context import research_context
from projectclarity.prompt_loader import load_prompt
from projectclarity.providers.structured_model_call_execution import invoke_structured
from projectclarity.retrieval.retrieval_limits import RunCancelled
from projectclarity.retrieval.runtime import retrieval_runtime
from projectclarity.run_monitor import MonitoringError
from projectclarity.session_storage import save_session, write_text

SYSTEM_PROMPT = load_prompt('study_coverage.txt')


def save_study(session, output_root, runtime=None):
    """Persist study state and any running acquisition clock without resetting budgets.

    Args:
        session: Mutable run with study state and cumulative model accounting.
        output_root: Directory holding the run's primary journal and study artifact.
        runtime: Optional invocation-local web client; never serialized.
    Returns:
        None. Save session.json and coverage_study.json, with actual study wire usage.
        Each write is atomic separately; session.json remains the restart authority.
    """
    if runtime and 'web' in runtime:
        session.retrieval_usage = {**runtime['web'].budget.summary(), 'checkpoint_wall_time': time.time()}
    session.coverage_study['model_requests'] = sum(a['task'] == 'study_coverage' for a in session.model_attempts)
    save_session(session, output_root)
    write_text(output_root / session.session_id / 'coverage_study.json',
        json.dumps(session.coverage_study, ensure_ascii=False, indent=2) + '\n')


def study_coverage(state, output_root, key_reader=read_api_key, monitor=None, runtime=None):
    """Decide, execute and record a short preliminary study before recursive research.

    Args:
        state: Typed graph state after the research plan and any human clarification.
        output_root: Run root for study, model, search and acquisition receipts.
        key_reader: Credential resolver passed to the shared bounded model executor.
        monitor: Optional cancellation/progress observer; no snippets enter telemetry.
        runtime: Invocation-local shared acquisition client and resource clock.
    Returns:
        Updated session. A skip, failed probe, invalid estimate or interrupted study
        remains explicit and hands independent work to the normal scheduler. A saved
        in-progress study is not replayed on restart. Source-grounded estimates retain
        exact quote locations but are not adjudicated claims or stopping criteria.
        A task-specific method and separately tracked checks can be established without
        any numeric estimate. Material-scoped pilots do not execute external searches.
    """
    session = state['session']
    session.visited_nodes.append('study_coverage')
    if not session.request.coverage_study or session.request.plan_only or not session.plan:
        return {'session': session}
    runtime = runtime if runtime is not None else {'output_root': output_root}
    if session.coverage_study:
        if session.coverage_study.get('status') == 'running':
            session.coverage_study.update(status='interrupted', error_code='interrupted_outcome_unknown')
            save_study(session, output_root, runtime)
        return {'session': session}
    session.workflow.setdefault('version', 1)
    session.workflow.setdefault('tasks', {})
    session.coverage_study = {'schema_version': 2, 'status': 'running', 'phase': 'plan',
        'coverage_outcome': 'unknown',
        'started_at': time.time(), 'error_code': None, 'plan': None, 'estimates': [],
        'query_ids': [], 'query_goals': {}, 'source_ids': [],
        'limits': {'logical_queries': 2, 'pages': 4, 'model_attempts': 3 * (1 + session.request.model_retries)}}
    study = session.coverage_study
    prior_status, prior_error = session.status, session.error
    save_study(session, output_root)
    try:
        if monitor:
            monitor.check_cancel()
        base = {'original_question': session.request.question, **research_context(session),
            'goals': [g.model_dump(mode='json') for g in session.plan.goals],
            'criteria': [c.model_dump(mode='json') for c in session.plan.completion_criteria],
            'scope_in': session.plan.scope_in, 'scope_out': session.plan.scope_out,
            'search_allowed': session.request.web_search,
            'model_estimate_fallback_enabled': session.request.coverage_estimate_fallback}
        payload = {**base, 'phase': 'plan', 'limits': study['limits'],
            'supplied_sources': [s.model_dump(mode='json') for s in session.sources]}
        answer, receipt = invoke_structured(session, output_root, call_id='coverage_study_plan',
            task='study_coverage', prompt_version='coverage-study-v3',
            messages=[('system', SYSTEM_PROMPT), ('human', json.dumps(payload, ensure_ascii=False))],
            schema=CoverageStudyPlan, key_reader=key_reader, monitor=monitor)
        if answer is None:
            study.update(status='incomplete', error_code=receipt['error_code'])
            return {'session': session}
        study['plan'] = answer.model_dump(mode='json')
        retain_readiness_profile(session, answer.readiness)
        goals = {g.goal_id for g in session.plan.goals}
        sources = {s.source_id for s in session.sources}
        if set(answer.source_ids) - sources or any(set(q.goal_ids) - goals for q in answer.queries):
            study.update(status='incomplete', error_code='invalid_study_references')
            return {'session': session}
        if not answer.needed:
            study.update(status='skipped', phase='finished', skip_reason='model_judged_study_unnecessary',
                coverage_outcome=checked_coverage_outcome(session, 'unnecessary'))
            return {'session': session}
        study['phase'] = 'search'
        save_study(session, output_root, runtime)
        material_only = answer.readiness.evidence_scope == 'provided_material'
        if session.request.web_search and answer.queries and not material_only:
            before = len(session.search.get('queries', []))
            web_search(state, output_root, monitor, retrieval_runtime(session, runtime, monitor), record_visit=False)
            queries = session.search.get('queries', [])[before:]
            study['query_ids'] = [q['query_id'] for q in queries]
            study['query_goals'] = {q['query_id']: q['goal_ids'] for q in queries}
            study['search_status'] = 'completed' if any(q['status'] == 'completed' for q in queries) else 'unavailable'
            study['search_error'] = session.search.get('stop_reason')
        else:
            study['search_status'] = 'not_required_for_provided_material' if material_only else 'disabled' if not session.request.web_search else 'not_requested'
        if session.status == 'cancelled':
            study.update(status='cancelled', error_code='cancelled')
            return {'session': session}
        study['phase'] = 'selection'
        save_study(session, output_root, runtime)
        selected = select_probe_sources(session, output_root, base, SYSTEM_PROMPT, key_reader, monitor)
        if selected is None:
            study.update(status='incomplete', error_code=study['selection']['error_code'])
            return {'session': session}
        study['phase'] = 'retrieve'
        save_study(session, output_root, runtime)
        if selected:
            retrieve_sources(state, output_root, monitor, runtime, only_source_ids=selected, record_visit=False)
        if session.status == 'cancelled':
            study.update(status='cancelled', error_code='cancelled')
            return {'session': session}
        pages, failures = collect_probe_text(session, output_root)
        study['acquisition_failures'] = failures
        study['offered_pages'] = [{k: p[k] for k in ('source_id', 'text_sha256', 'total_characters', 'truncated')} for p in pages]
        fallback_allowed = session.request.coverage_estimate_fallback and study['search_status'] in {'unavailable', 'disabled'}
        study['fallback_allowed'] = fallback_allowed
        if not pages and not fallback_allowed:
            study.update(status='unknown', phase='finished', error_code='no_study_evidence_without_permitted_fallback')
            return {'session': session}
        study['phase'] = 'assess'
        save_study(session, output_root, runtime)
        payload = {**base, 'phase': 'assess', 'study_plan': study['plan'], 'pages': pages,
            'acquisition_failures': failures, 'search_status': study['search_status'],
            'search_queries': [{k: q.get(k) for k in ('query_id', 'query', 'status', 'provider', 'result_count', 'error_code')}
                for q in session.search.get('queries', []) if q['query_id'] in study['query_ids']],
            'page_selection': study['selection']['items'],
            'fallback_allowed': fallback_allowed}
        assessment, receipt = invoke_structured(session, output_root, call_id='coverage_study_assess',
            task='study_coverage', prompt_version='coverage-study-v3',
            messages=[('system', SYSTEM_PROMPT), ('human', json.dumps(payload, ensure_ascii=False))],
            schema=CoverageStudyAssessment, key_reader=key_reader, monitor=monitor)
        if assessment is None:
            study.update(status='incomplete', error_code=receipt['error_code'])
            return {'session': session}
        study['proposal'] = assessment.model_dump(mode='json')
        rejected_checks = retain_readiness_profile(session, assessment.readiness)
        accepted, rejected = validate_estimates(session, assessment, pages, output_root, fallback_allowed=fallback_allowed)
        outcome = checked_coverage_outcome(session, assessment.coverage_outcome)
        if rejected or rejected_checks:
            outcome = 'partial' if study['readiness']['coverage_checks'] else 'unknown'
        study.update(status='partial' if rejected or rejected_checks else 'assessed', phase='finished',
            coverage_outcome=outcome,
            estimate_outcome='not_applicable' if not assessment.readiness.numeric_estimate_applicable else
                'preliminary_range' if any(e['range'] for e in accepted) else 'unknown',
            summary=assessment.summary, coverage_boundary=assessment.coverage_boundary,
            limitations=assessment.limitations, estimates=accepted, rejected_estimates=rejected)
    except (RunCancelled, KeyboardInterrupt, EOFError):
        session.status = 'cancelled'
        study.update(status='cancelled', error_code='cancelled')
    except MonitoringError:
        raise
    except Exception:
        study.update(status='incomplete', error_code='study_adapter_error')
    finally:
        if session.status == 'cancelled':
            study.update(status='cancelled', error_code='cancelled')
        else:
            session.status, session.error = prior_status, prior_error
        save_study(session, output_root, runtime)
    return {'session': session}
