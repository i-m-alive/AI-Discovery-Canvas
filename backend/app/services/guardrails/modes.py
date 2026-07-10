"""
The four privacy presets and the expansion rules that turn a preset
into a concrete (categories, controls) pair.

  * OPEN     - no masking. The pipeline behaves exactly as it did
               before this feature shipped.
  * STANDARD - masks emails, phone numbers, employee ids. Safe default
               for most workspaces (catches the obvious PII without
               touching anything that risks degrading document quality).
  * STRICT   - masks every identity, organization and project entity.
               Knowledge graph becomes anonymised (Person_1, Company_1).
  * CUSTOM   - whatever the user explicitly toggled.

Presets are pure functions: a given Mode always expands to the same
configuration. The Custom mode is what the persisted `categories` map
is for - the resolver leaves it untouched.
"""

from __future__ import annotations

from enum   import Enum
from typing import Dict

from app.services.guardrails.categories import default_categories, default_controls


class Mode(str, Enum):
    OPEN     = 'open'
    STANDARD = 'standard'
    STRICT   = 'strict'
    CUSTOM   = 'custom'


def _flags(*on: str) -> Dict[str, bool]:
    flags = default_categories()
    for c in on:
        if c in flags:
            flags[c] = True
    return flags


MODE_PRESETS: Dict[Mode, Dict[str, bool]] = {
    Mode.OPEN:     _flags(),
    Mode.STANDARD: _flags('email', 'phone', 'employee_id'),
    Mode.STRICT:   _flags(
        'person', 'company', 'email', 'url', 'employee_id',
        'contract_id', 'project', 'client', 'vendor', 'team',
        'repo', 'api_key', 'ip', 'phone', 'financial', 'ticket',
    ),
}


def mode_to_config(mode: Mode | str, custom_categories: Dict[str, bool] | None = None,
                   controls: Dict[str, bool] | None = None) -> Dict:
    """Expand a Mode (+ optional custom toggles) into a single config dict.

    The shape returned here is what `EffectiveConfig` is built from:

        {
          "mode":       "<one of the Mode values>",
          "categories": { "<category_id>": bool, ... },   # every id present
          "controls":   { "<control_id>":  bool, ... },   # every control id present
        }
    """
    try:
        m = Mode(mode) if not isinstance(mode, Mode) else mode
    except ValueError:
        m = Mode.STANDARD

    if m is Mode.CUSTOM:
        cats = default_categories()
        for k, v in (custom_categories or {}).items():
            if k in cats:
                cats[k] = bool(v)
    else:
        cats = dict(MODE_PRESETS[m])

    ctrls = default_controls()
    if m is Mode.OPEN:
        # Open mode is literally "do nothing" - turn off the masking
        # controls too so the pipeline behaves as if this feature wasn't
        # installed. Export & graph toggles stay at their defaults.
        ctrls['block_llm']        = False
        ctrls['redact_documents'] = False
        ctrls['mask_logs']        = False

    for k, v in (controls or {}).items():
        if k in ctrls:
            ctrls[k] = bool(v)

    return {
        'mode':       m.value,
        'categories': cats,
        'controls':   ctrls,
    }
