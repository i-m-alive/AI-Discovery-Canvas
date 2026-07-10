"""
Per-workflow-run alias vault.

Why this exists
---------------

A workflow run makes many LLM calls. For graph relationships to keep
working ("Person_1 worked on Project_2") every call has to map a given
literal entity to the SAME alias. The vault is what makes that happen:

    vault.alias_for('person', 'John Smith') -> 'Person_1'
    vault.alias_for('person', 'John Smith') -> 'Person_1'   # same run, same alias
    vault.alias_for('person', 'Jane Doe')   -> 'Person_2'
    # New run starts:
    vault.alias_for('person', 'John Smith') -> 'Person_1'   # counter reset

Lifecycle
---------

Vaults are keyed by run_id and held in a process-level dict. The
runtime cleans entries up when the run finishes. For the two-step
pipeline (POST /process, then POST /structure on a different run_id
that derives from the same job) the caller can adopt the previous
vault explicitly via `AliasVault.adopt(prev)` so masked tokens stay
identical across the FRD / Technical / SOP outputs the user generates
from the same merged context.

Thread-safety
-------------

A single workflow run can spin up a ThreadPoolExecutor (video frame
analysis is the main one). The vault uses a per-instance Lock around
the counter increment so concurrent allocations remain deterministic.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple


# Per-category alias prefix. Maps the masking category id (matches the
# one in regex_detectors / ner_detector) to the human-readable token
# the user sees in masked text & graph nodes.
_PREFIX = {
    'person':       'Person',
    'email':        'Email',
    'phone':        'Phone',
    'employee_id':  'EmployeeID',
    'company':      'Company',
    'client':       'Client',
    'vendor':       'Vendor',
    'team':         'Team',
    'url':          'URL',
    'repo':         'Repo',
    'api_key':      'ApiKey',
    'ip':           'IP',
    'project':      'Project',
    'financial':    'Financial',
    'contract_id':  'Contract',
    'ticket':       'Ticket',
}


def prefix_for(category: str) -> str:
    """Public lookup so the masker / knowledge_graph can format aliases
    without importing the private dict."""
    return _PREFIX.get(category, category.capitalize())


class AliasVault:
    """Stable raw -> alias mapping for a single workflow run.

    `for_run` / `release` are the only entry points callers should use.
    """

    _registry: Dict[str, 'AliasVault'] = {}
    _registry_lock = threading.Lock()

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._lock = threading.Lock()
        # category -> {raw_value -> alias}
        self._map:     Dict[str, Dict[str, str]] = {}
        # category -> next counter
        self._counter: Dict[str, int]            = {}
        # Cache the NER LLM result so a 200KB summary isn't classified
        # over and over for the dozen run_llm() calls in a single run.
        self.ner_cache: Dict[str, List[Tuple[str, str]]] = {}

    # ---- registry helpers ------------------------------------------------

    @classmethod
    def for_run(cls, run_id: str) -> 'AliasVault':
        if not run_id:
            return cls('_anon')
        with cls._registry_lock:
            v = cls._registry.get(run_id)
            if v is None:
                v = cls(run_id)
                cls._registry[run_id] = v
            return v

    @classmethod
    def release(cls, run_id: str) -> None:
        if not run_id:
            return
        with cls._registry_lock:
            cls._registry.pop(run_id, None)

    @classmethod
    def get_existing(cls, run_id: str) -> Optional['AliasVault']:
        with cls._registry_lock:
            return cls._registry.get(run_id)

    # ---- mapping ---------------------------------------------------------

    def alias_for(self, category: str, raw: str) -> str:
        """Return the stable alias for (category, raw), allocating one
        on the first call."""
        if not raw:
            return raw
        key = raw.strip()
        with self._lock:
            bucket = self._map.setdefault(category, {})
            if key in bucket:
                return bucket[key]
            n = self._counter.get(category, 0) + 1
            self._counter[category] = n
            alias = f"{prefix_for(category)}_{n}"
            bucket[key] = alias
            return alias

    def known(self, category: str, raw: str) -> Optional[str]:
        """Lookup without allocation."""
        b = self._map.get(category)
        return b.get(raw.strip()) if b and raw else None

    def adopt(self, other: 'AliasVault') -> None:
        """Copy another vault's mappings + counters into this one.

        Used when /structure starts a new run that should produce
        masked tokens consistent with the /process run that built the
        merged summary it consumes.
        """
        if not other:
            return
        with self._lock, other._lock:
            for cat, m in other._map.items():
                dest = self._map.setdefault(cat, {})
                dest.update(m)
            for cat, n in other._counter.items():
                if n > self._counter.get(cat, 0):
                    self._counter[cat] = n
            self.ner_cache.update(other.ner_cache)

    # ---- audit -----------------------------------------------------------

    def summary(self) -> Dict[str, int]:
        """Per-category count of distinct masked values. Used by audit."""
        with self._lock:
            return {cat: len(m) for cat, m in self._map.items() if m}
