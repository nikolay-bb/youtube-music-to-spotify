// Every call to the Python backend goes through here.
//
// The backend runs on 127.0.0.1:8000 and the UI on 127.0.0.1:3000, so these are
// cross-origin requests. The backend allows them explicitly (see the CORS
// settings in backend/app/main.py).
//
// Note the address is 127.0.0.1 and never "localhost". Spotify refuses to
// register a localhost redirect URI, so the whole app sticks to the IP address
// to keep the two halves on the same origin as far as the browser is concerned.
//
// Every failure comes out as an ApiError with two sentences: what went wrong,
// and what to do about it. The screens show both. No screen ever has to say
// "something went wrong" on its own.

import type {
  AuthStatus,
  Job,
  JobProgress,
  JobSummary,
  PlaylistSummary,
  TransferRequest,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

// How long to wait for the backend before giving up. Reading a big library
// from YouTube can take half a minute, so this is generous on purpose.
const REQUEST_TIMEOUT_MS = 60_000;

/** Thrown for any failed call.
 *
 *  `status` is the HTTP status, or 0 when the backend never answered at all
 *  (not running, or the request timed out). `hint` is the next thing to try,
 *  when there is one worth saying. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly hint: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** What a screen needs to show for a failure: the reason, the next step, and
 *  the status so it can offer the right button (Try again, Go to Connect). */
export interface Failure {
  message: string;
  hint: string | null;
  status: number | null;
}

/** Turn anything a call threw into a Failure the screen can show. */
export function describeError(caught: unknown, fallback: string): Failure {
  if (caught instanceof ApiError) {
    return { message: caught.message, hint: caught.hint, status: caught.status };
  }
  if (caught instanceof Error && caught.message) {
    return { message: `${fallback} (${caught.message})`, hint: null, status: null };
  }
  return { message: fallback, hint: null, status: null };
}

/** True when the fix is to log in to a service again. Screens use this to
 *  offer a "Go to Connect" button next to the message. */
export function isLoginProblem(failure: Failure): boolean {
  return (
    failure.status === 401 ||
    failure.status === 403 ||
    mentionsLogin(failure.message)
  );
}

/** The backend's own wording for "log in again", in any of its forms. */
export function mentionsLogin(text: string): boolean {
  return /connect (spotify|youtube)|log ?in again|no longer accepts|login has expired|not connected/i.test(
    text,
  );
}

/** FastAPI puts the reason in `detail`. Our routes send a sentence; a
 *  validation failure the backend did not translate sends an array. */
function readDetail(body: unknown): string | null {
  if (!body || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail) && detail.length > 0) {
    const parts = detail.map((item) => {
      if (item && typeof item === "object") {
        const { loc, msg } = item as { loc?: unknown[]; msg?: string };
        const where = Array.isArray(loc)
          ? loc.filter((part) => part !== "body").join(".")
          : "";
        return where ? `${where}: ${msg ?? "invalid"}` : (msg ?? "invalid");
      }
      return String(item);
    });
    return `The request was not in the shape the backend expects (${parts.join("; ")}).`;
  }
  return null;
}

/** For replies that carry no message of their own: what the status means here. */
function fallbackMessage(status: number): string {
  switch (status) {
    case 400:
      return "The backend refused the request as invalid.";
    case 401:
      return "That service no longer accepts the saved login.";
    case 403:
      return "That service refused access to this account.";
    case 404:
      return "The backend has no record of that.";
    case 409:
      return "That job is still busy.";
    case 422:
      return "The request was not in the shape the backend expects.";
    case 429:
      return "Too many requests in a short time.";
    case 500:
      return "The backend crashed while handling this.";
    case 502:
    case 503:
    case 504:
      return "The backend could not reach the music service.";
    default:
      return `The backend answered with HTTP ${status} and no explanation.`;
  }
}

/** The next thing to try, by status. Shown in smaller text under the reason. */
function hintFor(status: number): string | null {
  switch (status) {
    case 401:
    case 403:
      return "Go back to the Connect screen and log in to that service again.";
    case 404:
      return "It may have been deleted from backend/data/jobs. Go back to the home screen and pick another.";
    case 409:
      return "Wait for it to finish, or press Stop first.";
    case 429:
      return "Wait a minute, then try again.";
    case 500:
      return "The full error is in the terminal running npm run dev.";
    case 502:
    case 503:
    case 504:
      return "Check this computer's internet connection, then try again.";
    default:
      return null;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    if (controller.signal.aborted) {
      throw new ApiError(
        `The backend did not answer within ${REQUEST_TIMEOUT_MS / 1000} seconds.`,
        0,
        "It may be stuck reading a very large library, or a music service may be slow. Look at the terminal running npm run dev, then try again.",
      );
    }
    // fetch only rejects when the server could not be reached at all.
    throw new ApiError(
      `Cannot reach the backend at ${API_BASE}.`,
      0,
      'Is it running? Start it with "npm run dev" in the project root, then try again.',
    );
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      readDetail(body) ?? fallbackMessage(response.status),
      response.status,
      hintFor(response.status),
    );
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(
      "The backend answered with something that is not JSON.",
      response.status,
      "Something else may be listening on port 8000. Stop it, then start the app again with npm run dev.",
    );
  }
}

export const getAuthStatus = () => request<AuthStatus>("/api/auth/status");

export const disconnectSpotify = () =>
  request<{ ok: boolean }>("/api/auth/spotify/logout", { method: "POST" });

export const disconnectYoutube = () =>
  request<{ ok: boolean }>("/api/auth/youtube/logout", { method: "POST" });

export const getPlaylists = () =>
  request<PlaylistSummary[]>("/api/library/playlists");

export const startTransfer = (body: TransferRequest) =>
  request<Job>("/api/transfer", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const getJob = (jobId: string) => request<Job>(`/api/transfer/${jobId}`);

/** The light heartbeat: progress, counters and notices, without the results. */
export const getJobProgress = (jobId: string) =>
  request<JobProgress>(`/api/transfer/${jobId}/status`);

/** Past transfers, newest first. Summaries only, so this stays small. */
export const listJobs = () => request<JobSummary[]>("/api/transfer");

export const cancelJob = (jobId: string) =>
  request<{ ok: boolean }>(`/api/transfer/${jobId}/cancel`, { method: "POST" });

/** Write a dry run's matches to Spotify without searching for them again. */
export const applyJob = (jobId: string) =>
  request<Job>(`/api/transfer/${jobId}/apply`, { method: "POST" });

/** Carry on a stopped job, keeping every result it already had. */
export const resumeJob = (jobId: string) =>
  request<Job>(`/api/transfer/${jobId}/resume`, { method: "POST" });

/** Logins are browser redirects, not fetches, so they need plain URLs. */
export const spotifyLoginUrl = () => `${API_BASE}/api/auth/spotify/login`;
export const youtubeLoginUrl = () => `${API_BASE}/api/auth/youtube/login`;

export const reportUrl = (jobId: string, onlyProblems: boolean) =>
  `${API_BASE}/api/transfer/${jobId}/report.csv?only_problems=${onlyProblems}`;

/** Turn 203 into "3:23". Used all over the results table. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "—";
  const minutes = Math.floor(seconds / 60);
  const rest = Math.floor(seconds % 60);
  return `${minutes}:${rest.toString().padStart(2, "0")}`;
}
