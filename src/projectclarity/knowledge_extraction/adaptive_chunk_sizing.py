"""Adjust each document's extraction pieces using observed model outcomes.

Dense source lists can expand into many entities, facts and repeated supporting
quotes; reasoning also consumes the model's output allowance. Live extraction
still exhausted that allowance on 750-character passages, leaving missing facts.
This bounded size heuristic gives those passages another chance at 325 characters
without repeatedly submitting the same oversized request. It does not guarantee
complete extraction: even the smallest piece can still fail.

Character count is only a rough proxy for answer size. More intelligent options
may include record-aware boundaries, predicted output size, and model-supported
reasoning/output controls. Those alternatives have not been explored or validated
here; the ladder is a pragmatic recovery rule, not an optimal sizing algorithm.

Documents begin with 3,000-character pieces. Confirmed output truncation moves
pending work down a 3,000 / 1,500 / 750 / 325 ladder. Two clean, comfortably-sized replies
permit a one-step increase. Failed pieces split downward only, retaining their
original attempts and exact source offsets. This module makes no model calls;
the document coordinator owns it so concurrent workers cannot race its decisions.
"""
from projectclarity.documents.web_acquisition import split_text

CHUNK_SIZES = (3000, 1500, 750, 325)
PROMOTION_SUCCESSES = 2
PROMOTION_OUTPUT_FRACTION = 0.60


class AdaptiveChunkSizing:
    """Keep one document's pending work and size decisions separate from workers."""

    def __init__(self, source, text):
        """Attach sizing policy to an acquired document's existing pending chunks.

        Args: source: Mutable source snapshot owned by the coordinator; text: Exact
            saved document text, used for boundaries without trimming characters.
        Returns: None; adds size metadata, preserving all original chunk IDs.
        """
        self.source, self.text = source, text
        self.level = self.generation = self.successes = 0
        self.next_index = max((c['index'] for c in source['chunks']), default=-1) + 1
        for chunk in source['chunks']:
            chunk.update(sizing_level=0, sizing_generation=0, parent_chunk_index=None)
        self._publish_policy()

    def _publish_policy(self):
        """Expose the current ladder level and promotion streak in saved progress.

        Args: None; reads this document's coordinator-owned policy.
        Returns: None; updates only safe source metadata, not prompts or results.
        """
        self.source['extraction_sizing'] = dict(chunk_characters=CHUNK_SIZES[self.level],
            consecutive_small_successes=self.successes, generation=self.generation)

    def _pieces(self, start, end, level, parent=None):
        """Create uniquely identified contiguous children without repeating text.

        Args: start: Inclusive document offset; end: Exclusive document offset;
            level: Character-size ladder index; parent: Failed attempt ID, or None
            for ordinary pending work that may later be resized in either direction.
        Returns: New chunk dictionaries covering exactly [start,end). Boundary
            preference matches normal chunking; no text is added or removed.
        """
        pieces = []
        offset = start
        for content in split_text(self.text[start:end], CHUNK_SIZES[level]):
            stop = offset + len(content)
            pieces.append(dict(index=self.next_index, start=offset, end=stop,
                pages=[p['page'] for p in self.source.get('pages', [])
                       if p['has_text'] and p['start'] < stop and p['end'] > offset],
                status='pending', facts=0, sizing_level=level,
                sizing_generation=self.generation, parent_chunk_index=parent))
            offset = stop
            self.next_index += 1
        return pieces

    def _resize_pending(self):
        """Repartition only unstarted ordinary spans at the current document size.

        Args: None; mutates this source's pending queue on the coordinator thread.
        Returns: None; attempted chunks and recovery descendants remain untouched.
            Adjacent ordinary spans merge before repartitioning, allowing promotion.
            Pending recovery pieces can shrink, never grow; lineage is updated.
            Chunk IDs are never reused, even for discarded unstarted placeholders.
        """
        pending = sorted((c for c in self.source['chunks'] if c['status'] == 'pending'
                          and c['parent_chunk_index'] is None), key=lambda c: c['start'])
        keep = [c for c in self.source['chunks'] if c not in pending]
        spans = []
        for chunk in pending:
            if spans and spans[-1][1] == chunk['start']:
                spans[-1][1] = chunk['end']
            else:
                spans.append([chunk['start'], chunk['end']])
        for start, end in spans:
            keep.extend(self._pieces(start, end, self.level))
        for chunk in list(keep):
            if chunk['status'] != 'pending' or chunk['parent_chunk_index'] is None:
                continue
            if chunk['sizing_level'] < self.level:
                children = self._pieces(chunk['start'], chunk['end'], self.level,
                                        chunk['parent_chunk_index'])
                keep.remove(chunk)
                keep.extend(children)
                parent = next(c for c in keep if c['index'] == chunk['parent_chunk_index'])
                parent['child_chunk_indices'] = [i for i in parent['child_chunk_indices'] if i != chunk['index']]
                parent['child_chunk_indices'].extend(c['index'] for c in children)
            elif chunk['sizing_level'] == self.level:
                chunk['sizing_generation'] = self.generation
        self.source['chunks'] = sorted(keep, key=lambda c: (c['start'], c['index']))

    def _change_level(self, level):
        """Reset promotion evidence and resize unstarted work after a level change.

        Args: level: New index within the approved character-size ladder.
        Returns: None; increments the policy generation so older in-flight successes
            cannot cause a promotion under the new size policy.
        """
        self.level = level
        self.successes = 0
        self.generation += 1
        self._resize_pending()
        self._publish_policy()

    def pending(self):
        """Choose the earliest unstarted piece, prioritizing recovery in source order.

        Args: None.
        Returns: Mutable chunk or None; the coordinator marks it extracting before
            asking again. This method never dispatches or duplicates work.
        """
        return next((c for c in self.source['chunks'] if c['status'] == 'pending'), None)

    def observe(self, chunk, receipt, stopped=False):
        """Adapt to a completed attempt without ever retrying identical source input.

        Args: chunk: Completed mutable chunk; receipt: Saved worker result including
            provider usage and confirmed truncation flag; stopped: Whether the user
            cancelled, in which case no recovery or resizing is scheduled.
        Returns: Human-readable policy event, or None. Failed parents get children
            only below their own level and never above the current document level.
            At 325 (or a smaller actual span) truncation remains explicitly failed.
            Promotion needs two clean current-generation replies below 60% output;
            missing usage, partial results and other failures never count as success.
        """
        if stopped:
            return None
        if receipt.get('output_truncated'):
            self.successes = 0
            child_level = max(self.level, chunk['sizing_level'] + 1)
            while child_level < len(CHUNK_SIZES) and chunk['end'] - chunk['start'] <= CHUNK_SIZES[child_level]:
                child_level += 1  # Do not resubmit a short tail unchanged.
            if child_level >= len(CHUNK_SIZES):
                self._change_level(len(CHUNK_SIZES) - 1)
                chunk['error'] = 'Output limit reached at the minimum chunk size; this source portion remains unresolved.'
                self._publish_policy()
                return f'Chunk {chunk["index"]+1}: minimum size reached; extraction remains unresolved.'
            self._change_level(child_level)
            children = self._pieces(chunk['start'], chunk['end'], child_level, chunk['index'])
            chunk.update(status='split', child_chunk_indices=[c['index'] for c in children])
            self.source['chunks'].extend(children)
            self.source['chunks'].sort(key=lambda c: (c['start'], c['index']))
            return f'Output limit: chunk {chunk["index"]+1} split into {len(children)} pieces at {CHUNK_SIZES[child_level]} characters; pending ordinary work resized.'
        if chunk['sizing_level'] == self.level and chunk['sizing_generation'] == self.generation:
            output = (receipt.get('provider', {}).get('usage') or {}).get('output_tokens')
            allowance = receipt.get('request', {}).get('max_tokens', 8192)
            clean = (receipt['status'] == 'checks_pass' and not receipt.get('initial_rejections')
                     and not receipt.get('entity_issues') and type(output) is int
                     and 0 <= output < allowance * PROMOTION_OUTPUT_FRACTION)
            self.successes = self.successes + 1 if clean else 0
            if self.level and self.successes >= PROMOTION_SUCCESSES:
                self._change_level(self.level - 1)
                return f'Two small successful replies: pending ordinary chunks increased to {CHUNK_SIZES[self.level]} characters.'
            self._publish_policy()
        elif receipt['status'] not in ('checks_pass', 'checks_partial'):
            self.successes = 0
            self._publish_policy()
        return None
