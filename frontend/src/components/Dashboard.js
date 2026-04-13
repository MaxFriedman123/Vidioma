import React, { useEffect, useState } from 'react';
import axios from 'axios';
import { useAuth } from '../AuthContext';

const API_BASE_URL = (process.env.REACT_APP_API_URL || 'http://localhost:5000').replace(/\/$/, '');

function timeAgo(dateString) {
  const now = new Date();
  const then = new Date(dateString);
  const seconds = Math.floor((now - then) / 1000);

  if (seconds < 60) return 'Just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  const weeks = Math.floor(days / 7);
  if (weeks < 4) return `${weeks}w ago`;
  return then.toLocaleDateString();
}

export default function Dashboard({ onSelectVideo }) {
  const { accessToken } = useAuth();
  const [progress, setProgress] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!accessToken) {
      console.log('Dashboard: No accessToken, skipping fetch');
      return;
    }

    console.log('Dashboard: Fetching progress with token:', accessToken.substring(0, 20) + '...');
    axios
      .get(`${API_BASE_URL}/api/progress`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      })
      .then((resp) => {
        console.log('Dashboard fetch success:', resp.data);
        setProgress(resp.data.progress || []);
      })
      .catch((err) => {
        console.error('Dashboard fetch failed:', err.response?.status, err.response?.data, err.message);
        setError('Failed to load your progress.');
      })
      .finally(() => setLoading(false));
  }, [accessToken]);

  if (loading) {
    return (
      <div className="dashboard">
        <h2 className="dashboard-title">My Dashboard</h2>
        <p className="dashboard-loading">Loading your videos...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="dashboard">
        <h2 className="dashboard-title">My Dashboard</h2>
        <p className="dashboard-error">{error}</p>
      </div>
    );
  }

  if (progress.length === 0) {
    return (
      <div className="dashboard">
        <h2 className="dashboard-title">My Dashboard</h2>
        <p className="dashboard-empty">
          No saved videos yet. Start translating a video and your progress will appear here!
        </p>
      </div>
    );
  }

  return (
    <div className="dashboard">
      <h2 className="dashboard-title">My Dashboard</h2>
      <div className="dashboard-grid">
        {progress.map((item) => {
          const video = item.videos || {};
          const pct =
            item.total_lines > 0
              ? Math.round((item.current_line_index / item.total_lines) * 100)
              : 0;

          return (
            <div
              key={item.id}
              className="dashboard-card"
              onClick={() =>
                onSelectVideo({
                  youtubeId: video.youtube_id,
                  transcriptLanguage: item.transcript_language,
                  translationLanguage: item.translation_language,
                  startLine: item.current_line_index,
                })
              }
            >
              <img
                src={
                  video.thumbnail_url ||
                  `https://img.youtube.com/vi/${video.youtube_id}/hqdefault.jpg`
                }
                alt={video.title || 'Video thumbnail'}
                className="dashboard-thumb"
              />
              <div className="dashboard-card-body">
                <h3 className="dashboard-card-title">
                  {video.title || video.youtube_id}
                </h3>
                <p className="dashboard-card-langs">
                  {item.transcript_language.toUpperCase()} &rarr;{' '}
                  {item.translation_language.toUpperCase()}
                </p>
                <div className="dashboard-progress-bar">
                  <div
                    className="dashboard-progress-fill"
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <p className="dashboard-card-stat">
                  {item.current_line_index} / {item.total_lines} lines ({pct}%)
                </p>
                <p className="dashboard-card-time">
                  Last watched: {timeAgo(item.last_accessed_at)}
                </p>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
