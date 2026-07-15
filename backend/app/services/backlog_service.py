"""
Product Backlog service — the Post-Workshop Epics → Features → Stories
tree (see postgres/models/backlog.py).

Write paths:
    replace_tree(workshop_id, epics)  — the 'backlog' agent's output. The
        tree is a DERIVED artifact (generated from the captured
        requirements + capability map), so regeneration REPLACES it
        wholesale — unlike `requirements`, which is captured source of
        truth and only accumulates. Sync links are wiped with the tree:
        a regenerated backlog is new content, and the next Azure DevOps
        push counts everything as pending again.
    update_epic / update_feature / update_story — the facilitator's
        inline edits before pushing to a real board (LLM output should
        get a human pass first — explicit product decision).
    delete_epic / delete_feature / delete_story — pruning.

`EPIC-NN`/`FEAT-NN`/`US-NN` ids are assigned here, per workshop,
numbered in tree order per run (replace semantics make max()-scanning
unnecessary — every run starts from 01).

All functions follow this codebase's degraded-Postgres convention:
empty/False results instead of raising when the database is away.
"""

from __future__ import annotations

from app.core.logging import log
from app.postgres import session_scope
from app.postgres.repositories import backlog as repo

STATUSES = ('draft', 'approved')


def _story_dict(row) -> dict:
    return {'id': row.id, 'story_id': row.story_id, 'text': row.text,
            'acceptance_criteria': row.acceptance_criteria or [],
            'source_req_ids': row.source_req_ids or [],
            'status': row.status,
            'created_at': int(row.created_at.timestamp())}


def _tree_dicts(epics, features, stories) -> list[dict]:
    """Assemble flat rows into the nested epic→feature→story shape the
    UI renders."""
    stories_by_feature: dict[int, list[dict]] = {}
    for st in stories:
        stories_by_feature.setdefault(st.feature_row_id, []).append(_story_dict(st))
    features_by_epic: dict[int, list[dict]] = {}
    for f in features:
        features_by_epic.setdefault(f.epic_row_id, []).append({
            'id': f.id, 'feature_id': f.feature_id, 'title': f.title,
            'stories': stories_by_feature.get(f.id, []),
        })
    return [{'id': e.id, 'epic_id': e.epic_id, 'title': e.title,
             'description': e.description, 'status': e.status,
             'features': features_by_epic.get(e.id, []),
             'created_at': int(e.created_at.timestamp())}
            for e in epics]


def get_tree(workshop_id: int) -> dict:
    """{epics: [nested tree], counts: {epics, features, stories,
    stories_with_ac}} — everything the Product Backlog panel and the
    hero stat tiles need in one read."""
    with session_scope() as s:
        if s is None:
            return {'epics': [], 'counts': {'epics': 0, 'features': 0, 'stories': 0, 'stories_with_ac': 0}}
        epics = repo.list_epics(s, workshop_id)
        features = repo.list_features(s, workshop_id)
        stories = repo.list_stories(s, workshop_id)
        return {
            'epics': _tree_dicts(epics, features, stories),
            'counts': {
                'epics': len(epics),
                'features': len(features),
                'stories': len(stories),
                'stories_with_ac': sum(1 for st in stories if st.acceptance_criteria),
            },
        }


def counts(workshop_id: int) -> dict:
    return get_tree(workshop_id)['counts']


def replace_tree(workshop_id: int, epics: list[dict]) -> dict:
    """Wipe and rebuild the backlog tree from the 'backlog' agent's
    coerced output (see agent_catalog._coerce_backlog for the input
    shape — already clamped/validated, this function trusts it).
    Returns the stored tree (same shape as get_tree), or {} when
    Postgres is unavailable."""
    with session_scope() as s:
        if s is None:
            return {}
        repo.delete_tree(s, workshop_id)
        n_epic = n_feat = n_story = 0
        for e in epics:
            n_epic += 1
            epic_row = repo.create_epic(
                s, workshop_id=workshop_id, epic_id=f'EPIC-{n_epic:02d}',
                title=e['title'], description=e.get('description') or '', sort=n_epic)
            for f in e.get('features') or []:
                n_feat += 1
                feat_row = repo.create_feature(
                    s, workshop_id=workshop_id, epic_row_id=epic_row.id,
                    feature_id=f'FEAT-{n_feat:02d}', title=f['title'], sort=n_feat)
                for st in f.get('stories') or []:
                    n_story += 1
                    repo.create_story(
                        s, workshop_id=workshop_id, feature_row_id=feat_row.id,
                        story_id=f'US-{n_story:02d}', text=st['text'],
                        acceptance_criteria=st.get('acceptance_criteria') or [],
                        source_req_ids=st.get('source_req_ids') or [], sort=n_story)
    log.info('[BACKLOG] replaced tree on workshop=%s: %d epics, %d features, %d stories',
             workshop_id, n_epic, n_feat, n_story)
    return get_tree(workshop_id)


def update_epic(workshop_id: int, row_id: int, fields: dict) -> dict:
    with session_scope() as s:
        if s is None:
            return {}
        row = repo.get_epic(s, row_id)
        if row is None or row.workshop_id != workshop_id:
            return {}
        if 'title' in fields and (fields['title'] or '').strip():
            row.title = str(fields['title']).strip()[:200]
        if 'description' in fields:
            row.description = str(fields['description'] or '')[:2000]
        if 'status' in fields and str(fields['status']).lower() in STATUSES:
            row.status = str(fields['status']).lower()
        s.flush()
        return {'id': row.id, 'epic_id': row.epic_id, 'title': row.title,
                'description': row.description, 'status': row.status}


def update_feature(workshop_id: int, row_id: int, fields: dict) -> dict:
    with session_scope() as s:
        if s is None:
            return {}
        row = repo.get_feature(s, row_id)
        if row is None or row.workshop_id != workshop_id:
            return {}
        if 'title' in fields and (fields['title'] or '').strip():
            row.title = str(fields['title']).strip()[:200]
        s.flush()
        return {'id': row.id, 'feature_id': row.feature_id, 'title': row.title}


def _coerce_ac_list(raw) -> list[dict] | None:
    """Clamp a PATCHed acceptance_criteria payload to the stored shape;
    None means "field absent / unusable — leave as is"."""
    if not isinstance(raw, list):
        return None
    out = []
    for c in raw[:8]:
        if not isinstance(c, dict):
            continue
        given = str(c.get('given') or '').strip()[:300]
        when = str(c.get('when') or '').strip()[:300]
        then = str(c.get('then') or '').strip()[:300]
        if given or when or then:
            out.append({'given': given, 'when': when, 'then': then})
    return out


def update_story(workshop_id: int, row_id: int, fields: dict) -> dict:
    with session_scope() as s:
        if s is None:
            return {}
        row = repo.get_story(s, row_id)
        if row is None or row.workshop_id != workshop_id:
            return {}
        if 'text' in fields and (fields['text'] or '').strip():
            row.text = str(fields['text']).strip()[:2000]
        if 'acceptance_criteria' in fields:
            ac = _coerce_ac_list(fields['acceptance_criteria'])
            if ac is not None:
                row.acceptance_criteria = ac
        if 'status' in fields and str(fields['status']).lower() in STATUSES:
            row.status = str(fields['status']).lower()
        s.flush()
        return _story_dict(row)


def delete_item(workshop_id: int, item_type: str, row_id: int) -> bool:
    """Prune one epic/feature/story. Children cascade via FK; sync links
    for the deleted rows (and, for containers, their children) are
    removed explicitly since they're polymorphic."""
    getters = {'epic': repo.get_epic, 'feature': repo.get_feature, 'story': repo.get_story}
    getter = getters.get(item_type)
    if getter is None:
        return False
    with session_scope() as s:
        if s is None:
            return False
        row = getter(s, row_id)
        if row is None or row.workshop_id != workshop_id:
            return False
        # Collect affected ids BEFORE the cascade delete removes them.
        if item_type == 'epic':
            feats = [f for f in repo.list_features(s, workshop_id) if f.epic_row_id == row_id]
            feat_ids = [f.id for f in feats]
            story_ids = [st.id for st in repo.list_stories(s, workshop_id)
                         if st.feature_row_id in feat_ids]
            repo.delete_sync_links_for_items(s, workshop_id, 'epic', [row_id])
            repo.delete_sync_links_for_items(s, workshop_id, 'feature', feat_ids)
            repo.delete_sync_links_for_items(s, workshop_id, 'story', story_ids)
        elif item_type == 'feature':
            story_ids = [st.id for st in repo.list_stories(s, workshop_id)
                         if st.feature_row_id == row_id]
            repo.delete_sync_links_for_items(s, workshop_id, 'feature', [row_id])
            repo.delete_sync_links_for_items(s, workshop_id, 'story', story_ids)
        else:
            repo.delete_sync_links_for_items(s, workshop_id, 'story', [row_id])
        s.delete(row)
        return True
