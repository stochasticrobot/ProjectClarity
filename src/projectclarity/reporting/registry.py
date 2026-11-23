"""Route saved report intent to an implemented builder without choosing a new format.

The registry owns type-specific construction and export. Shared execution still
owns receipts, budgets, cancellation and batch recovery. Unimplemented report
formats stop explicitly before research; the returned evidence never chooses a
replacement format. The legacy narrative path is separate historical behavior.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ReportType:
    """Implementation identity for one supported format, loaded without model calls.

    Args:
        name: Persisted report_type key.
        presentation: Browser rendering identifier.
        builder: Class using the shared bounded batch execution interface.
        prompts: Prompt filenames retained in the report manifest.
    Returns:
        Immutable routing record; constructing it does not run the builder.
    """
    name: str
    presentation: str
    builder: type
    prompts: tuple[str, ...]


def report_type(name):
    """Resolve an implemented format or fail without a substitute report.

    Args:
        name: Saved report type; currently directory or entity_profile.
    Returns:
        ReportType holding the real implementation. Known future types and unknown
        names raise RuntimeError with a safe explanation before any report call.
        Imports are lazy to avoid circular references in shared validation code.
    """
    from projectclarity.reporting.directory.builder import ItemReportBuilder
    from projectclarity.reporting.entity_profile.builder import EntityProfileBuilder
    types = {
        'directory': ReportType('directory', 'items', ItemReportBuilder,
                                ('prepare_report.txt', 'prepare_report_batch.txt')),
        'entity_profile': ReportType('entity_profile', 'entity_profile', EntityProfileBuilder,
                                     ('prepare_entity_profile_batch.txt',)),
    }
    if name not in types:
        raise RuntimeError('The requested report type (' + str(name) + ') is not implemented yet. '
                           'Available types are list/directory and entity profile. No substitute report was selected.')
    return types[name]


def export_csv(report):
    """Choose the export matching an already validated artifact's saved format.

    Args:
        report: Saved report artifact with its presentation and evidence mapping.
    Returns:
        Spreadsheet-safe CSV text. Old artifacts retain the directory/legacy exporter;
        profiles use section/finding rows. No model requests or file writes occur.
    """
    if report.get('presentation') == 'entity_profile':
        from projectclarity.reporting.entity_profile.export import profile_csv
        return profile_csv(report)
    from projectclarity.reporting.report_generation import report_csv
    return report_csv(report)
