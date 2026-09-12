// These mirror the pydantic models in backend/app/models.py.
// If you change one side, change the other.

export type Verdict = "matched" | "low_confidence" | "not_found" | "error";

export type JobStatus =
  | "pending"
  | "running"
  | "completed"
  | "cancelled"
  | "failed";

export interface AuthStatus {
  youtube_connected: boolean;
  youtube_detail: string;
  spotify_connected: boolean;
  spotify_detail: string;
  spotify_user: string | null;
  // Whether the client keys are in .env at all. Without them the Connect
  // button can only lead to an error page, so the UI disables it instead.
  youtube_configured: boolean;
  spotify_configured: boolean;
}

export interface PlaylistSummary {
  id: string;
  title: string;
  track_count: number | null;
  is_liked_songs: boolean;
}

export interface YtTrack {
  video_id: string;
  title: string;
  artists: string[];
  album: string | null;
  duration_seconds: number | null;
}

export interface SpotifyCandidate {
  id: string;
  uri: string;
  title: string;
  artists: string[];
  album: string | null;
  duration_seconds: number | null;
  url: string | null;
  score: number;
}

export interface MatchResult {
  source: YtTrack;
  verdict: Verdict;
  best: SpotifyCandidate | null;
  score: number;
  reason: string;
  playlist_name: string;
}

export interface PlaylistProgress {
  source_id: string;
  source_title: string;
  spotify_playlist_id: string | null;
  spotify_playlist_url: string | null;
  total: number;
  processed: number;
  added: number;
  skipped_duplicates: number;
  // Spotify search requests spent. The daily limit is on this, not on songs.
  searches: number;
}

export interface JobCounters {
  total: number;
  processed: number;
  matched: number;
  low_confidence: number;
  not_found: number;
  errors: number;
  added: number;
  skipped_duplicates: number;
  // Spotify search requests spent. The daily limit is on this, not on songs.
  searches: number;
}

export interface Job {
  id: string;
  status: JobStatus;
  dry_run: boolean;
  request: TransferRequest | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  error: string | null;
  // Things that went wrong without stopping the run, in plain words.
  warnings: string[];
  notice: string | null;
  // What the job is doing right now, in words. Changes even when the count does not.
  activity: string | null;
  waiting_until: string | null;
  quota_resets_at: string | null;
  counters: JobCounters;
  playlists: PlaylistProgress[];
  results: MatchResult[];
}

export interface TransferRequest {
  playlist_ids: string[];
  include_liked: boolean;
  dry_run: boolean;
}

/** The heartbeat: everything about a job except its results. Small, so the
 *  transfer page can poll it often without re-downloading a megabyte. */
export interface JobProgress {
  id: string;
  status: JobStatus;
  dry_run: boolean;
  request: TransferRequest | null;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  error: string | null;
  warnings: string[];
  notice: string | null;
  activity: string | null;
  waiting_until: string | null;
  quota_resets_at: string | null;
  counters: JobCounters;
  playlists: PlaylistProgress[];
}

/** A row in the recent-transfers list. Same as Job, minus the results. */
export interface JobSummary {
  id: string;
  status: JobStatus;
  dry_run: boolean;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  error: string | null;
  counters: JobCounters;
  playlist_titles: string[];
}
