"use client";

// Screen 2: choose what to move.
// The dry-run toggle is on by default. For a job you only do once, seeing the
// report before anything is written to Spotify is worth one extra click.

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  Button,
  ButtonLink,
  Card,
  ErrorNote,
  InfoNote,
  Skeleton,
  Spinner,
} from "@/components/ui";
import {
  describeError,
  getPlaylists,
  isLoginProblem,
  startTransfer,
  type Failure,
} from "@/lib/api";
import type { PlaylistSummary } from "@/lib/types";

// Rough row widths for the loading skeleton, so it looks like a list of
// playlists rather than five identical bars.
const SKELETON_WIDTHS = ["w-1/3", "w-1/2", "w-2/5", "w-3/5", "w-1/4"];

export default function LibraryPage() {
  const router = useRouter();
  const [playlists, setPlaylists] = useState<PlaylistSummary[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dryRun, setDryRun] = useState(true);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setFailure(null);
    try {
      const found = await getPlaylists();
      setPlaylists(found);
      // Select everything by default: the usual intent is "move it all".
      setSelected(new Set(found.map((p) => p.id)));
    } catch (caught) {
      setFailure(describeError(caught, "Could not load your playlists."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function toggle(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  const allSelected = selected.size === playlists.length && playlists.length > 0;

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(playlists.map((p) => p.id)));
  }

  const songTotal = playlists
    .filter((p) => selected.has(p.id))
    .reduce((sum, p) => sum + (p.track_count ?? 0), 0);

  async function start() {
    setStarting(true);
    setFailure(null);
    try {
      const chosen = playlists.filter((p) => selected.has(p.id));
      const job = await startTransfer({
        playlist_ids: chosen.filter((p) => !p.is_liked_songs).map((p) => p.id),
        include_liked: chosen.some((p) => p.is_liked_songs),
        dry_run: dryRun,
      });
      router.push(`/transfer/${job.id}`);
    } catch (caught) {
      setFailure(describeError(caught, "Could not start the transfer."));
      setStarting(false);
    }
  }

  const heading = (
    <div>
      <h1 className="text-xl font-semibold">Choose what to move</h1>
      <p className="mt-1 text-sm text-muted">
        Each playlist is recreated on Spotify with the same name. Liked songs
        become a playlist called{" "}
        <span className="font-medium text-ink">YouTube Music Likes</span>,
        which is easy to check and easy to delete.
      </p>
    </div>
  );

  if (loading) {
    return (
      <div className="space-y-6" aria-busy>
        {heading}
        <p className="flex items-center gap-2 text-sm text-muted">
          <Spinner />
          Reading your YouTube Music library…
        </p>
        <Card>
          <div className="space-y-3.5 py-1">
            {SKELETON_WIDTHS.map((width) => (
              <div key={width} className="flex items-center gap-3">
                <Skeleton className="h-4 w-4 shrink-0" />
                <Skeleton className={`h-4 ${width}`} />
                <Skeleton className="ml-auto h-4 w-8" />
              </div>
            ))}
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {heading}

      {failure && (
        <ErrorNote
          title="Could not read your YouTube Music library"
          hint={failure.hint}
          action={
            <>
              <Button size="sm" variant="secondary" onClick={load}>
                Try again
              </Button>
              {isLoginProblem(failure) && (
                <ButtonLink size="sm" href="/">
                  Go to Connect
                </ButtonLink>
              )}
            </>
          }
        >
          {failure.message}
        </ErrorNote>
      )}

      {playlists.length > 0 && (
        <Card>
          <div className="mb-2 flex items-center justify-between text-xs text-muted">
            <span className="tabular" aria-live="polite">
              {selected.size} of {playlists.length} selected
            </span>
            <button
              type="button"
              onClick={toggleAll}
              className="underline underline-offset-2 transition-colors hover:text-ink"
            >
              {allSelected ? "Select none" : "Select all"}
            </button>
          </div>

          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="w-10 pb-2">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    onChange={toggleAll}
                    aria-label="Select every playlist"
                  />
                </th>
                <th className="pb-2 font-medium">Playlist</th>
                <th className="w-24 pb-2 text-right font-medium">Songs</th>
              </tr>
            </thead>
            <tbody>
              {playlists.map((playlist) => {
                const on = selected.has(playlist.id);
                return (
                  // The whole row is the target: a checkbox alone is a small
                  // thing to hit twenty times.
                  <tr
                    key={playlist.id}
                    onClick={() => toggle(playlist.id)}
                    className={`cursor-pointer select-none border-b border-line transition-colors last:border-0 hover:bg-white/[0.04] active:bg-white/[0.07] ${
                      on ? "" : "text-muted"
                    }`}
                  >
                    <td className="py-2">
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={() => toggle(playlist.id)}
                        onClick={(event) => event.stopPropagation()}
                        aria-label={`Select ${playlist.title}`}
                      />
                    </td>
                    <td className="py-2">
                      {playlist.title}
                      {playlist.is_liked_songs && (
                        <span className="ml-2 rounded-full bg-raised px-2 py-0.5 text-xs text-muted">
                          liked
                        </span>
                      )}
                    </td>
                    <td className="tabular py-2 text-right text-muted">
                      {playlist.track_count ?? "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}

      {!failure && playlists.length === 0 && (
        <Card>
          <p className="py-2 text-sm text-muted">
            No playlists found in your YouTube Music library.
          </p>
        </Card>
      )}

      <Card>
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            className="mt-1"
            checked={dryRun}
            onChange={(event) => setDryRun(event.target.checked)}
          />
          <span className="text-sm">
            <span className="font-medium">Dry run</span> — search for every song
            and show the full report, but write nothing to Spotify.
            <span className="mt-1 block text-xs text-muted">
              Leave this on for the first run. Look at the report, then come back
              and turn it off to do it for real.
            </span>
          </span>
        </label>
        {!dryRun && (
          <div className="mt-3">
            <InfoNote tone="warn">
              This run will create playlists in your Spotify account and add the
              matched songs to them. Songs already there are skipped, so running
              it twice is safe.
            </InfoNote>
          </div>
        )}
      </Card>

      <div className="flex flex-wrap items-center gap-3">
        <Button
          onClick={start}
          loading={starting}
          disabled={selected.size === 0 || playlists.length === 0}
          variant={dryRun ? "primary" : "spotify"}
        >
          {starting
            ? "Starting…"
            : dryRun
              ? "Start dry run"
              : "Transfer to Spotify"}
        </Button>
        <p className="text-sm text-muted" aria-live="polite">
          {playlists.length > 0 && selected.size === 0 ? (
            "Pick at least one playlist."
          ) : (
            <>
              {selected.size} selected
              {songTotal > 0 && (
                <span className="tabular"> · about {songTotal.toLocaleString()} songs</span>
              )}
            </>
          )}
        </p>
      </div>
    </div>
  );
}
