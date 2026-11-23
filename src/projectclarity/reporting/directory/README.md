# List/directory reports

The original directory implementation now lives here. It selects cited field
contributions in bounded batches and assembles exact-label groups locally.

Read `selection.py` for the model contract and validation, `assembly.py` for identity
qualifiers/conflicts, and `builder.py` for execution through the shared dispatcher.
Old package-root imports remain compatibility exports. The directory algorithm and
CSV layout are unchanged; newly planned requests also carry the saved report choice.

See [report types](../../../../docs/REPORT_TYPES.md) for routing and profile differences.
