"""Match report references to supplied IDs despite harmless case or outer spaces.

Only reference fields are changed. Evidence, prose and original provider replies
remain untouched; unknown or ambiguous references are never guessed.
"""
from copy import deepcopy


def normalize_references(result, fact_ids, requirement_ids):
    """Return a copied report/merge result with canonical supplied identifiers.

    Args:
        result: Parsed JSON-shaped report or merge response.
        fact_ids: Actual fact IDs, or local finding IDs for a merge request.
        requirement_ids: Actual required-field IDs from the research definition.
    Returns:
        Independent normalized structure. Exact IDs win; otherwise surrounding
        whitespace and case are ignored only for a unique supplied match.
        Unknown/ambiguous IDs raise descriptive ValueError. Duplicate references
        remain visible to existing validation, and nonreference values are unchanged.
    """
    def index(values):
        """Index supplied IDs without silently resolving collisions.

        Args: values: Authoritative identifiers for one reference namespace.
        Returns: Exact set and folded keys mapped to all corresponding IDs.
        """
        exact=set(values);folded={}
        for value in exact:
            folded.setdefault(value.strip().casefold(),set()).add(value)
        return exact,folded

    facts,requirements=index(fact_ids),index(requirement_ids)

    def canonical(value,namespace,field):
        """Resolve a reference only against its request's actual identifier set.

        Args: value: Parsed string ID; namespace: Exact/folded index; field: Error label.
        Returns: Canonical ID; raises for an unknown or ambiguous nonexact reference.
        """
        exact,folded=namespace
        if value in exact:return value
        matches=folded.get(value.strip().casefold(),set())
        if len(matches)!=1:
            kind='Ambiguous' if matches else 'Unknown'
            raise ValueError(f'{kind} {field} reference: {value!r}.')
        return next(iter(matches))

    def visit(value):
        """Normalize recognized reference keys, never arbitrary prose strings.

        Args: value: Copied nested JSON structure.
        Returns: None; mutates only copied reference fields.
        """
        if isinstance(value,list):
            for item in value:visit(item)
        elif isinstance(value,dict):
            for key,item in value.items():
                if key in ('fact_ids','label_fact_ids','input_ids'):
                    value[key]=[canonical(identifier,facts,key) for identifier in item]
                elif key=='fact_id':
                    value[key]=canonical(item,facts,key)
                elif key=='requirement_id':
                    value[key]=canonical(item,requirements,key)
                else:visit(item)

    copied=deepcopy(result);visit(copied)
    return copied
