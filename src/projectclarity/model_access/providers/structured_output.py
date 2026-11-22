"""Translate declared model output formats without guessing missing research data.

Different services can struggle with different JSON shapes. A response codec
advertises one explicit shape to a model and converts that shape back to the
application's unchanged answer structure. The raw service reply stays available
for inspection. A codec is not an evidence validator or a repair model: it never
fills omitted fields, drops unknown fields, or turns the string "null" into null.

The nullable-list codec represents an absent value as [] and a present value as
[value]. Lists containing two or more items are invalid. It can be reused by any
provider without importing an SDK or changing the research stages.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Protocol

from jsonschema import Draft202012Validator


class OutputStructureError(ValueError):
    """A returned value cannot be decoded using the advertised representation."""


@dataclass
class DecodedOutput:
    """Hold canonical data and value-free conversion records.

    Args:
        value: Detached decoded JSON data; original provider data stays unchanged.
        conversions: JSON-pointer paths and operation names, without copied values.
    Returns:
        Result ready for the application's existing schema/evidence validators.
        Successful decoding alone does not establish schema or research validity.
    """
    value: object
    conversions: list[dict]


class ResponseCodec(Protocol):
    """Provider-independent interface for explicit output representations.

    name is a versioned format identifier; schema is what the model is asked to
    return. decode must preserve meaning and fail on ambiguous representation.
    Providers choose a codec; workflows never select or implement one.
    """
    name: str
    schema: dict

    def decode(self, value) -> DecodedOutput:
        """Convert only the representation promised in this codec's schema.

        Args: value: Parsed provider JSON, treated as read-only.
        Returns: Canonical data and conversion records; malformed encoding raises
            OutputStructureError. No model call, schema repair or fact inference.
        """
        ...


def inline_schema(schema):
    """Resolve local schema references so codecs can operate on complete fields.

    Args: schema: Read-only JSON schema with acyclic local JSON-pointer references.
    Returns: Detached expanded schema, without definition catalogues. Malformed
        schemas raise jsonschema.SchemaError. External,
        cyclic or sibling-constrained references and invalid pointers raise
        ValueError rather than performing network resolution or losing constraints.
        Property names and enum/constant data are not treated as schema keywords.
    """
    Draft202012Validator.check_schema(schema)

    def expand(node, seen=()):
        """Resolve references only in schema-bearing positions.

        Args: node: Current schema or boolean constraint; seen: Active ref stack.
        Returns: Expanded copy; unsupported reference composition raises ValueError.
        """
        if not isinstance(node, dict):
            return deepcopy(node)
        if '$ref' in node:
            ref = node['$ref']
            if not ref.startswith('#/') or ref in seen:
                raise ValueError('Response formats require acyclic local schema references.')
            siblings = set(node) - {'$ref', 'title', 'description'}
            if siblings:
                raise ValueError('Constrained reference siblings need an explicit codec implementation.')
            target = schema
            try:
                for part in ref[2:].split('/'):
                    target = target[part.replace('~1', '/').replace('~0', '~')]
            except (KeyError, TypeError) as error:
                raise ValueError('Invalid local schema reference.') from error
            result = expand(target, (*seen, ref))
            return {**result, **{k: v for k, v in node.items() if k != '$ref'}}
        result = deepcopy(node)
        result.pop('$defs', None)
        for key in ('properties', 'patternProperties', 'dependentSchemas'):
            if key in node:
                result[key] = {name: expand(child, seen) for name, child in node[key].items()}
        for key in ('items', 'additionalProperties', 'contains', 'not', 'if', 'then', 'else', 'propertyNames'):
            if isinstance(node.get(key), dict):
                result[key] = expand(node[key], seen)
        for key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            if key in node:
                result[key] = [expand(child, seen) for child in node[key]]
        return result

    return expand(schema)


class NativeJSONCodec:
    """Pass native JSON through unchanged while offering the common codec contract."""
    name = 'native_json_v1'

    def __init__(self, schema):
        """Retain the provider's supported schema without rewriting it.

        Args: schema: Application response schema, potentially containing refs.
        Returns: None; schema is copied, with no provider or filesystem activity.
        """
        self.schema = deepcopy(schema)

    def decode(self, value):
        """Preserve native values for the ordinary stage validators.

        Args: value: Parsed JSON data, not assumed valid research content.
        Returns: Detached value and no conversions. No missing fields are supplied.
        """
        return DecodedOutput(deepcopy(value), [])


class JSONTextCodec(NativeJSONCodec):
    """Parse exactly the advertised JSON text, optionally continuing a fixed prefix."""
    name = 'json_prefill_v1'

    def __init__(self, schema, prefix='{'):
        """Declare a text response and the exact prefix supplied to the model.

        Args: schema: Canonical response schema; prefix: Assistant text already sent
            in the request, defaulting to an opening object brace. Use an empty
            string for services returning complete JSON text without prefilling.
        Returns: None; stores copied schema and immutable prefix, with no I/O.
        """
        super().__init__(schema)
        self.prefix = prefix

    def decode(self, value):
        """Parse the exact continuation without stripping fences or repairing JSON.

        Args: value: Raw model text continuing the configured prefix.
        Returns: Canonical JSON value and a framing audit record. Invalid JSON,
            duplicate keys, non-finite numbers, extra text or non-text input raise
            OutputStructureError. No fields, nulls or facts are guessed or supplied.
        """
        def unique_object(pairs):
            """Reject duplicate fields rather than silently keeping the last value.

            Args: pairs: Ordered key/value pairs supplied by the JSON parser.
            Returns: Object mapping; duplicate fields raise OutputStructureError.
            """
            result = {}
            for key, item in pairs:
                if key in result:
                    raise OutputStructureError('Duplicate JSON object field.')
                result[key] = item
            return result

        def reject_constant(value):
            """Reject JSON extensions that ordinary schemas do not permit.

            Args: value: Parser token for NaN or infinity; not logged as user data.
            Returns: Never returns; raises OutputStructureError without coercion.
            """
            raise OutputStructureError('Non-finite JSON numbers are not supported.')

        if not isinstance(value, str):
            raise OutputStructureError('Expected JSON response text.')
        try:
            parsed = json.loads(self.prefix + value, object_pairs_hook=unique_object,
                                parse_constant=reject_constant)
        except json.JSONDecodeError as error:
            raise OutputStructureError(f'Invalid JSON response at line {error.lineno}, column {error.colno}.') from error
        return DecodedOutput(parsed, [{'path': '', 'operation': 'parse_json_text',
                                      'prefix': self.prefix}])


class NullableListCodec:
    """Represent nullable values as zero-or-one lists, with no guessed coercions."""
    name = 'nullable_list_v1'

    def __init__(self, schema):
        """Compile an explicit provider schema and reversible conversion plan.

        Args: schema: Canonical application schema; it remains unchanged.
        Returns: None; stores expanded wire schema and a private traversal plan.
            Malformed schemas raise jsonschema.SchemaError.
            Supports object properties, homogeneous arrays and one non-null/null
            union. Other composition/conditional shapes fail before any model call.
        """
        self.canonical_schema = deepcopy(schema)
        self.schema, self._plan = self._compile(inline_schema(schema))
        Draft202012Validator.check_schema(self.schema)

    def _compile(self, schema):
        """Build matching schema/plan trees without changing source constraints.

        Args: schema: Expanded canonical subtree; no external references remain.
        Returns: Pair of wire-schema copy and traversal plan. Unsupported schema
            composition raises ValueError; no silent constraint removal is allowed.
        """
        if isinstance(schema, bool):
            return schema, {'kind': 'identity'}
        node = deepcopy(schema)
        forbidden = {'oneOf', 'allOf', 'not', 'if', 'then', 'else', 'prefixItems',
                     'patternProperties', 'dependentSchemas', 'contains', 'unevaluatedProperties'}
        if forbidden & set(node):
            raise ValueError('Unsupported response-format composition: ' + ', '.join(sorted(forbidden & set(node))))
        alternatives = node.get('anyOf')
        if isinstance(node.get('type'), list):
            if {'enum', 'const'} & set(node):
                raise ValueError('Nullable type unions with enum/const need explicit codec support.')
            if set(node['type']) - {'null'} and len(node['type']) == 2 and 'null' in node['type']:
                nonnull = {**node, 'type': next(t for t in node['type'] if t != 'null')}
                alternatives = [nonnull, {'type': 'null'}]
                node = {}
            else:
                raise ValueError('Only a single non-null/null type union is supported.')
        if alternatives is not None:
            nulls = [item for item in alternatives if item == {'type': 'null'}]
            others = [item for item in alternatives if item != {'type': 'null'}]
            if len(nulls) != 1 or len(others) != 1:
                raise ValueError('Only a single non-null/null alternative is supported.')
            if set(node) - {'anyOf', 'title', 'description', 'default'}:
                raise ValueError('Nullable sibling constraints need explicit codec support.')
            items, child = self._compile(others[0])
            description = ('Value container: return an empty list [] when there is no value, '
                           'or a list with exactly one value when it is present. '
                           'A field marked required must still be included when its list is empty. '
                           'Do not use the text "null" to mean no value.')
            if node.get('description'):
                description += ' ' + node['description']
            wire = {'type': 'array', 'minItems': 0, 'maxItems': 1,
                    'items': items, 'description': description}
            if 'title' in node:
                wire['title'] = node['title']
            if 'default' in node:
                wire['default'] = self._convert(node['default'], {'kind': 'nullable', 'child': child}, '', [], True)
            return wire, {'kind': 'nullable', 'child': child}
        plan = {'kind': 'identity'}
        if node.get('type') == 'object' or 'properties' in node:
            properties, children = {}, {}
            for key, value in node.get('properties', {}).items():
                properties[key], children[key] = self._compile(value)
            node['properties'] = properties
            if node.get('required'):
                required = 'Every object here must include these fields: ' + ', '.join(node['required']) + '.'
                node['description'] = (node.get('description', '') + ' ' + required).strip()
            additional = None
            if isinstance(node.get('additionalProperties'), dict):
                node['additionalProperties'], additional = self._compile(node['additionalProperties'])
            plan = {'kind': 'object', 'children': children, 'additional': additional}
        elif node.get('type') == 'array':
            node['items'], child = self._compile(node.get('items', True))
            plan = {'kind': 'array', 'child': child}
        return node, plan

    def _convert(self, value, plan, path, conversions, encode):
        """Walk only declared format boundaries, preserving all other information.

        Args:
            value: Read-only JSON subtree.
            plan: Matching codec traversal node.
            path: JSON pointer to the canonical value, used for audit/error location.
            conversions: Caller-owned list receiving value-free audit records.
            encode: True for canonical-to-wire test/example encoding; False to decode.
        Returns:
            Detached subtree. Invalid nullable-list shapes raise OutputStructureError.
            Missing fields and unknown keys are retained as-is for stage validation;
            no defaults, literal coercion, content repair or field removal occurs.
        """
        kind = plan['kind']
        if kind == 'nullable':
            if encode:
                return [] if value is None else [self._convert(value, plan['child'], path, conversions, True)]
            if not isinstance(value, list) or len(value) > 1 or (value and value[0] is None):
                raise OutputStructureError(f'{path or "/"}: expected a zero-or-one value list.')
            conversions.append({'path': path, 'operation': 'empty_list_to_null' if not value else 'single_item_to_value'})
            return None if not value else self._convert(value[0], plan['child'], path, conversions, False)
        if kind == 'object' and isinstance(value, dict):
            result = {}
            for key, item in value.items():
                child = plan['children'].get(key, plan['additional'])
                pointer = path + '/' + key.replace('~', '~0').replace('/', '~1')
                result[key] = self._convert(item, child, pointer, conversions, encode) if child else deepcopy(item)
            return result
        if kind == 'array' and isinstance(value, list):
            return [self._convert(item, plan['child'], path + '/' + str(index), conversions, encode)
                    for index, item in enumerate(value)]
        return deepcopy(value)

    def encode(self, value):
        """Produce the advertised wire representation from known canonical data.

        Args: value: Canonical JSON data for fixtures or inspected examples.
        Returns: Detached encoded value; no fields are supplied or validated. Live
            inference uses schema, not this method or a pre-authored answer.
        """
        return self._convert(value, self._plan, '', [], True)

    def decode(self, value):
        """Translate the advertised representation without repairing research data.

        Args: value: Parsed, complete model output in nullable-list format.
        Returns: DecodedOutput with canonical data and value-free conversion audit.
            Malformed nullable containers raise OutputStructureError; missing and
            unknown fields stay available to ordinary validation/recovery checks.
        """
        conversions = []
        return DecodedOutput(self._convert(value, self._plan, '', conversions, False), conversions)
