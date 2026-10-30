"""Connect research tasks to the model provider without defining their instructions.

Task code prepares messages and an expected response schema. Modules in this package
construct the Anthropic adapter, execute and account for requests, handle limited
transport retries, and retain diagnostic records. Planning, extraction and review
remain separate tasks even though they use the same provider. Importing the package
does not send a request or read credentials.
"""
