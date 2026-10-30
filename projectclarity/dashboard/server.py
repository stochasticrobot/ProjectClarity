"""Serve the loopback dashboard and launch ordinary production research workers.

The browser reads checked run snapshots and submits bounded research forms to
this server. A launch runs python -m projectclarity in a separate process, with
uploaded inputs and job receipts in the output root's private .dashboard folder.
Closing the browser or server does not cancel that worker. Explicit stop requests
use the application's cooperative cancellation mechanism. Host/origin checks and
a per-server CSRF token protect local mutations; this is not a public deployment.
Projection caches are short-lived, and model credentials are never sent to the UI."""
import argparse
import base64
import json
import mimetypes
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote
from uuid import uuid4

from projectclarity.dashboard.projection import list_runs, read_json, snapshot
from projectclarity.nodes.plan_research.research_planner import ResearchError
from projectclarity.provisional_answer import read_saved_answer, render_provisional_answer
from projectclarity.research_schemas import DEFAULT_MODEL
from projectclarity.run_status_commands import request_cancel, run_directory

ASSETS = Path(__file__).with_name('static')
PROJECT = Path(__file__).resolve().parents[2]
MAX_BODY = 24 * 1024 * 1024


def bounded_integer(value, low, high, label):
    """Validate resource controls without accepting booleans as numeric budgets.

    Args:
        value: Submitted JSON scalar.
        low: Inclusive minimum.
        high: Inclusive maximum.
        label: Public field label for validation feedback.
    Returns:
        Integer in range; raises ValueError before any worker is launched otherwise.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{label} must be an integer between {low} and {high}.')
    return value


class Dashboard:
    """Own local worker receipts and read-only projection caches, never model credentials."""

    def __init__(self, root, launcher=subprocess.Popen):
        """Initialize one dashboard against an explicit research-output directory.

        Args:
            self: Dashboard instance owning the run root, job receipts and cache.
            root: Directory holding ordinary production runs.
            launcher: Process factory; defaults to the real non-shell process launcher.
        Returns:
            None. Create private dashboard storage, a CSRF token and synchronization.
        """
        self.root = root.resolve()
        self.store = self.root / '.dashboard'
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.csrf = secrets.token_urlsafe(32)
        self.launcher = launcher
        self.processes = {}
        self.cache = {}
        self.lock = threading.RLock()

    def launch(self, args, directory, run_id=None):
        """Start the normal research entry point with private logs and no shell.

        Args:
            self: Dashboard instance owning the run root, job receipts and cache.
            args: Validated application-owned argument list, not shell text.
            directory: Fresh job directory containing any bounded uploads.
            run_id: Existing run only for an explicit plan-review continuation.
        Returns:
            Job receipt. Credentials remain inherited environment/backend settings;
            no credential value is read or returned. Worker survives browser closure.
        """
        job = dict(id=directory.name, created_at=time.time(), run_id=run_id, exit_code=None)
        with (directory / 'stdout.log').open('wb') as out, (directory / 'stderr.log').open('wb') as err:
            process = self.launcher([sys.executable, '-m', 'projectclarity', *args],
                cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                start_new_session=True)
        self.processes[job['id']] = process
        (directory / 'job.json').write_text(json.dumps(job))
        return job

    def start(self, payload):
        """Validate a question, source uploads and budgets before starting a fresh run.

        Args:
            self: Dashboard instance owning the run root, job receipts and cache.
            payload: JSON form values; uploads are base64 content, never server paths.
        Returns:
            New job receipt. Writes private inputs and launches the real production
            command. Invalid forms raise ValueError without contacting a model.
        """
        question = payload.get('question', '')
        if not isinstance(question, str) or not question.strip() or len(question) > 12000:
            raise ValueError('Enter a research question of 1–12,000 characters.')
        calls = bounded_integer(payload.get('max_requests', 200), 3, 1000, 'Model-call limit')
        concurrency = bounded_integer(payload.get('model_concurrency', 3), 1, 16, 'Model concurrency')
        seconds = bounded_integer(payload.get('max_seconds', 1800), 1, 7200, 'Time limit')
        depth = bounded_integer(payload.get('explore_depth', 1), 0, 10, 'Exploration depth')
        model = payload.get('model', DEFAULT_MODEL)
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,120}', model):
            raise ValueError('Invalid model identifier.')
        urls = payload.get('urls', '')
        if not isinstance(urls, str) or len(urls) > 32000:
            raise ValueError('Starting URLs are too long.')
        urls = [u.strip() for u in urls.splitlines() if u.strip()]
        if any(urlsplit(u).scheme not in {'https', 'http'} or not urlsplit(u).hostname for u in urls):
            raise ValueError('Starting URLs must be complete HTTP or HTTPS addresses.')
        uploads = payload.get('files', [])
        if not isinstance(uploads, list) or len(uploads) > 8:
            raise ValueError('Upload at most eight documents.')
        decoded = []
        for upload in uploads:
            if not isinstance(upload, dict):
                raise ValueError('Invalid document upload.')
            name = str(upload.get('name', 'document.txt'))
            suffix = Path(name).suffix.lower()
            if suffix not in {'.txt', '.md', '.html', '.htm', '.pdf'}:
                raise ValueError('Documents must be TXT, Markdown, HTML or PDF.')
            try:
                content = base64.b64decode(upload['content'], validate=True)
            except (ValueError, KeyError, TypeError):
                raise ValueError('Invalid document upload.') from None
            if len(content) > 5 * 1024 * 1024:
                raise ValueError('Each document must be at most 5 MiB.')
            decoded.append((suffix, content))
        for field in ('search', 'follow_links', 'coverage_study', 'automatic_stopping', 'estimate_fallback', 'design_answer'):
            if field in payload and not isinstance(payload[field], bool):
                raise ValueError(f'{field} must be true or false.')
        if payload.get('follow_links', True) and calls < 6:
            raise ValueError('Following links needs at least six shared model calls. Raise the limit or disable following links.')
        if not payload.get('search', True) and not urls and not decoded:
            raise ValueError('Provide a starting source or enable web search.')
        directory = self.store / uuid4().hex
        directory.mkdir(mode=0o700)
        args = ['research', '--question', question.strip(), '--extract', '--review-claims',
            '--non-interactive', '--progress-json', '--output-dir', str(self.root),
            '--max-requests', str(calls), '--model-concurrency', str(concurrency), '--max-fetch-seconds', str(seconds),
            '--explore-before-search-depth', str(depth), '--model', model]
        if payload.get('design_answer', False):
            args.append('--design-answer')
        if urls:
            path = directory / 'source_urls.txt'
            path.write_text('\n'.join(urls) + '\n')
            args += ['--urls', str(path)]
        for index, (suffix, content) in enumerate(decoded):
            path = directory / f'document-{index + 1}{suffix}'
            path.write_bytes(content)
            args += ['--document', str(path)]
        for field, flag in [('search', '--search'), ('follow_links', '--follow-links'),
                ('coverage_study', '--coverage-study'), ('automatic_stopping', '--stop-when-satisfied')]:
            if payload.get(field, field != 'automatic_stopping'):
                args.append(flag)
        if payload.get('coverage_study', True) and payload.get('estimate_fallback', True):
            args.append('--coverage-estimate-fallback')
        with self.lock:
            return self.launch(args, directory)

    def jobs(self):
        """Resolve launched jobs to real run IDs from recorded production progress.

        Args:
            self: Dashboard holding the output root and child-process handles.
        Returns:
            Job summaries, including startup failure when no run was created. Read
            only a bounded log prefix; never expose private stdout/model content.
        """
        rows = []
        with self.lock:
            for path in self.store.glob('*/job.json'):
                job = read_json(path)
                if not job:
                    continue
                process = self.processes.get(job['id'])
                if process:
                    job['exit_code'] = process.poll()
                if not job.get('run_id'):
                    try:
                        with (path.parent / 'stderr.log').open() as stream:
                            lines = stream.read(131072).splitlines()
                        for line in lines:
                            try:
                                event = json.loads(line)
                                if event.get('run_id'):
                                    job['run_id'] = event['run_id']
                                    break
                            except ValueError:
                                pass
                    except OSError:
                        pass
                job['error'] = ('Worker stopped before creating a run. Check backend credentials and input settings.'
                    if job.get('exit_code') is not None and not job.get('run_id') else None)
                path.write_text(json.dumps(job))
                rows.append(job)
        return sorted(rows, key=lambda r: r['created_at'], reverse=True)

    def review(self, run_id, payload):
        """Submit explicit answers or rejection to the existing plan-review workflow.

        Args:
            self: Dashboard instance owning the run root, job receipts and cache.
            run_id: Run whose pending review the user inspected.
            payload: Review ID, answer map and optional explicit rejection.
        Returns:
            Continuation job receipt, or ValueError if the displayed review is stale.
            Research reuses its existing budget and does not silently waive questions.
        """
        path = run_directory(self.root, run_id)
        session = read_json(path / 'session.json')
        review = session.get('pending_review') if session else None
        if not review or payload.get('review_id') != review.get('review_id'):
            raise ValueError('This plan review is no longer current. Refresh the run.')
        answers = payload.get('answers', {})
        if not isinstance(answers, dict) or any(not isinstance(v, str) or len(v) > 12000 for v in answers.values()):
            raise ValueError('Invalid clarification answers.')
        if not payload.get('reject') and (set(answers) != {q['question_id'] for q in review['questions']}
                or any(not answer.strip() for answer in answers.values())):
            raise ValueError('Answer every displayed clarification question before continuing.')
        directory = self.store / uuid4().hex
        directory.mkdir(mode=0o700)
        args = ['review', run_id, '--review-id', review['review_id'], '--output-dir', str(self.root),
                '--non-interactive', '--progress-json']
        if payload.get('reject'):
            args.append('--reject')
        else:
            answer_file = directory / 'answers.json'
            answer_file.write_text(json.dumps(answers))
            args += ['--answers', str(answer_file)]
        with self.lock:
            return self.launch(args, directory, run_id)

    def view(self, run_id):
        """Cache checked views briefly while respecting changing run snapshots.

        Args:
            self: Dashboard instance owning the run root, job receipts and cache.
            run_id: Existing run selected by the browser.
        Returns:
            Current projection; raises on unreadable artifacts rather than inventing
            fresh progress. Cache expires after two seconds even without a save.
        """
        directory = run_directory(self.root, run_id)
        stamp = (directory / 'session.json').stat().st_mtime_ns
        with self.lock:
            cached = self.cache.get(run_id)
            if cached and cached[0] == stamp and time.monotonic() - cached[1] < 2:
                return cached[2]
            value = snapshot(self.root, run_id)
            self.cache[run_id] = (stamp, time.monotonic(), value)
            if len(self.cache) > 8:
                self.cache.pop(next(iter(self.cache)))
            return value


class Handler(BaseHTTPRequestHandler):
    """Same-origin, loopback-only JSON API; no arbitrary file or command endpoints."""

    def log_message(self, format, *args):
        """Keep research URLs and request contents out of HTTP access logs.

        Args:
            self: HTTP request handler bound to the local dashboard server.
            format: Base server log template, intentionally unused.
            args: Base server arguments, intentionally unused.
        Returns:
            None; authoritative research logs remain in each run directory.
        """

    def send(self, status, body, content_type='application/json; charset=utf-8'):
        """Send non-cacheable output with restrictive browser headers.

        Args:
            self: HTTP request handler bound to the local dashboard server.
            status: HTTP status code.
            body: JSON-compatible object or already encoded bytes.
            content_type: Explicit MIME type for static files or reports.
        Returns:
            None. Writes one response; disconnected clients do not stop workers.
        """
        payload = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, write=False):
        """Reject DNS-rebinding hosts and cross-origin mutations before accessing data.

        Args:
            self: HTTP request handler bound to the local dashboard server.
            write: Require the bootstrap CSRF token and matching Origin when present.
        Returns:
            True for an authorized local request; otherwise sends a 403 response.
        """
        host = self.headers.get('Host', '')
        port = self.server.server_port
        valid = host in {f'127.0.0.1:{port}', f'localhost:{port}'}
        if write:
            origin = self.headers.get('Origin')
            valid = valid and self.headers.get('X-Clarity-CSRF') == self.server.app.csrf
            valid = valid and (origin is None or origin == 'http://' + host)
        if not valid:
            self.send(403, {'error': 'This dashboard accepts only local, same-origin requests.'})
        return valid

    def do_GET(self):
        """Serve bundled UI assets and explicit read-only research views.

        Args:
            self: Bound HTTP request handler.
        Returns:
            None. Sends data or a bounded error; never serves arbitrary run files,
            raw model traces, credentials or uploaded documents.
        """
        if not self.allowed():
            return
        path = unquote(urlsplit(self.path).path)
        app = self.server.app
        try:
            if path == '/api/bootstrap':
                return self.send(200, dict(csrf=app.csrf, model=DEFAULT_MODEL))
            if path == '/api/runs':
                return self.send(200, list_runs(app.root))
            if path == '/api/jobs':
                return self.send(200, app.jobs())
            match = re.fullmatch(r'/api/runs/([A-Za-z0-9_-]+)(/report)?', path)
            if match:
                run_id, report = match.groups()
                if report:
                    text = render_provisional_answer(read_saved_answer(app.root, run_id), include_leads=True)
                    return self.send(200, text.encode(), 'text/plain; charset=utf-8')
                return self.send(200, app.view(run_id))
            files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                     '/discoveries.js': 'discoveries.js'}
            if path in files:
                file = ASSETS / files[path]
                return self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0] + '; charset=utf-8')
            self.send(404, {'error': 'Not found.'})
        except (ResearchError, ValueError, OSError):
            self.send(409, {'error': 'Run data is unavailable or changing. Refresh to try again.'})

    def do_POST(self):
        """Handle explicit launches, stop requests and plan-review decisions.

        Args:
            self: Bound request with CSRF authorization.
        Returns:
            None. Performs only the named action; rejects oversized/non-JSON bodies
            and invalid paths without invoking a shell or arbitrary file access.
        """
        if not self.allowed(write=True):
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= MAX_BODY or self.headers.get_content_type() != 'application/json':
                raise ValueError('Submit a JSON request smaller than 24 MiB.')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object.')
            path = urlsplit(self.path).path
            if path == '/api/jobs':
                return self.send(202, self.server.app.start(payload))
            match = re.fullmatch(r'/api/runs/([A-Za-z0-9_-]+)/(stop|review)', path)
            if match:
                run_id, action = match.groups()
                if action == 'stop':
                    return self.send(200, {'message': request_cancel(self.server.app.root, run_id)})
                return self.send(202, self.server.app.review(run_id, payload))
            self.send(404, {'error': 'Not found.'})
        except (ValueError, ResearchError) as exc:
            self.send(400, {'error': str(exc)[:500]})
        except OSError:
            self.send(500, {'error': 'The local worker could not be started or its files could not be written.'})


def make_server(root, port=8765):
    """Bind a dashboard to loopback; remote access is deliberately not implicit.

    Args:
        root: Production run-output directory.
        port: TCP port; zero requests an ephemeral port for tests.
    Returns:
        Unstarted threaded HTTP server with its Dashboard attached. Port conflicts
        raise OSError. This function does not start research or open a browser.
    """
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.app = Dashboard(root)
    return server


def main():
    """Run the local dashboard until interrupted without stopping research workers.

    Args:
        None; reads --port and --output-dir from command-line arguments.
    Returns:
        None. Prints the URL and serves until Ctrl-C; does not open a browser panel.
    """
    parser = argparse.ArgumentParser(description='Local dashboard for the ProjectClarity agentic research tool.')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--output-dir', type=Path, default=Path('.projectclarity/runs'))
    args = parser.parse_args()
    server = make_server(args.output_dir, args.port)
    print(f'ProjectClarity dashboard: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
