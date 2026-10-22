"""Build the current provider adapter without owning a research task's prompt.

Planning, extraction and review are tasks; Anthropic is our present transport.
This factory lets review/discovery request a typed answer without importing a
provider in their nodes. Replacing the adapter requires equivalent structured
output, usage and completion metadata; other backends are not implemented yet.
Construction does not send anything or grant tool permissions.
"""
from langchain_anthropic import ChatAnthropic


def create_structured_model(api_key, session, schema):
    """Return a no-retry LangChain runnable with parsed output and raw answer metadata."""
    model = ChatAnthropic(model=session.request.model, api_key=api_key,
        base_url="https://api.anthropic.com", max_tokens=session.request.budget.max_output_tokens,
        timeout=60.0, max_retries=0, output_config={"effort": "low"})
    return model.with_structured_output(schema, method="json_schema", include_raw=True)
