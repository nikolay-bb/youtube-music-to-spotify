"use client";

// Screen 1: connect both accounts.
//
// For anyone but the author, this screen *is* the product. Getting a Spotify app
// and a Google client made is the only hard part of using this tool, so the page
// is written as a guide that checks itself rather than a pair of status lights.

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  Button,
  ButtonLink,
  Card,
  CopyField,
  ErrorNote,
  InfoNote,
  Skeleton,
  StatusDot,
  Step,
} from "@/components/ui";
import {
  describeError,
  disconnectSpotify,
  disconnectYoutube,
  getAuthStatus,
  listJobs,
  spotifyLoginUrl,
  youtubeLoginUrl,
  type Failure,
} from "@/lib/api";
import type { AuthStatus, JobSummary } from "@/lib/types";

const SPOTIFY_REDIRECT = "http://127.0.0.1:8000/api/auth/spotify/callback";
const YOUTUBE_REDIRECT = "http://127.0.0.1:8000/api/auth/youtube/callback";

type Service = "spotify" | "youtube";

export default function ConnectPage() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [loading, setLoading] = useState(true);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [showSetup, setShowSetup] = useState(false);
  // Which button is mid-flight, so it can answer the press at once.
  const [opening, setOpening] = useState<Service | null>(null);
  const [disconnecting, setDisconnecting] = useState<Service | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await getAuthStatus());
      setFailure(null);
    } catch (caught) {
      setFailure(describeError(caught, "Could not check the connections."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Both callbacks send the browser back here with a query string saying how
    // it went. Read it, then clean the address bar so a refresh does not show a
    // stale message.
    const params = new URLSearchParams(window.location.search);
    const problem = params.get("spotify_error") ?? params.get("youtube_error");
    if (problem) setLoginError(problem);
    if (problem || params.get("spotify") || params.get("youtube")) {
      window.history.replaceState({}, "", "/");
    }
    void refresh();
  }, [refresh]);

  function connect(service: Service) {
    // The login is a full-page redirect, so without this the button looks
    // dead for the second it takes the backend to answer.
    setOpening(service);
    setLoginError(null);
    window.location.assign(
      service === "spotify" ? spotifyLoginUrl() : youtubeLoginUrl(),
    );
  }

  async function disconnect(service: Service) {
    setDisconnecting(service);
    setFailure(null);
    try {
      await (service === "spotify" ? disconnectSpotify() : disconnectYoutube());
      await refresh();
    } catch (caught) {
      setFailure(describeError(caught, "Could not disconnect."));
    } finally {
      setDisconnecting(null);
    }
  }

  const youtubeOk = !!status?.youtube_connected;
  const spotifyOk = !!status?.spotify_connected;
  const ready = youtubeOk && spotifyOk;

  // Nothing connected at all almost always means the credentials are missing,
  // so open the guide rather than making people hunt for it. Not when the
  // backend is down, though: then the guide is noise and the error is the point.
  const needsSetup = !loading && !failure && !youtubeOk && !spotifyOk;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">
          Move your music to Spotify
        </h1>
        <p className="mt-1.5 text-sm text-muted">
          Connect both accounts, pick what to move, and this copies your playlists
          across. Everything runs on your own machine — your library is never sent
          to anyone.
        </p>
      </div>

      {failure && (
        <ErrorNote
          title="Could not check the connections"
          hint={failure.hint}
          action={
            <Button size="sm" variant="secondary" loading={loading} onClick={refresh}>
              Try again
            </Button>
          }
        >
          {failure.message}
        </ErrorNote>
      )}

      {loginError && (
        <ErrorNote
          title="Login failed"
          action={
            <Button
              size="sm"
              variant="secondary"
              onClick={() => {
                setLoginError(null);
                setShowSetup(true);
              }}
            >
              Show the setup guide
            </Button>
          }
        >
          {loginError}
        </ErrorNote>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        <ServiceCard
          service="youtube"
          title="YouTube Music"
          label="YouTube"
          loading={loading}
          connected={youtubeOk}
          configured={status?.youtube_configured ?? true}
          detail={status?.youtube_detail ?? "Unknown"}
          opening={opening}
          disconnecting={disconnecting}
          onConnect={() => connect("youtube")}
          onDisconnect={() => disconnect("youtube")}
        />
        <ServiceCard
          service="spotify"
          title="Spotify"
          label="Spotify"
          loading={loading}
          connected={spotifyOk}
          configured={status?.spotify_configured ?? true}
          detail={status?.spotify_detail ?? "Unknown"}
          opening={opening}
          disconnecting={disconnecting}
          onConnect={() => connect("spotify")}
          onDisconnect={() => disconnect("spotify")}
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        {ready ? (
          <ButtonLink href="/library">Choose playlists →</ButtonLink>
        ) : (
          <Button variant="secondary" onClick={refresh} loading={loading}>
            {loading ? "Checking…" : "Check again"}
          </Button>
        )}

        <button
          type="button"
          onClick={() => setShowSetup((open) => !open)}
          className="text-sm text-muted underline underline-offset-2 transition-colors hover:text-ink"
        >
          {showSetup || needsSetup ? "Hide setup guide" : "First time here? Setup guide"}
        </button>
      </div>

      {(showSetup || needsSetup) && <SetupGuide status={status} />}

      <RecentTransfers />
    </div>
  );
}

/** One service: its status line, and the one button that makes sense now. */
function ServiceCard({
  service,
  title,
  label,
  loading,
  connected,
  configured,
  detail,
  opening,
  disconnecting,
  onConnect,
  onDisconnect,
}: {
  service: Service;
  title: string;
  label: string;
  loading: boolean;
  connected: boolean;
  configured: boolean;
  detail: string;
  opening: Service | null;
  disconnecting: Service | null;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  const busy = opening === service;

  return (
    <Card accent={service}>
      <div className="flex items-start gap-2.5">
        <span className="mt-0.5">
          <StatusDot ok={connected} pending={loading} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">{title}</h2>
          {loading ? (
            <Skeleton className="mt-1.5 h-3.5 w-40" />
          ) : (
            <p className="mt-1 break-words text-xs text-muted">{detail}</p>
          )}

          {!loading && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {connected ? (
                <Button
                  size="sm"
                  variant="secondary"
                  loading={disconnecting === service}
                  onClick={onDisconnect}
                >
                  {disconnecting === service ? "Disconnecting…" : "Disconnect"}
                </Button>
              ) : (
                <>
                  <Button
                    size="sm"
                    variant={service === "spotify" ? "spotify" : "primary"}
                    loading={busy}
                    disabled={!configured || opening !== null}
                    onClick={onConnect}
                  >
                    {busy ? `Opening ${label}…` : `Connect ${label}`}
                  </Button>
                  {!configured && (
                    <span className="text-xs text-muted">
                      Add the keys to <code>.env</code> first — the guide below
                      shows where to get them.
                    </span>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </Card>
  );
}

/** Past transfers, so a restart never hides finished work behind a lost URL. */
function RecentTransfers() {
  const [jobs, setJobs] = useState<JobSummary[]>([]);

  useEffect(() => {
    // A missing backend is already reported at the top of the page, so a
    // failure here just means no list rather than a second error.
    listJobs()
      .then(setJobs)
      .catch(() => setJobs([]));
  }, []);

  if (jobs.length === 0) return null;

  return (
    <Card title="Recent transfers" subtitle="Pick one up where it stopped">
      <ul className="mt-1 divide-y divide-line">
        {jobs.map((job) => (
          <li key={job.id}>
            <Link
              href={`/transfer/${job.id}`}
              className="-mx-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded-md px-2 py-2.5 transition-colors hover:bg-white/[0.04] active:bg-white/[0.07]"
            >
              <span className="text-sm font-medium">
                {describeJob(job)}
              </span>
              <span className="text-xs text-muted">
                {job.counters.processed} of {job.counters.total} songs
                {job.dry_run && " · dry run"}
                {" · "}
                {new Date(job.updated_at).toLocaleDateString(undefined, {
                  day: "numeric",
                  month: "short",
                })}
              </span>
              <span className="ml-auto text-xs text-muted">{job.status}</span>
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** Name a job by what it moved, since the id means nothing to anyone. */
function describeJob(job: JobSummary): string {
  const titles = job.playlist_titles;
  if (titles.length === 0) return "Transfer";
  if (titles.length <= 2) return titles.join(", ");
  return `${titles[0]}, ${titles[1]} and ${titles.length - 2} more`;
}

/** The full first-run setup, written so a friend can follow it unaided. */
function SetupGuide({ status }: { status: AuthStatus | null }) {
  const youtubeOk = !!status?.youtube_connected;
  const spotifyOk = !!status?.spotify_connected;

  return (
    <div className="space-y-4">
      <InfoNote>
        This is a one-off, about 15 minutes. You are creating your own free API
        keys for Spotify and Google, so nothing is shared with anyone and there
        are no limits imposed by a middleman. Keys go in the{" "}
        <code>.env</code> file in the project folder.
      </InfoNote>

      <Card title="Spotify" subtitle="About 5 minutes">
        <ol className="mt-1">
          <Step number={1} title="Create an app" done={spotifyOk}>
            Go to the{" "}
            <a
              href="https://developer.spotify.com/dashboard"
              target="_blank"
              rel="noreferrer"
              className="text-spotify underline underline-offset-2"
            >
              Spotify developer dashboard
            </a>{" "}
            and click <strong>Create app</strong>. Any name and description will
            do. Tick <strong>Web API</strong>.
          </Step>

          <Step number={2} title="Add the redirect URI" done={spotifyOk}>
            Paste this into <strong>Redirect URIs</strong>, then click{" "}
            <strong>Add</strong> before saving. It must be{" "}
            <code>127.0.0.1</code> — Spotify rejects <code>localhost</code>.
            <CopyField value={SPOTIFY_REDIRECT} />
          </Step>

          <Step number={3} title="Allow your own account" done={spotifyOk}>
            Open the app&apos;s <strong>User Management</strong> tab and add your
            name and the email address on your Spotify account. Without this
            Spotify returns 403 even though the app is yours.
          </Step>

          <Step number={4} title="Copy the keys into .env" done={spotifyOk}>
            From <strong>Settings</strong>, copy the Client ID and{" "}
            <strong>View client secret</strong>, then put them in{" "}
            <code>.env</code>:
            <CopyField value="SPOTIFY_CLIENT_ID=" />
            <CopyField value="SPOTIFY_CLIENT_SECRET=" />
          </Step>
        </ol>

        <InfoNote tone="warn">
          The Spotify account that owns the app needs{" "}
          <strong>Premium</strong>. Since February 2026 development-mode apps do
          not work without it.
        </InfoNote>
      </Card>

      <Card title="Google" subtitle="About 10 minutes">
        <ol className="mt-1">
          <Step number={1} title="Create a project" done={youtubeOk}>
            Go to the{" "}
            <a
              href="https://console.cloud.google.com/projectcreate"
              target="_blank"
              rel="noreferrer"
              className="text-spotify underline underline-offset-2"
            >
              Google Cloud Console
            </a>{" "}
            and create a project. Any name.
          </Step>

          <Step number={2} title="Enable the YouTube Data API" done={youtubeOk}>
            Search for <strong>YouTube Data API v3</strong> and click{" "}
            <strong>Enable</strong>. This is what lets the app read your
            playlists.
          </Step>

          <Step number={3} title="Set up the consent screen" done={youtubeOk}>
            Under <strong>Google Auth Platform</strong>, set the app name and
            your email, choose <strong>External</strong>, and add your own Google
            address under <strong>Test users</strong>.
          </Step>

          <Step number={4} title="Create a Web application client" done={youtubeOk}>
            <strong>Credentials → Create credentials → OAuth client ID</strong>.
            Application type must be <strong>Web application</strong>. Add this
            as an authorised redirect URI:
            <CopyField value={YOUTUBE_REDIRECT} />
          </Step>

          <Step number={5} title="Copy the keys into .env" done={youtubeOk}>
            <CopyField value="YTM_CLIENT_ID=" />
            <CopyField value="YTM_CLIENT_SECRET=" />
            Then restart the app and press <strong>Connect YouTube</strong> above.
          </Step>
        </ol>

        <InfoNote tone="warn">
          Google will warn that the app is not verified. It is your own app, so
          click <strong>Advanced</strong> then <strong>Go to … (unsafe)</strong>.
          While the project is in <em>Testing</em> the login expires after 7 days;
          publishing it to <em>Production</em> on the Audience page removes that.
        </InfoNote>
      </Card>
    </div>
  );
}
