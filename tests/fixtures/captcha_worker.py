"""Run production CAPTCHA/session code in a real child with invented HTTP replies.

No provider is contacted. The synthetic image is a plain pixel, not a real CAPTCHA;
its required answer exists only to check that an explicit decision reaches the same
client and that a result page is parsed after the success interstitial.
"""
import base64
from functools import partial
import json
import sys
from types import SimpleNamespace
from projectclarity.search.duckduckgo_session import search_session, exchange_challenge
from projectclarity.documents.web_acquisition import SourceError

PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOAAAAABJRU5ErkJggg==')
CHALLENGE = '''<form id="challenge-form" action="https://duckduckgo.com/anomaly.js" method="POST">
<p class="anomaly-modal__instructions">Synthetic test: select the single image.</p>
<div class="anomaly-modal__box"><input type="checkbox" name="image-check_test"><img src="/tile.png"></div>
<button name="challenge-submit" value="private-fixture-token">Submit</button></form>'''
RESULTS = '<div class="result"><a class="result__a" href="https://example.org/doc">Fixture document</a><span class="result__snippet">Candidate source.</span></div>'


class FixtureClient:
    """Retain request history and return synthetic provider pages without network I/O."""
    def __init__(self):
        """Start one client identity and request history for the child lifetime.

        Args: None.
        Returns: None; initializes the simulated acceptance state.
        """
        self.accepted = False
        self.requests = []

    def request(self, method, url, **options):
        """Respond to the real session handler's search, image and answer requests.

        Args: method: HTTP verb; url: Provider URL; options: Actual form/redirect flags.
        Returns: Response-shaped fixture. Only the supplied synthetic selection
            permits the success form and following results, preserving one client.
        """
        self.requests.append((method, url))
        if url.endswith('/tile.png'):
            body,status,mime=PIXEL,200,'image/png'
        elif '/anomaly.js' in url:
            data=options.get('data',{})
            self.accepted=data.get('image-check_test')=='on' and data.get('challenge-submit')=='private-fixture-token'
            body=('<form id="success-form" action="https://html.duckduckgo.com/html/?confirmed=1" method="GET"></form>' if self.accepted else CHALLENGE).encode()
            status,mime=200,'text/html'
        elif self.accepted:
            body,status,mime=RESULTS.encode(),200,'text/html'
        else:
            body,status,mime=CHALLENGE.encode(),202,'text/html'
        return SimpleNamespace(status_code=status,content=body,text=body.decode(errors='replace'),headers={'content-type':mime})


if __name__ == '__main__':
    request=json.loads(sys.stdin.read(10000))
    client=FixtureClient()
    try:
        result=search_session(request['query'],request['limit'],partial(exchange_challenge,request['captcha_directory']),client)
        result['fixture_requests']=len(client.requests)
        print(json.dumps(result))
    except InterruptedError:
        print(json.dumps({'code':'search_cancelled','error':'Cancelled'}));sys.exit(1)
    except SourceError as error:
        print(json.dumps({'code':error.code,'error':str(error)}));sys.exit(1)
