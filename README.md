# ProjectClarity

A research CLI that searches the web, follows links, and extracts claims with
source citations. Each run prints a provisional answer and saves its evidence.

## Requirements and installation

- Python 3.12 with `pip` and `venv`, on Linux or WSL2.
- An Anthropic API key and credits for research, including local-document analysis.
- Internet access. No GPU, database server, or paid search API is required.

From the repository root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements.txt
test -e .env || cp .env.example .env
chmod 600 .env
```

Set `ANTHROPIC_API_KEY` in `.env`, or export it in your environment. Keep credentials
private. Activate `.venv` again when opening a new terminal.

## Usage

Run `python -m projectclarity` for guided setup, or provide a question directly:

```bash
python -m projectclarity \
  --question "Identify researchers and organizations studying AI security in Canada." \
  --urls source_urls.txt \
  --search --extract --review-claims --follow-links \
  --explore-before-search-depth 1 \
  --max-requests 200 --max-output-tokens 8192
```

Edit `source_urls.txt` with one starting URL per line; blank lines and `#` comments
are allowed. Omit `--urls` to start with web search alone. To research local files,
use `--document path/to/report.pdf` instead; repeat it for multiple documents.
Text, HTML and text-based PDFs are supported, without OCR or browser rendering.

Useful options:

- `--max-requests N`: total model-call allowance across all stages.
- `--max-pages N`: page/document cap, not a domain cap; default 250.
- `--explore-before-search-depth N`: link levels before prioritizing another search;
  default 2, with 1 used in the example. Depth does not limit breadth or guarantee
  that search runs before the model budget is spent.
- `--max-unproductive-depth N`: stop digging along a branch after N analyzed levels
  without qualifying progress; default 3.
- Omit `--search` to disable web search; omit `--follow-links` to disable crawling.
- `--plan-only`: generate a plan without retrieving sources; omit extraction flags.
- `--knowledge-max-requests N`: optionally capture a broader knowledge graph,
  sharing the total model-call allowance; disabled by default.

Free search providers fall back to alternatives when one fails, with an initial
five-minute cooldown. Availability is not guaranteed. Source restrictions are
configured in `config/blacklist.txt` and `config/whitelist.json`; `--whitelist-only`
restricts source acquisition to the allowlist.

For all options and their defaults:

```bash
python -m projectclarity research --help
```

### Inspect or continue a run

Replace `RUN_ID` with the identifier printed by the CLI:

```bash
python -m projectclarity status RUN_ID
python -m projectclarity answer RUN_ID --details
python -m projectclarity review RUN_ID
python -m projectclarity cancel RUN_ID
python -m projectclarity resume RUN_ID
```

`review` handles a plan awaiting clarification. `resume` continues eligible saved
work without resetting budgets. `status` and `answer` make no model calls.

Runs are saved in `.projectclarity/runs/RUN_ID/`, including `answer.md`, `answer.json`,
`session.json`, and source receipts. Use `--output-dir PATH` consistently to change
storage. Results may be partial or wrong; source support is not independent fact
verification, and budget exhaustion is not research completion.

## Run the tests

From the repository root with `.venv` active:

```bash
python -m unittest discover -s tests -t . -v
```

The normal suite uses simulated external services: no API key or model credits
are needed. Keep `-t .` to avoid package-name collisions during discovery.
