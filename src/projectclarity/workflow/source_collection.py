"""Connect the optional search step to document processing for the current web workflow.

The first research round starts with searches. This file passes the resulting paths
to the independent document processor and combines their progress for the existing
screen. Search has its own code and saved inputs; the document pool receives only
paths, titles and optional origin notes. Later rounds use supply_links to bypass searching. Other callers can supply
their own paths directly to DocumentPipeline.
"""
from datetime import datetime, timezone

from projectclarity.documents.document_pipeline import DocumentPipeline
from projectclarity.search.search_execution import SearchExecution
from projectclarity.search.web_search import search_web, search_wikipedia
from projectclarity.knowledge_extraction.knowledge_extraction import save


class SourceCollection:
    """Coordinate the web app's search and document steps without merging their jobs."""

    def __init__(self, root, state, publish, caller, stop, search=search_web,
                 reader=None, fallback=search_wikipedia):
        """Prepare separate search and document workers for one web research attempt.

        Args:
            root: Owning session directory for this attempt's private files.
            state: Saved question, requirements and prepared query list.
            publish: Callback persisting public progress for the existing screen.
            caller: AI transport used only by document extraction.
            stop: Shared cancellation event.
            search: Search callable, replaceable for offline checks.
            reader: Optional document reader implementing fetch(path).
            fallback: Alternate search callable used by search's existing policy.
        Returns:
            None. Initializes the document and search input files; no external calls.
            root and run expose the attempt location and public combined progress.
        """
        self.documents = DocumentPipeline(root, state, publish, caller, stop, reader=reader)
        self.root, self.run = self.documents.root, self.documents.run
        self.state, self.publish, self.stop = state, publish, stop
        self.search_ready = False
        self.links_only = False
        self.search = SearchExecution(self.root, state, self.run, publish, stop, search, fallback)

    def supply_links(self, paths):
        """Use explicitly selected destinations instead of running search.

        Args: paths: Saved selected-link entries accepted by DocumentPool.
        Returns: None; replaces unstarted document inputs and marks this round as
            search-free. The ordinary reader, extraction limits and stop checks apply.
        """
        self.documents.set_paths(paths)
        self.run.update(stage='downloading', searches=[])
        self.links_only = True
        self.search_ready = True

    def discover(self, graph_state):
        """Hand candidate paths from search to the independent document pool.

        Args:
            graph_state: Owning session identity supplied by LangGraph.
        Returns:
            Unchanged graph identity after saving search results and setting the
            document input list. No result page is downloaded in this node.
            Search cancellation or file failures propagate to execute.
        """
        paths = self.search.execute()
        self.documents.set_paths(paths)
        return graph_state

    def process_documents(self, graph_state):
        """Read the supplied pool using the same entry point available without search.

        Args:
            graph_state: Owning session identity supplied by LangGraph.
        Returns:
            Unchanged identity after document processing records its own outcome.
            The document processor never sees search terms or chooses a provider.
        """
        self.documents.execute()
        self.finish()
        return graph_state

    def run_search(self, graph_state):
        """Run search and retain a stop/failure without entering document processing.

        Args:
            graph_state: Session identity supplied by either research graph.
        Returns:
            Unchanged identity. search_ready becomes true only after candidate paths
            are handed over successfully. Interrupted or failed work is finalized.
        """
        try:
            self.discover(graph_state)
            self.search_ready = True
        except Exception as error:
            self.finish(error)
        return graph_state

    def finish(self, error=None):
        """Save the combined web outcome without changing document-only success rules.

        Args:
            error: Optional exception that prevented search from handing over paths.
                InterruptedError represents cancellation, not a failed source.
        Returns:
            None. Saves the web summary and publishes final progress. A successful
            document pool does not conceal an incomplete search. Document-only
            results remain separately saved in document-summary.json.
        """
        if error is not None:
            self.run['status'] = 'cancelled' if isinstance(error, InterruptedError) else 'failed'
            if not isinstance(error, InterruptedError):
                self.run['error'] = type(error).__name__+': '+str(error)[:500]
        elif self.run['status'] == 'completed' and not self.links_only and not self.search.complete:
            self.run['status'] = 'partial'
        self.run.update(stage='finished', finished_at=datetime.now(timezone.utc).isoformat())
        self.state['status'] = 'run_' + self.run['status']
        if self.state.get('auto_select') and not self.stop.is_set() and self.run['status'] in ('completed', 'partial'):
            self.state['status'] = 'run_running'
        save(self.root/'summary.json', self.run, '')
        self.documents.event('Source collection '+self.run['status']+'.')

    def execute(self):
        """Run the search-to-document connection independently of later research stages.

        Args: None.
        Returns:
            None. Invokes the same search and document methods used by the full
            research graph. Stage outcomes are saved by those methods. No extra
            searches, automatic retries or downstream evidence selection occur.
        """
        from projectclarity.workflow.research_graph import build_source_collection_graph
        build_source_collection_graph(self).invoke({'session_id': self.state['session_id']})
