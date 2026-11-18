"""Browser sessions: one page kept open across calls, so an agent can act step by step."""

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from progdata_crawler.models.options import PageOptions
from progdata_crawler.models.page import Page
from progdata_crawler.models.steps import MAX_STEPS, Step


class SessionOpenRequest(PageOptions):
    """Open `url` in a new session. The page options apply to every answer in the
    session; `steps` run right after the page has loaded."""

    url: HttpUrl


class SessionStepRequest(BaseModel):
    """Run steps in an open session and get the page back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    steps: tuple[Step, ...] = Field(default=(), max_length=MAX_STEPS)
    """Without steps, the page is returned as it is now."""


class SessionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    expires_at: float
    """Unix time when the session closes if it is not used again."""
    page: Page | None = None
    """The page after the last action. None once the session is closed."""
    closed: bool = False
