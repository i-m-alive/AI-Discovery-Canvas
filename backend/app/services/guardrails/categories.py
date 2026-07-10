"""
Authoritative catalog of every togglable guardrail category and AI/KG
control. Mirrors the four UI groupings:

  * Identity        - Person, Email, Phone, EmployeeID
  * Organization    - Company, Client, Vendor, Team
  * Technical       - URL, Repo, APIKey, IP
  * Business        - Project, Financial, ContractID, Ticket

Plus five orthogonal AI / Knowledge Graph behavioural toggles:

  * hide_in_graph       - swap entity names for aliases in Neo4j writes
  * block_llm           - mask before any prompt is shipped to the LLM
  * redact_documents    - apply a final regex sweep to generated docs
  * mask_logs           - install a logging filter that masks per line
  * block_export        - deny the document download endpoints

A category id is *both* the key in the persisted JSONB config and the
key the masker looks up to decide whether to mask a detected span.
Keep it stable: the migration & UI both depend on it.
"""

from __future__ import annotations

from typing import Dict, List


CATEGORY_GROUPS: List[Dict] = [
    {
        'id': 'identity',
        'label': 'Identity Information',
        'items': [
            {'id': 'person',      'label': 'Person Names'},
            {'id': 'email',       'label': 'Email Addresses'},
            {'id': 'phone',       'label': 'Phone Numbers'},
            {'id': 'employee_id', 'label': 'Employee IDs'},
        ],
    },
    {
        'id': 'organization',
        'label': 'Organization Information',
        'items': [
            {'id': 'company', 'label': 'Company Names'},
            {'id': 'client',  'label': 'Client Names'},
            {'id': 'vendor',  'label': 'Vendor Names'},
            {'id': 'team',    'label': 'Internal Team Names'},
        ],
    },
    {
        'id': 'technical',
        'label': 'Technical Information',
        'items': [
            {'id': 'url',     'label': 'URLs / Domains'},
            {'id': 'repo',    'label': 'Repository Names'},
            {'id': 'api_key', 'label': 'API Keys / Tokens'},
            {'id': 'ip',      'label': 'IP Addresses'},
        ],
    },
    {
        'id': 'business',
        'label': 'Business Information',
        'items': [
            {'id': 'project',     'label': 'Project Names'},
            {'id': 'financial',   'label': 'Financial Information'},
            {'id': 'contract_id', 'label': 'Contract IDs'},
            {'id': 'ticket',      'label': 'Ticket Numbers'},
        ],
    },
]

# Flat list of every category id.
CATEGORIES: List[str] = [
    item['id']
    for group in CATEGORY_GROUPS
    for item in group['items']
]


# AI / Knowledge Graph behavioural toggles. Surfaced as a separate
# section in the UI; persisted under config["controls"].
AI_KG_CONTROLS: List[Dict] = [
    {
        'id': 'hide_in_graph',
        'label': 'Hide entities in Knowledge Graph',
        'description': 'Replace person / company / project nodes with stable aliases (Person_1, Company_2).',
    },
    {
        'id': 'block_llm',
        'label': 'Prevent sensitive data from reaching LLMs',
        'description': 'Mask every prompt before it leaves the backend. The single most important toggle.',
    },
    {
        'id': 'redact_documents',
        'label': 'Redact generated documents',
        'description': 'Apply a final regex sweep on FRD / Technical / SOP HTML before persisting or download.',
    },
    {
        'id': 'mask_logs',
        'label': 'Mask workflow logs & history',
        'description': 'Scrub the same patterns from log records so operator dashboards never leak PII.',
    },
    {
        'id': 'block_export',
        'label': 'Prevent export / download of sensitive content',
        'description': 'Deny the download endpoints when this scope is active (returns 403 with a friendly message).',
    },
]


def default_categories() -> Dict[str, bool]:
    """Empty (all-off) toggle map. Used as the base for the Custom mode."""
    return {c: False for c in CATEGORIES}


def default_controls() -> Dict[str, bool]:
    """Sensible safe defaults: redact, mask logs and block LLM are on; the
    two graph / export switches default off so existing flows still work
    until an admin opts in."""
    return {
        'hide_in_graph':     False,
        'block_llm':         True,
        'redact_documents':  True,
        'mask_logs':         True,
        'block_export':      False,
    }
