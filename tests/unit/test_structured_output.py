"""Prove format adaptation is explicit and lossless, not permissive JSON repair.

Frozen live failures remain negative evidence. The codec changes the advertised
representation for future calls; it must not retroactively reinterpret old bad
replies or make schema/evidence validators accept missing or invented content.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from projectclarity.knowledge_extraction.knowledge_extraction import validate_response, wire_schema
from projectclarity.model_access.providers.structured_output import (
    NativeJSONCodec, NullableListCodec, JSONTextCodec, OutputStructureError, inline_schema,
)

SOURCE = 'Acme Labs is based in Ottawa.'
VALID = {'entities': [{'id': 'acme', 'name': 'Acme Labs', 'type': 'organization', 'subtype': None}],
         'facts': [{'subject_id': 'acme', 'predicate': 'based_in', 'value': 'Ottawa',
                    'object_id': None, 'statement': SOURCE, 'quote': SOURCE}]}


class StructuredOutputTests(unittest.TestCase):
    """Check the shared codec and unchanged application validator together."""

    def test_roundtrip_and_source_schema_unchanged(self):
        """Retain exact content while making nullable values explicit to the model.

        Args: None.
        Returns: None; validates both wire and canonical shapes and audit paths.
            This is a codec proof, not evidence that a model generates valid data.
        """
        schema = wire_schema(); original = deepcopy(schema)
        codec = NullableListCodec(schema)
        wire = codec.encode(VALID); before = deepcopy(wire)
        Draft202012Validator(codec.schema).validate(wire)
        self.assertEqual(wire['entities'][0]['subtype'], [])
        result = codec.decode(wire)
        self.assertEqual(result.value, VALID)
        self.assertEqual(schema, original)
        self.assertEqual(wire, before)
        self.assertEqual({item['path'] for item in result.conversions},
                         {'/entities/0/subtype', '/facts/0/object_id'})
        self.assertEqual(len(validate_response(json.dumps(result.value), SOURCE).facts), 1)

    def test_json_prefill_is_exact_framing_not_json_repair(self):
        """Accept only a valid continuation of the prefix actually sent to the model.

        Args: None.
        Returns: None; valid content remains unchanged; duplicate keys, prose,
            fences, non-finite numbers and truncated JSON fail without guessing.
        """
        codec = JSONTextCodec(wire_schema())
        encoded = json.dumps(VALID)
        self.assertEqual(codec.decode(encoded[1:]).value, VALID)
        self.assertEqual(codec.decode(encoded[1:]).conversions[0]['prefix'], '{')
        for bad in ('"x": 1, "x": 2}', '"x": NaN}', '"x": Infinity}',
                    '"x": 1} extra prose', '```json\n{}\n```', '"entities": ['):
            with self.subTest(text=bad), self.assertRaises(OutputStructureError):
                codec.decode(bad)
        complete = JSONTextCodec(wire_schema(), prefix='')
        self.assertEqual(complete.decode(encoded).value, VALID)

    def test_exact_entity_echo_replay_keeps_all_original_facts(self):
        """Recover harmless catalogue echoes from a frozen real Nova review.

        Args: None.
        Returns: None; the real review validator applies two supported links,
            records three exact echoes and changes no other fact fields. No calls.
        """
        from projectclarity.knowledge_extraction.entity_omission_repair import repair_omissions
        from unittest.mock import Mock
        path = Path(__file__).resolve().parents[1] / 'fixtures/nova_entity_echo.json'
        fixture = json.loads(path.read_text())
        caller = Mock(return_value=fixture['raw_review']); caller.model = 'offline-fixture'
        original = deepcopy(fixture['original_parsed'])
        output, audit = repair_omissions(original, fixture['source'], caller, 's', 'echo-review', lambda: False)
        self.assertEqual(audit['status'], 'checks_pass')
        self.assertEqual(len(audit['entity_normalization']), 3)
        self.assertEqual(audit['raw_text'], fixture['raw_review'])
        self.assertEqual(original, fixture['original_parsed'])
        self.assertEqual([f['object_id'] for f in output['facts']], ['Ottawa', 'Acme Labs'])
        for before, after in zip(original['facts'], output['facts']):
            self.assertEqual({**after, 'object_id': before['object_id']}, before)
        caller.assert_called_once()

    def test_entity_normalizer_rejects_conflicts_and_duplicate_proposals(self):
        """Permit only exact known echoes, not redefinitions or duplicate new data.

        Args: None.
        Returns: None; conflicts and repeated IDs still fail, while new entities
            and decision content are not rewritten by the compatibility rule.
        """
        from projectclarity.knowledge_extraction.entity_output_normalization import normalize_entity_echoes
        entity = VALID['entities'][0]
        for entities in ([{**entity, 'name': 'Changed'}], [entity, entity],
                         [{**entity, 'id': 'new'}, {**entity, 'id': 'new'}]):
            with self.subTest(entities=entities), self.assertRaises(ValueError):
                normalize_entity_echoes({'entities': entities, 'decisions': []}, [entity])
        new = {**entity, 'id': 'new'}
        review = {'entities': [entity, new], 'decisions': [{'arbitrary': 'untouched'}]}
        output, audit = normalize_entity_echoes(review, [entity])
        self.assertEqual(output['entities'], [new])
        self.assertEqual(output['decisions'], review['decisions'])
        self.assertEqual(len(audit), 1)

    def test_present_strings_and_literal_null_are_not_guessed(self):
        """Distinguish a real string from an explicit absent value.

        Args: None.
        Returns: None; strings including null, whitespace and Unicode are unchanged.
        """
        codec = NullableListCodec({'type': 'object', 'properties': {
            'value': {'anyOf': [{'type': 'string'}, {'type': 'null'}]}}, 'required': ['value']})
        for value in ('null', 'NULL', '  café  ', '', None):
            with self.subTest(value=value):
                self.assertEqual(codec.decode(codec.encode({'value': value})).value, {'value': value})
        with self.assertRaises(OutputStructureError):
            codec.decode({'value': 'null'})

    def test_malformed_containers_are_not_repaired(self):
        """Reject ambiguous lists instead of picking a value or inventing null.

        Args: None.
        Returns: None; multiple values, bare null, nested null and scalars fail.
        """
        codec = NullableListCodec(wire_schema())
        for bad in (None, 'null', ['one', 'two'], [None], 0, {}):
            wire = codec.encode(VALID); wire['facts'][0]['object_id'] = bad
            with self.subTest(value=bad), self.assertRaises(OutputStructureError):
                codec.decode(wire)

    def test_missing_extra_and_bad_evidence_still_fail_validation(self):
        """Leave ordinary content failures visible to existing application checks.

        Args: None.
        Returns: None; no field defaults, extra-field removal, quote repair or
            dangling-reference correction occurs in the decoder.
        """
        codec = NullableListCodec(wire_schema())
        missing = codec.encode(VALID); del missing['entities'][0]['subtype']
        extra = codec.encode(VALID); extra['invented'] = 'retained'
        quote = codec.encode(VALID); quote['facts'][0]['quote'] = 'Not in the source'
        dangling = codec.encode(VALID); dangling['facts'][0]['object_id'] = ['null']
        for value in (missing, extra, quote, dangling):
            decoded = codec.decode(value).value
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_response(json.dumps(decoded), SOURCE)
        self.assertNotIn('subtype', codec.decode(missing).value['entities'][0])
        self.assertIn('invented', codec.decode(extra).value)

    def test_frozen_live_failures_remain_rejected(self):
        """Replay original Nova failures without modifying or reinterpreting them.

        Args: None.
        Returns: None; both historic replies still fail the canonical validator.
            The new representation does not retroactively make them successful.
        """
        path = Path(__file__).resolve().parents[1] / 'fixtures/nova_structure_failures.json'
        for fixture in json.loads(path.read_text()):
            with self.subTest(origin=fixture['origin']), self.assertRaises(ValueError):
                validate_response(fixture['raw_text'], fixture['source'])

    def test_nullable_objects_arrays_and_escaped_paths(self):
        """Round-trip nesting without confusing nullable lists with ordinary arrays.

        Args: None.
        Returns: None; nested objects, nullable arrays and JSON-pointer escapes work.
        """
        schema = {'type': 'object', 'properties': {'a/b~c': {'anyOf': [
            {'type': 'object', 'properties': {'items': {'anyOf': [
                {'type': 'array', 'items': {'type': 'integer'}}, {'type': 'null'}]}},
             'required': ['items']}, {'type': 'null'}]}}, 'required': ['a/b~c']}
        codec = NullableListCodec(schema)
        for value in ({'a/b~c': None}, {'a/b~c': {'items': []}}, {'a/b~c': {'items': [1, 2]}}, {'a/b~c': {'items': None}}):
            wire = codec.encode(value)
            Draft202012Validator(codec.schema).validate(wire)
            decoded = codec.decode(wire)
            self.assertEqual(decoded.value, value)
            self.assertEqual(decoded.conversions[0]['path'], '/a~1b~0c')

    def test_identity_codec_does_not_change_native_json(self):
        """Keep the native JSON path independent of the Nova representation.

        Args: None.
        Returns: None; identity codec copies schemas/data with no conversions.
        """
        schema = wire_schema(); codec = NativeJSONCodec(schema)
        self.assertEqual(codec.schema, schema)
        self.assertEqual(codec.decode(VALID).value, VALID)
        self.assertEqual(codec.decode(VALID).conversions, [])

    def test_unsupported_and_external_schemas_fail_before_inference(self):
        """Fail closed where the codec cannot promise reversible semantics.

        Args: None.
        Returns: None; unsupported unions, cycles and external refs are rejected
            locally; no external schema resolution or model request is performed.
        """
        for schema in ({'$ref': 'https://example.invalid/schema'},
                       {'$defs': {'cycle': {'$ref': '#/$defs/cycle'}}, '$ref': '#/$defs/cycle'},
                       {'anyOf': [{'type': 'string'}, {'type': 'integer'}]},
                       {'type': ['string', 'null'], 'const': 'must-not-be-null'},
                       {'allOf': [{'type': 'string'}]}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                NullableListCodec(schema)

    def test_reference_expansion_preserves_property_names_and_enums(self):
        """Resolve schema refs without treating user field names as schema syntax.

        Args: None.
        Returns: None; a property named $defs and literal $ref data survive exactly.
        """
        schema = {'type': 'object', 'properties': {'$defs': {'type': 'string', 'enum': ['$ref']}}}
        self.assertEqual(inline_schema(schema), schema)
