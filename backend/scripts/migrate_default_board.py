"""
One-time migration: the single hardcoded 'default' board -> a real
Project + Workshop in Postgres.

Run once, from backend/:

    .venv/bin/python3 scripts/migrate_default_board.py

What it does:
  1. Reads the existing flat-file data (data/canvas_boards.json['default'],
     data/prepare_docs.json['default'], data/generated_docs.json['default']).
  2. Picks the owner: the single existing `users` row (there should be
     exactly one — whoever has logged in so far). Bails with a clear
     message if there are zero or more than one, rather than guessing.
  3. Creates a Project ("My First Project") + one Workshop (named from
     the board's own `board_name` field) with board_data = the existing
     board blob (verbatim — every node/edge/artifact/docId reference
     keeps working unchanged).
  4. Copies each prepare_docs/generated_docs row into Postgres, preserving
     its original doc_id, name, and timestamp, and copies its object_store
     blob to the new workshop-scoped key (object_store is content-
     addressed, so this only duplicates a pointer file, not the bytes).
  5. Renames the three JSON files to '<name>.migrated.bak' — never
     deletes — so this is fully reversible if anything looks wrong.

Safe to run multiple times: it's a no-op (with a clear message) once the
JSON files have already been renamed away.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from app.core.logging import log  # noqa: E402
from app.postgres import is_configured, session_scope  # noqa: E402
from app.postgres.models.generated_doc import GeneratedDoc  # noqa: E402
from app.postgres.models.prepare_doc import PrepareDoc  # noqa: E402
from app.postgres.repositories import projects as projects_repo  # noqa: E402
from app.postgres.repositories import workshops as workshops_repo  # noqa: E402
from app.services import object_store  # noqa: E402

_DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
_BOARD_ID = 'default'


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def _epoch_to_dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def main() -> int:
    if not is_configured():
        print('Postgres is not configured (POSTGRES_HOST/POSTGRES_USER blank in .env) — aborting.')
        return 1

    boards_file = _DATA_DIR / 'canvas_boards.json'
    prepare_file = _DATA_DIR / 'prepare_docs.json'
    generated_file = _DATA_DIR / 'generated_docs.json'

    if not boards_file.exists():
        print(f'{boards_file} not found — nothing to migrate (already migrated, or a fresh install).')
        return 0

    boards = _load_json(boards_file)
    board = boards.get(_BOARD_ID)
    if not board:
        print(f"No '{_BOARD_ID}' board in {boards_file} — nothing to migrate.")
        return 0

    prepare_docs_all = _load_json(prepare_file).get(_BOARD_ID, [])
    generated_docs_all = _load_json(generated_file).get(_BOARD_ID, [])

    with session_scope() as s:
        if s is None:
            print('Could not open a Postgres session — is the DB reachable? Aborting.')
            return 1

        from sqlalchemy import select
        from app.postgres.models.user import User
        all_users = list(s.execute(select(User)).scalars())
        if not all_users:
            print('No users found in Postgres yet — log into the app once first '
                  '(so there is an owner to assign this board to), then re-run this script.')
            return 1
        if len(all_users) > 1:
            names = ', '.join(f'{u.email} (id={u.id})' for u in all_users)
            print(f'More than one user exists ({names}) — pick one manually and adapt this '
                  'script rather than guessing which should own the migrated board.')
            return 1
        owner = all_users[0]
        print(f'Owner: {owner.email} (id={owner.id})')

        project = projects_repo.create(
            s, name='My First Project', owner_user_id=owner.id,
            description='Auto-created by migrate_default_board.py from the pre-existing single board.',
            created_by_name=owner.name, created_by_email=owner.email,
        )
        workshop = workshops_repo.create(
            s, project_id=project.id, name=(board.get('board_name') or 'Untitled Engagement'),
            created_by_name=owner.name, created_by_email=owner.email, board_data=board,
        )
        print(f'Created project id={project.id} "{project.name}", '
              f'workshop id={workshop.id} "{workshop.name}"')

        for rec in prepare_docs_all:
            old_key = f"prepare_docs/{_BOARD_ID}/{rec['doc_id']}.txt"
            data = object_store.get_bytes(old_key)
            if data is None:
                log.warning('[MIGRATE] prepare_doc %s: object_store blob missing at %s — skipping',
                           rec['doc_id'], old_key)
                continue
            new_key = f"prepare_docs/{workshop.id}/{rec['doc_id']}.txt"
            object_store.put_bytes(new_key, data, content_type='text/plain')
            s.add(PrepareDoc(
                doc_id=rec['doc_id'], workshop_id=workshop.id, name=rec['name'],
                chars=rec.get('chars', 0), uploaded_by=rec.get('uploaded_by', ''),
                uploaded_at=_epoch_to_dt(rec['uploaded_at']),
            ))
            print(f"  prepare_doc: {rec['name']} ({rec['doc_id']})")

        for rec in generated_docs_all:
            old_key = f"generated_docs/{_BOARD_ID}/{rec['doc_id']}.html"
            data = object_store.get_bytes(old_key)
            if data is None:
                log.warning('[MIGRATE] generated_doc %s: object_store blob missing at %s — skipping',
                           rec['doc_id'], old_key)
                continue
            new_key = f"generated_docs/{workshop.id}/{rec['doc_id']}.html"
            object_store.put_bytes(new_key, data, content_type='text/html')
            s.add(GeneratedDoc(
                doc_id=rec['doc_id'], workshop_id=workshop.id, name=rec['name'],
                agent_id=rec.get('agent_id', ''), chars=rec.get('chars', 0),
                created_at=_epoch_to_dt(rec['created_at']),
            ))
            print(f"  generated_doc: {rec['name']} ({rec['doc_id']})")

    for f in (boards_file, prepare_file, generated_file):
        if f.exists():
            f.rename(f.with_suffix(f.suffix + '.migrated.bak'))
            print(f'Renamed {f.name} -> {f.name}.migrated.bak')

    print('\nMigration complete.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
