'use client';

import { use, useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuthedUser } from '../../lib/useAuthedUser';
import { apiGet, apiPost, apiDelete } from '../../lib/api';
import { Icon } from '../../lib/icons';
import { toggleTheme, useTheme } from '../../lib/theme';
import UserMenu from '../../lib/UserMenu';
import '../../shared.css';

// Workshops within one Project — each Workshop IS a canvas board (see
// app/routes/projects.py). Styled with app/shared.css to match the
// canvas's own design system.
export default function ProjectDetailPage({ params }) {
  const { projectId } = use(params);
  const user = useAuthedUser();
  const router = useRouter();
  const [project, setProject] = useState(null);
  const [workshops, setWorkshops] = useState(null);
  const [error, setError] = useState(null);
  const [name, setName] = useState('');
  const [creating, setCreating] = useState(false);

  async function load() {
    try {
      const [p, w] = await Promise.all([
        apiGet(`/api/projects/${projectId}`),
        apiGet(`/api/projects/${projectId}/workshops`),
      ]);
      if (p && p.ok) setProject(p.project);
      else setError((p && p.error) || 'could not load project');
      if (w && w.ok) setWorkshops(w.workshops);
      else setError((w && w.error) || 'could not load workshops');
    } catch (err) {
      setError(err.message || 'could not load project');
    }
  }

  useEffect(() => {
    if (user) load();
  }, [user, projectId]);

  async function handleCreate(e) {
    e.preventDefault();
    setCreating(true);
    try {
      const data = await apiPost(`/api/projects/${projectId}/workshops`, {
        name: name.trim() || 'Untitled Engagement',
      });
      if (data && data.ok) {
        router.push(`/canvas/${data.workshop.id}`);
      } else {
        setError((data && data.error) || 'could not create workshop');
      }
    } catch (err) {
      setError(err.message || 'could not create workshop');
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(id) {
    if (!confirm('Delete this workshop? This cannot be undone.')) return;
    try {
      await apiDelete(`/api/workshops/${id}`);
      await load();
    } catch (err) {
      setError(err.message || 'could not delete workshop');
    }
  }

  const theme = useTheme();

  return (
    <div className="app-shell">
      <div className="app-topbar">
        <span className="brandmark"><Icon name="sparkles" /></span>
        <div>
          <div className="brand">AI Discovery Canvas</div>
          <div className="brandtag">ENGAGEMENT INTELLIGENCE</div>
        </div>
        <div className="spacer" />
        <button
          className="topbar-icon-btn" onClick={toggleTheme}
          title={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
        >
          <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
        </button>
        <UserMenu user={user} avatarClassName="av" />
      </div>

      {!user || (!project && !error) ? (
        <p style={{ padding: 40, color: 'var(--muted)', fontSize: 14 }}>Loading…</p>
      ) : (
        <main className="app-main">
          <a href="/projects" className="app-crumb"><Icon name="caretL" />All Projects</a>
          <h1 className="app-h1" style={{ marginTop: 8 }}>{project ? project.name : 'Project'}</h1>
          {project && project.description && <p className="app-sub">{project.description}</p>}

          {error && <p className="app-error">⚠ {error}</p>}

          <form onSubmit={handleCreate} className="app-form">
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="New workshop name"
              className="field"
            />
            <button type="submit" disabled={creating} className="btn solid">
              <Icon name="plus" />
              {creating ? 'Creating…' : 'Create workshop'}
            </button>
          </form>

          {workshops === null && <p className="app-empty">Loading workshops…</p>}
          {workshops && workshops.length === 0 && (
            <p className="app-empty">No workshops yet — create your first one above.</p>
          )}
          {workshops && workshops.length > 0 && (
            <ul className="app-list">
              {workshops.map((w) => (
                <li key={w.id} className="row-card">
                  <a href={`/canvas/${w.id}`}>
                    <div className="rtitle"><Icon name="target" />{w.name}</div>
                    <div className="rmeta">updated {new Date(w.updated_at).toLocaleString()}</div>
                  </a>
                  <button onClick={() => handleDelete(w.id)} title="Delete workshop" className="rdel">
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
