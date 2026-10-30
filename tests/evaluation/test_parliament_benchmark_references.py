"""Validate the two frozen Parliament-page answer keys against their own saved inputs.

Tests check hashes, CSV/JSON agreement, unique member–riding pairs and source
coordinates. The official page's separate list layout cross-checks its cards;
Wikipedia's table checks preserve shared cells and former-member exclusions.
Disagreements between pages stay separate. These tests validate reference data,
not a live model's extraction or today's membership.
"""
import csv
import hashlib
import json
from pathlib import Path
import unittest

from bs4 import BeautifulSoup

from evaluation.membership_result_scoring import normalize_name, relation_metrics


ROOT = Path(__file__).resolve().parents[2] / 'benchmarks' / 'canadian_parliament'


def reference(stem):
    """Read one independent answer key for offline dataset validation.

    Args:
        stem: Frozen source filename without its extension.

    Returns:
        Decoded reference dictionary; missing or malformed files raise normally.
    """
    return json.loads((ROOT / f'{stem}.expected.json').read_text())


class ParliamentReferenceTests(unittest.TestCase):
    """Check source integrity, pair provenance, history and false associations."""

    def test_hashes_csv_and_unique_pairs(self):
        """Keep each answer key bound to its own frozen page and inspectable CSV.

        Args:
            self: Test instance providing assertions.

        Returns:
            None; fails on changed inputs, inconsistent CSVs or duplicate records.
        """
        manifest = json.loads((ROOT / 'sources.json').read_text())
        for source in manifest['sources']:
            stem = Path(source['file']).stem
            expected = reference(stem)
            digest = hashlib.sha256((ROOT / source['file']).read_bytes()).hexdigest()
            self.assertEqual(digest, source['sha256'])
            self.assertEqual(digest, expected['source_sha256'])
            rows = expected['expected_pairs']
            self.assertEqual(len(rows), expected['expected_pair_count'])
            self.assertEqual(len(rows), 337)
            self.assertEqual(len({row['mp'] for row in rows}), 337)
            self.assertEqual(len({row['riding'] for row in rows}), 337)
            with (ROOT / f'{stem}.expected.csv').open(newline='') as handle:
                self.assertEqual(list(csv.DictReader(handle)),
                                 [{key: row[key] for key in ('mp', 'riding')} for row in rows])

    def test_official_pairs_against_separate_list_layout(self):
        """Cross-check tile-derived answers against the independent list presentation.

        Args:
            self: Test instance providing assertions.

        Returns:
            None; fails if a name is attached to another member's riding or ID.
        """
        soup = BeautifulSoup((ROOT / 'house_of_commons.html').read_bytes(), 'html.parser')
        expected = reference('house_of_commons')['expected_pairs']
        self.assertEqual(len(soup.select('tr[id^="mp-list-id-"]')), len(expected))
        for record in expected:
            row = soup.select_one(record['provenance']['list_selector'])
            cells = row.find_all('td', recursive=False)
            last, first = cells[0].get_text(' ', strip=True).split(', ', 1)
            first = first.removeprefix('Right Hon. ').removeprefix('Hon. ')
            self.assertEqual(first + ' ' + last, record['mp'])
            self.assertEqual([c.get_text(' ', strip=True) for c in cells[1:]],
                             [record[key] for key in ('party', 'riding', 'province')])

    def test_wikipedia_cell_provenance_and_history(self):
        """Account for every roster row while retaining shared cells and former MPs.

        Args:
            self: Test instance providing assertions.

        Returns:
            None; fails on lost rows, wrong source cells or historical inclusion.
        """
        data = reference('wikipedia')
        soup = BeautifulSoup((ROOT / 'wikipedia.html').read_bytes(), 'html.parser')
        tables = soup.select('table')
        accounted = set()
        for active, records in [(True, data['expected_pairs']),
                                (False, data['excluded_historical_pairs'])]:
            for record in records:
                for location in record['provenance']:
                    coordinate = location['table_index'], location['row_index']
                    self.assertNotIn(coordinate, accounted)
                    accounted.add(coordinate)
                    self.assertEqual('until ' not in location['name_annotation'].lower(), active)
                    for field, origin_key in [('mp', 'name_cell'), ('riding', 'riding_cell')]:
                        origin = location[origin_key]
                        self.assertEqual(origin['table_index'], location['table_index'])
                        cell = tables[origin['table_index']].find_all('tr')[origin['row_index']].find_all(
                            ['td', 'th'], recursive=False)[origin['cell_index']]
                        self.assertLessEqual(origin['row_index'], location['row_index'])
                        self.assertLess(location['row_index'], origin['row_index'] + int(cell.get('rowspan', 1)))
                        value = cell.select_one('a[href]').get_text(' ', strip=True) if field == 'mp' else cell.get_text(' ', strip=True)
                        self.assertEqual(record[field], value)
        roster_rows = set()
        for index, table in enumerate(tables):
            rows = table.find_all('tr')
            if 'Electoral district' in [cell.get_text(' ', strip=True) for cell in rows[0].find_all(['td', 'th'])]:
                roster_rows.update((index, row) for row in range(1, len(rows)))
        self.assertEqual(accounted, roster_rows)
        self.assertEqual(len(accounted), 356)
        self.assertEqual(len(data['excluded_historical_pairs']), 12)
        by_name = {r['mp']: r for r in data['expected_pairs']}
        self.assertEqual(by_name['Pierre Poilievre']['riding'], 'Battle River—Crowfoot')
        self.assertEqual(by_name['Danielle Martin']['riding'], 'University—Rosedale')
        self.assertNotIn('Chrystia Freeland', by_name)
        self.assertNotIn('Damien Kurek', by_name)

    def test_source_disagreements_remain_separate(self):
        """Prevent one page's expected answer from being silently copied to the other.

        Args:
            self: Test instance providing assertions.

        Returns:
            None; fails if known member or label disagreements are erased.
        """
        official = {r['riding']: r['mp'] for r in reference('house_of_commons')['expected_pairs']}
        wiki = {r['riding']: r['mp'] for r in reference('wikipedia')['expected_pairs']}
        riding = 'Sudbury East—Manitoulin—Nickel Belt'
        self.assertEqual(official[riding], 'Jim Bélanger')
        self.assertEqual(wiki[riding], 'Marc Serré')
        self.assertEqual(official['London Centre'], 'Peter Fragiskatos')
        self.assertEqual(wiki['London North Centre'], 'Peter Fragiskatos')

    def test_wrong_associations_are_not_correct_pairs(self):
        """Reject real names paired with real but incorrect ridings, despite valid counts.

        Args:
            self: Test instance providing assertions.

        Returns:
            None; fails if counting entities alone can satisfy pair-level scoring.
        """
        wrong = [('Eric Melillo', 'Kapuskasing—Timmins—Mushkegowuk'),
                 ('Natilien Joseph', 'Beaches—East York'),
                 ('Ned Kuruc', 'Longueuil—Saint-Hubert'),
                 ('Patty Hajdu', 'West Vancouver—Sunshine Coast—Sea to Sky Country'),
                 ('Robert Oliphant', 'Egmont')]
        expected = {(normalize_name(r['mp']), normalize_name(r['riding']))
                    for r in reference('house_of_commons')['expected_pairs']}
        observed = {(normalize_name(mp), normalize_name(riding)) for mp, riding in wrong}
        self.assertTrue({mp for mp, _ in observed} <= {mp for mp, _ in expected})
        self.assertTrue({riding for _, riding in observed} <= {riding for _, riding in expected})
        result = relation_metrics(expected, observed)
        self.assertEqual(result['correct_count'], 0)
        self.assertEqual(result['precision'], 0)
        self.assertEqual(len(result['missing']), 337)
