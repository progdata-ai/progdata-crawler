"""Browser steps: what to do on a page before it is captured.

Steps run in order in the browser after the page has loaded. They are for using a page
the way a person would (open a menu, fill in a search form, page through results), not
for getting around a site's limits.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_STEPS = 50
DEFAULT_STEP_TIMEOUT_MS = 5_000
MAX_STEP_TIMEOUT_MS = 30_000

Selector = Annotated[str, Field(min_length=1, max_length=1_000, examples=["#search"])]
"""A CSS selector."""


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_ms: int = Field(default=DEFAULT_STEP_TIMEOUT_MS, gt=0, le=MAX_STEP_TIMEOUT_MS)
    """How long to wait for the element, or for the step to finish."""


class ClickStep(_Step):
    action: Literal["click"] = "click"
    selector: Selector


class TypeStep(_Step):
    action: Literal["type"] = "type"
    selector: Selector
    text: str = Field(max_length=10_000)
    clear: bool = True
    """Empty the field first."""


class PressStep(_Step):
    action: Literal["press"] = "press"
    key: str = Field(min_length=1, max_length=50, examples=["Enter", "ArrowDown"])
    selector: Selector | None = None
    """Element to press the key in; the focused element when unset."""


class ScrollDirection(StrEnum):
    DOWN = "down"
    UP = "up"


class ScrollStep(_Step):
    action: Literal["scroll"] = "scroll"
    direction: ScrollDirection = ScrollDirection.DOWN
    pixels: int = Field(default=800, gt=0, le=100_000)
    selector: Selector | None = None
    """Scroll this element into view instead of scrolling by pixels."""


class WaitStep(_Step):
    action: Literal["wait"] = "wait"
    selector: Selector | None = None
    """Wait until this element is visible."""
    ms: int = Field(default=0, ge=0, le=MAX_STEP_TIMEOUT_MS)
    """Or wait this long."""


class CaptureStep(_Step):
    action: Literal["capture"] = "capture"
    """Record the page as it is now; the result lists every capture."""


Step = Annotated[
    ClickStep | TypeStep | PressStep | ScrollStep | WaitStep | CaptureStep,
    Field(discriminator="action"),
]


class Capture(BaseModel):
    """The page at a `capture` step."""

    model_config = ConfigDict(frozen=True)

    step: int
    """Position of the capture step, from 1."""
    url: str
    markdown: str | None = None
    screenshot: str | None = None
    """PNG, base64-encoded, when the screenshot output was asked for."""
