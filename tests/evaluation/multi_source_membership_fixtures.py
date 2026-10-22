"""Provide tiny artificial pages/replies for the combined membership regression.

These sentences are authored test data, NOT downloaded official records. They
use the reference identities to exercise the real pipeline without credentials,
network access or model charges. Overlapping Balint names test deduplication;
Sanders's official-name variant tests alias matching. Expected outputs here are
independent of the evaluator's answer-key loader, so changing an answer key does
not silently rewrite the fake model's answer to match it.
"""

# Separate pages contain different people; no single page contains the full set.
PAGE_MEMBERS = [
    [('Brendan Hanley', 'House of Commons of Canada', 'Yukon')],
    [('Lori Idlout', 'House of Commons of Canada', 'Nunavut')],
    [('Rebecca A. Balint', 'United States House of Representatives', 'Vermont')],
    [('Becca Balint', 'United States House of Representatives', 'Vermont'),
     ('Bernard Sanders', 'United States Senate', 'Vermont'),
     ('Peter Welch', 'United States Senate', 'Vermont')],
]


def page_and_reply(members):
    """Create source HTML and a provider-shaped extraction from explicit fixture rows.

    Each person has two atomic claims anchored to one exact sentence. Entity IDs
    are local to this page, just as in real independently extracted documents.
    """
    entities, claims, sentences = [], [], []
    for index, (name, chamber, region) in enumerate(members):
        person, office, area = [f'E{3 * index + n}' for n in (1, 2, 3)]
        entities.extend([{'entity_id': person, 'name': name, 'entity_type': 'Person'},
            {'entity_id': office, 'name': chamber, 'entity_type': 'Organization'},
            {'entity_id': area, 'name': region, 'entity_type': 'Location'}])
        quote = f'{name} is a current member of {chamber} and represents {region}.'
        sentences.append(quote)
        for predicate, target, statement in [
            ('MEMBER_OF', office, f'{name} is a current member of {chamber}.'),
            ('REPRESENTS', area, f'{name} represents {region}.')]:
            claims.append({'claim_id': f'C{len(claims) + 1}', 'statement': statement,
                'goal_ids': ['G1'], 'entity_ids': [person, target],
                'relationship': {'subject_id': person, 'predicate': predicate, 'object_id': target},
                'qualifications': [], 'evidence': [{'passage_id': 'P1', 'quote': quote}]})
    html = ('<main>' + ''.join('<p>' + sentence + '</p>' for sentence in sentences) + '</main>').encode()
    return html, {'entities': entities, 'claims': claims}
