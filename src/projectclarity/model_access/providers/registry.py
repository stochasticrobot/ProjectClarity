"""Choose the configured AI service without teaching research steps its API.

This file associates service names with their adapters and default models. It
combines explicit options, environment variables and a private settings file in
a defined order. Unknown names fail instead of selecting another service. Reading
configuration and creating an adapter do not themselves send a model request.
"""
import os
import hashlib
import json
from dotenv import dotenv_values
from projectclarity.model_access.providers.contracts import ProviderConfig

_PROVIDERS = {}


def register_provider(name, factory, default_model, contract_version='1', contract_keys=()):
    """Register an adapter at application startup, including future local adapters.

    Args: name: Stable lower-case provider key; factory: ProviderConfig -> adapter;
        default_model: Model ID used when configuration does not override it.
        contract_version: Adapter output-policy version; bump on incompatible changes.
        contract_keys: Setting names affecting output/endpoint identity, excluding
            credentials. Values are hashed, never included in public identity text.
    Returns: None. Adds one factory to the process-local registry; duplicate, empty
        or non-lower-case names raise ValueError. Register before starting workers;
        this function supplies no synchronization or factory/interface validation.
        No network activity occurs.
    """
    if not name or name != name.lower() or name in _PROVIDERS:
        raise ValueError('Invalid or duplicate provider registration.')
    _PROVIDERS[name] = (factory, default_model, contract_version, tuple(contract_keys))


def resolve_config(env_file, provider=None, model=None):
    """Resolve selection and private settings without changing global environment.

    Args: env_file: Private dotenv path; provider: Optional explicit provider;
        model: Optional explicit model. Explicit options win over process values,
        then dotenv settings. Missing provider defaults to Anthropic.
    Returns: Private ProviderConfig with an output-contract fingerprint. Unknown providers fail without fallback.
        Reads credentials but makes no network calls; values are omitted from repr.
    """
    values = {**dotenv_values(env_file, interpolate=False), **os.environ}
    name = provider or values.get('PROJECTCLARITY_PROVIDER') or 'anthropic'
    if name not in _PROVIDERS:
        raise ValueError(f'Unknown model provider: {name}. Available: {", ".join(sorted(_PROVIDERS))}')
    chosen = model or values.get('PROJECTCLARITY_MODEL') or _PROVIDERS[name][1]
    definition = _PROVIDERS[name]
    identity = {'provider': name, 'model': chosen, 'version': definition[2],
                'options': {key: values.get(key) for key in definition[3]}}
    contract_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return ProviderConfig(name, chosen, values, contract_id)


def create_provider(config):
    """Instantiate the registered adapter without introducing workflow branches.

    Args: config: Resolved private provider configuration.
    Returns: ModelProvider implementation; invalid credentials/settings may raise.
        An unregistered config.provider raises KeyError. Factory construction
        never makes a network request; SDK clients are owned by adapter operations.
    """
    return _PROVIDERS[config.provider][0](config)


from projectclarity.model_access.providers.anthropic_provider import AnthropicProvider
from projectclarity.model_access.providers.bedrock_provider import BedrockProvider

register_provider('anthropic', AnthropicProvider, 'claude-haiku-5-5', 'native-json-v1')
register_provider('bedrock', BedrockProvider, 'us.amazon.nova-micro-v1:0', 'output-codecs-v1',
                  ('PROJECTCLARITY_BEDROCK_OUTPUT', 'AWS_REGION'))
