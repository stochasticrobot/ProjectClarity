"""Construct the Anthropic model adapter used by research tasks expecting structured replies.

A task supplies instructions and a response schema describing the fields it needs.
This factory configures the LangChain adapter that sends those messages to Anthropic
and returns both parsed output and provider metadata. Planning, review and source
selection can therefore use the same provider setup without defining instructions
inside the network adapter. Constructing it sends no request and grants no tools.
Other providers are not implemented by this factory.
"""
from langchain_anthropic import ChatAnthropic


def create_structured_model(api_key, session, schema, *, timeout_seconds=None, max_output_tokens=None):
    """Return a no-retry LangChain runnable with parsed output and raw answer metadata.

    Args:
        api_key: Transient model-provider credential; used to configure the client, not
            saved as research data.
        session: ResearchSession containing this run's request, plan, source records,
            accumulated results and status.
        schema: Pydantic response-model class defining the structured answer expected from
            the provider.
        max_output_tokens: Optional task-specific output allowance; None preserves
            the shared default for review, planning and coverage.
        timeout_seconds: Optional attempt-specific timeout capped by remaining run time;
            None uses the saved request's model timeout. Application retries live elsewhere.

    Returns:
        A configured LangChain runnable returning raw, parsed and parsing_error fields when
        invoked; construction itself sends no request.
    """
    model = ChatAnthropic(model=session.request.model, api_key=api_key,
        base_url="https://api.anthropic.com", max_tokens=session.request.budget.max_output_tokens if max_output_tokens is None else max_output_tokens,
        timeout=session.request.model_timeout_seconds if timeout_seconds is None else timeout_seconds,
        max_retries=0, output_config={"effort": "low"})
    return model.with_structured_output(schema, method="json_schema", include_raw=True)
