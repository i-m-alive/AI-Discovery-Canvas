'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuthedUser } from '../lib/useAuthedUser';
import { apiGet, apiPost, apiDelete } from '../lib/api';
import { Icon } from '../lib/icons';
import '../shared.css';

// BA -> Projects -> Workshops (see app/routes/projects.py). Landing page
// after login — lists every Project owned by the signed-in BA. Styled
// with app/shared.css to match the canvas's own design system (same
// color tokens, button/card shapes, icon style) rather than a generic
// bootstrap-y form.
export default function ProjectsPage() {
  const router = useRouter();
  const user = useAuthedUser();
  const [projects, setProjects] = useState(null);
  const [error, setError] = useState(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [creating, setCreating] = useState(false);

  async function load() {
    try {
      const data = await apiGet('/api/projects');
      if (data && data.ok) {
        setProjects(data.projects);
        setError(null);
      } else {
        setError((data && data.error) || 'could not load projects');
      }
    } catch (err) {
      setError(err.message || 'could not load projects');
    }
  }

  useEffect(() => {
    if (user) load();
  }, [user]);

  async function handleCreate(e) {
    e.preventDefault();
    if (!name.trim()) return;
    setCreating(true);
    try {
      const data = await apiPost('/api/projects', { name: name.trim(), description: description.trim() });
      if (data && data.ok) {
        setName('');
        setDescription('');
        await load();
      } else {
        setError((data && data.error) || 'could not create project');
      }
    } catch (err) {
      setError(err.message || 'could not create project');
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(id) {
    if (!confirm('Delete this project and all its workshops? This cannot be undone.')) return;
    try {
      await apiDelete(`/api/projects/${id}`);
      await load();
    } catch (err) {
      setError(err.message || 'could not delete project');
    }
  }

  const initials = ((user && (user.name || user.email)) || '?').trim().slice(0, 1).toUpperCase();

  async function handleLogout() {
    try {
      await apiPost('/auth/logout');
    } finally {
      router.push('/login');
    }
  }

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <span className="branddot" />
        <span className="brand">AI Discovery Canvas</span>
        <div className="spacer" />
        {user && <div className="av" title={user.name || user.email}>{initials}</div>}
        {user && (
          <button onClick={handleLogout} className="btn" style={{ marginLeft: 10 }} title="Log out">
            Log out
          </button>
        )}
      </div>

      {!user ? (
        <p style={{ padding: 40, color: 'var(--muted)', fontSize: 14 }}>Loading…</p>
      ) : (
        <main className="app-main">
          <h1 className="app-h1">Your Projects</h1>
          <p className="app-sub">
            Signed in as {user.name || user.email}. Each project can hold multiple workshops.
          </p>

          {error && <p className="app-error">⚠ {error}</p>}

          <form onSubmit={handleCreate} className="app-form">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="New project name"
              className="field"
            />
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Description (optional)"
              className="field"
            />
            <button type="submit" disabled={creating || !name.trim()} className="btn solid">
              <Icon name="plus" />
              {creating ? 'Creating…' : 'Create project'}
            </button>
          </form>

          {projects === null && <p className="app-empty">Loading projects…</p>}
          {projects && projects.length === 0 && (
            <p className="app-empty">No projects yet — create your first one above.</p>
          )}
          {projects && projects.length > 0 && (
            <ul className="app-list">
              {projects.map((p) => (
                <li key={p.id} className="row-card">
                  <a href={`/projects/${p.id}`}>
                    <div className="rtitle"><Icon name="folder" />{p.name}</div>
                    {p.description && <div className="rmeta">{p.description}</div>}
                  </a>
                  <button onClick={() => handleDelete(p.id)} title="Delete project" className="rdel">
                    <Icon name="trash" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </main>
      )}
    </div>
  );
}
