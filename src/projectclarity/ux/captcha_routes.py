"""Expose session-bound human CAPTCHA controls without provider cookies or tokens.

The loopback application's existing origin checks protect these routes. Images are
cached raster bytes from the live worker; POST records only the human's decision.
Opening or refreshing a page never submits an answer or starts a search.
"""
from typing import Literal
from uuid import UUID
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class CaptchaDecision(BaseModel):
    """Bind an explicit human action to the exact currently displayed puzzle."""
    model_config = ConfigDict(extra='forbid', strict=True)
    challenge_id: str
    action: Literal['submit', 'skip', 'cancel', 'activity']
    tiles: list[StrictInt] = Field(default_factory=list, max_length=16)


def register_captcha_routes(app, sessions):
    """Attach human-only image/decision routes to the existing local web application.

    Args: app: FastAPI application with loopback/origin middleware; sessions: owner.
    Returns: None; registers routes only, without creating or answering a challenge.
    """
    @app.get('/api/setup/{sid}/captcha/{challenge_id}/tiles/{index}')
    def image(sid: UUID, challenge_id: UUID, index: int):
        """Return one tile for this session's current challenge, never remote HTML.

        Args: sid: Session UUID; challenge_id: displayed puzzle UUID; index: tile index.
        Returns: Raster response, or404 for unknown sessions, stale puzzles or indices.
        """
        sessions.read(str(sid))
        data, mime = sessions.captchas.image(str(sid), str(challenge_id), index)
        return Response(data, media_type=mime)

    @app.post('/api/setup/{sid}/captcha')
    def decide(sid: UUID, decision: CaptchaDecision):
        """Accept an answer, skip, cancellation or human inactivity-timer reset.

        Args: sid: Session UUID; decision: one-use ID, action and human tile selections.
        Returns: Current session snapshot. Invalid/stale requests fail without waking
            the worker; activity only extends a live challenge's deadline, and
            cancel also sets the normal research stop event.
        """
        sessions.read(str(sid))
        sessions.captchas.decide(str(sid), decision.challenge_id, decision.action, decision.tiles)
        if decision.action == 'cancel':
            sessions.stop_run(str(sid))
        return sessions.read(str(sid))
