"use client";

// Screen 3: watch the run, then read the report.
//
// The page polls a light "status" endpoint every second or two and fetches the
// heavy results table separately - on arrival, when a run finishes, and now
// and then while one goes on. Polling the whole table made every button feel
// slow, because a megabyte of JSON arrived with every heartbeat.
//
// Buttons answer at once: Stop and Resume update the screen immediately and
// the poll confirms the truth a moment later. Coming back to the tab asks
// the backend straight away rather than waiting out the timer.

import { memo, use, useEffect, useRef, useState } from "react";
import {
  Button,
  ButtonLink,
  Card,
  ErrorNote,
  InfoNote,
  ProgressBar,
  Spinner,
  Stat,
  VerdictBadge,
} from "@/components/ui";
import {
  ApiError,
  applyJob,
  cancelJob,
  describeError,
  formatDuration,
  getJob,
  getJobProgress,
  isLoginProblem,
  mentionsLogin,
  reportUrl,
  resumeJob,
  type Failure,
} from "@/lib/api";
import type { JobProgress, MatchResult, Verdict } from "@/lib/types";

const POLL_INTERVAL_MS = 1500;
// Slower, but never zero: a finished job can be started again from this page.
const IDLE_POLL_INTERVAL_MS = 4000;
// How often the results table is refreshed while a run is going on.
const RESULTS_REFRESH_MS = 10_000;
const FINISHED = new Set(["completed", "cancelled", "failed"]);

type Filter = "all" | Verdict;

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "Everything" },
  { key: "matched", label: "Matched" },
  { key: "low_confidence", label: "Not sure" },
  { key: "not_found", label: "Not found" },
  { key: "error", label: "Errors" },
];

export default function TransferPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = use(params);
  const [job, setJob] = useState<JobProgress | null>(null);
  const [results, setResults] = useState<MatchResult[]>([]);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [resuming, setResuming] = useState(false);
  const [applying, setApplying] = useState(false);
  const [stopping, setStopping] = useState(false);
  // Bumped by "Try again": restarts the polling loop from scratch.
  const [attempt, setAttempt] = useState(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    let inFlight = false;
    const resultsFetchedAt = { at: 0 };
    let wasFinished = false;

    async function fetchResults() {
      const full = await getJob(jobId);
      if (cancelled) return;
      setResults(full.results);
      resultsFetchedAt.at = Date.now();
    }

    async function poll() {
      // One heartbeat at a time. A tab coming back into view asks at once,
      // and that must not stack a second loop on top of the first.
      if (cancelled || inFlight) return;
      inFlight = true;
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
      }
      try {
        const latest = await getJobProgress(jobId);
        if (cancelled) return;
        setJob(latest);
        setFailure(null);

        const finished = FINISHED.has(latest.status);
        if (finished) setStopping(false);

        // The heavy table: on arrival, at the moment a run finishes, and
        // occasionally while one goes on. Never with every heartbeat.
        const stale = Date.now() - resultsFetchedAt.at >= RESULTS_REFRESH_MS;
        const justFinished = wasFinished === false && finished === true;
        if (resultsFetchedAt.at === 0 || justFinished || (!finished && stale)) {
          // Mark it now, so a slow fetch is not started again by the next poll.
          resultsFetchedAt.at = Date.now();
          void fetchResults().catch(() => undefined);
        }
        wasFinished = finished;

        // Never stop asking. A finished job can start again - Resume, or Add
        // matched songs - and a page that stopped polling shows a dead screen
        // while the work is really running. Just ask less often.
        timer.current = setTimeout(
          poll,
          finished ? IDLE_POLL_INTERVAL_MS : POLL_INTERVAL_MS,
        );
      } catch (caught) {
        if (cancelled) return;
        setFailure(describeError(caught, "Lost contact with the backend."));
        // A job that is not there will not appear by asking again.
        const status = caught instanceof ApiError ? caught.status : null;
        if (status !== 404) {
          timer.current = setTimeout(poll, POLL_INTERVAL_MS * 2);
        }
      } finally {
        inFlight = false;
      }
    }

    function onVisibilityChange() {
      if (document.visibilityState === "visible") void poll();
    }
    document.addEventListener("visibilitychange", onVisibilityChange);

    void poll();
    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (timer.current) clearTimeout(timer.current);
    };
  }, [jobId, attempt]);

  const retry = () => setAttempt((n) => n + 1);

  const failureNote = failure && (
    <ErrorNote
      title={job ? "Something went wrong" : "Could not load this transfer"}
      hint={failure.hint}
      action={
        <>
          {failure.status !== 404 && (
            <Button size="sm" variant="secondary" onClick={retry}>
              Try again
            </Button>
          )}
          {isLoginProblem(failure) && (
            <ButtonLink size="sm" href="/">
              Go to Connect
            </ButtonLink>
          )}
          {failure.status === 404 && (
            <ButtonLink size="sm" href="/">
              Back to the home screen
            </ButtonLink>
          )}
        </>
      }
    >
      {failure.message}
    </ErrorNote>
  );

  if (failure && !job) return failureNote;
  if (!job) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted">
        <Spinner />
        Loading the transfer…
      </p>
    );
  }

  const { counters } = job;
  const running = !FINISHED.has(job.status);
  // Unsearched songs are the only thing that decides this. Checking the status
  // as well used to hide Resume forever after "Add matched songs" finished,
  // because that marks the job completed while songs are still unsearched.
  const canResume = job.request !== null && counters.processed < counters.total;
  const songsLeft = counters.total - counters.processed;
  // Name the playlists with songs still to search, so "545 left" is concrete.
  const unfinishedPlaylists = job.playlists
    .filter((p) => p.processed < p.total)
    .map((p) => `${p.source_title} (${p.total - p.processed})`);

  const startResume = async () => {
    setResuming(true);
    setFailure(null);
    try {
      // Show it as running at once. Waiting for the next poll leaves the old
      // "stopped" screen up for a second, which reads as a dead button.
      setJob({ ...job, status: "running", error: null, activity: "Starting…" });
      await resumeJob(jobId);
    } catch (caught) {
      setFailure(describeError(caught, "Could not resume."));
      setJob(job);
    } finally {
      setResuming(false);
    }
  };
  // A live run writes each playlist itself as it goes, so once it has
  // finished there is nothing left to add and the button would only confuse.
  // It is offered after a dry run, and after a live run that stopped early
  // (the playlists after the stop were never written). Adding is idempotent,
  // so there is no need to guess how many are outstanding.
  const allWritten =
    !running && !job.dry_run && job.status === "completed" && counters.matched > 0;
  const canAdd = !running && counters.matched > 0 && !allWritten;
  const alreadyThere = Math.max(0, counters.matched - counters.added);
  // The daily limit is not a failure, so it gets its own panel rather than a
  // red error box with a stack-trace flavour to it. Spotify names the quota on
  // some days and only sends 429s on others, so every rate-limit-shaped stop
  // is treated the same way.
  const hitQuota =
    !!job.error && /daily limit|quota|rate limit|429/i.test(job.error);
  const rows =
    filter === "all"
      ? results
      : results.filter((result) => result.verdict === filter);

  const startApply = async () => {
    setApplying(true);
    setFailure(null);
    try {
      await applyJob(jobId);
      // The poll carries the truth a second later; this keeps the screen
      // answering at once instead of flashing through a reload.
      setJob({
        ...job,
        status: "running",
        error: null,
        activity: "Adding matched songs to your Spotify playlists…",
      });
    } catch (caught) {
      setFailure(describeError(caught, "Could not add the songs."));
    } finally {
      setApplying(false);
    }
  };

  const stop = () => {
    // Felt at once; the engine confirms a moment later.
    setStopping(true);
    setFailure(null);
    cancelJob(jobId).catch((caught) => {
      setStopping(false);
      setFailure(describeError(caught, "Could not stop the job."));
    });
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">
            {job.dry_run ? "Dry run" : "Transfer"}{" "}
            <span className="text-muted">·</span>{" "}
            <span className="inline-flex items-center gap-1.5 font-normal text-muted">
              {running && !stopping && <Spinner />}
              {stopping && job.status === "running"
                ? "stopping"
                : statusLabel(job)}
            </span>
          </h1>
          <p className="mt-1 text-sm text-muted">
            {job.dry_run
              ? "Nothing is being written to Spotify. This is a preview."
              : running
                ? "Searching, and adding matches to your Spotify playlists."
                : `${counters.added} songs added to Spotify so far.`}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          {running && (
            <Button variant="danger" loading={stopping} onClick={stop}>
              {stopping ? "Stopping…" : "Stop"}
            </Button>
          )}
          {!running && canResume && (
            <Button loading={resuming} onClick={startResume}>
              {resuming ? "Resuming…" : "Resume"}
            </Button>
          )}
          <ButtonLink variant="secondary" href="/library">
            Back to playlists
          </ButtonLink>
        </div>
      </div>

      {job.notice && (
        <div className="rounded-lg border border-amber-400/25 bg-amber-400/10 px-4 py-3 text-sm text-amber-200">
          <span className="font-medium">Waiting.</span> {job.notice}
          {job.waiting_until && <Countdown until={job.waiting_until} />}
        </div>
      )}

      {failureNote}
      {job.error && !hitQuota && (
        <ErrorNote
          title="The transfer stopped"
          action={
            <>
              {canResume && (
                <Button size="sm" loading={resuming} onClick={startResume}>
                  Resume
                </Button>
              )}
              {mentionsLogin(job.error) && (
                <ButtonLink size="sm" variant="secondary" href="/">
                  Go to Connect
                </ButtonLink>
              )}
            </>
          }
        >
          {job.error}
        </ErrorNote>
      )}
      {hitQuota && <QuotaStopped job={job} />}

      {job.warnings.length > 0 && (
        <InfoNote tone="warn">
          {job.warnings.map((warning) => (
            <p key={warning}>{warning}</p>
          ))}
        </InfoNote>
      )}

      <Card>
        <ProgressBar
          done={counters.processed}
          total={counters.total}
          animated={running}
        />
        {running && <LiveActivity job={job} />}
      </Card>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Matched" value={counters.matched} tone="good" />
        <Stat label="Not sure" value={counters.low_confidence} tone="warn" />
        <Stat label="Not found" value={counters.not_found} tone="bad" />
        <Stat
          label={job.dry_run ? "Would be added" : "Added to Spotify"}
          value={job.dry_run ? counters.matched : counters.added}
          // A bare 0 reads as "nothing worked", when usually it means the
          // songs were already there from an earlier run. Say which. Once a
          // live run has finished, matched minus added is the exact figure.
          hint={
            !job.dry_run &&
            (allWritten ? alreadyThere : counters.skipped_duplicates) > 0
              ? `${(allWritten ? alreadyThere : counters.skipped_duplicates).toLocaleString()} already in your playlists`
              : undefined
          }
        />
      </div>

      {!running && canResume && (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-sm">
              <p>
                <span className="font-medium">
                  {songsLeft.toLocaleString()} songs have not been searched yet.
                </span>{" "}
                Resume carries on from song{" "}
                {(counters.processed + 1).toLocaleString()} and keeps every
                result already found, so nothing is searched twice.
              </p>
              <p className="mt-1 text-xs text-muted">
                Still to do: {unfinishedPlaylists.join(", ") || "the rest of your library"}.
              </p>
            </div>
            <Button loading={resuming} onClick={startResume}>
              {resuming ? "Resuming…" : `Resume — ${songsLeft.toLocaleString()} songs`}
            </Button>
          </div>
        </Card>
      )}

      {allWritten && (
        <Card className="border-spotify/30">
          <p className="text-sm">
            <span className="font-medium text-spotify">
              Every matched song is in your Spotify playlists.
            </span>{" "}
            This transfer added {counters.added.toLocaleString()} of them. The
            other {alreadyThere.toLocaleString()} were there already, so nothing
            was added twice. The links below open each playlist.
          </p>
        </Card>
      )}

      {canAdd && (
        <Card>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm">
              <span className="font-medium">
                {counters.matched} songs matched.
              </span>{" "}
              Adding them uses the matches already worked out, so it costs a
              handful of requests instead of searching again — and it works even
              when the daily search quota is spent. Anything already in the
              playlist is skipped, so pressing this twice is harmless.
            </p>
            <Button variant="spotify" loading={applying} onClick={startApply}>
              {applying ? "Adding…" : "Add matched songs to Spotify"}
            </Button>
          </div>
        </Card>
      )}

      {counters.low_confidence > 0 && (
        <Card>
          <p className="text-sm">
            <span className="font-medium">
              {counters.low_confidence} songs were not added on purpose.
            </span>{" "}
            The best guess for each was close but not certain, so adding it
            risked putting the wrong song in your playlist. They are listed below
            under <span className="font-medium">Not sure</span>, with a link to
            the guess. Adding those by hand takes a couple of minutes.
          </p>
        </Card>
      )}

      {!job.dry_run && job.playlists.some((p) => p.spotify_playlist_url) && (
        <Card title="Playlists on Spotify">
          <ul className="space-y-1 text-sm">
            {job.playlists
              .filter((playlist) => playlist.spotify_playlist_url)
              .map((playlist) => (
                <li key={playlist.source_id} className="flex gap-2">
                  <a
                    href={playlist.spotify_playlist_url ?? "#"}
                    target="_blank"
                    rel="noreferrer"
                    className="text-spotify underline underline-offset-2"
                  >
                    {playlist.source_title}
                  </a>
                  <span className="tabular text-muted">
                    {playlist.added} added
                    {playlist.skipped_duplicates > 0 &&
                      `, ${playlist.skipped_duplicates} already there`}
                  </span>
                </li>
              ))}
          </ul>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {FILTERS.map(({ key, label }) => (
          <button
            key={key}
            type="button"
            onClick={() => setFilter(key)}
            aria-pressed={filter === key}
            className={`rounded-md px-3 py-1.5 text-xs font-medium transition-[background-color,color,transform] duration-150 active:scale-95 ${
              filter === key
                ? "bg-ink text-canvas"
                : "border border-line bg-surface text-muted hover:bg-raised hover:text-ink"
            }`}
          >
            {label}
            {key !== "all" && (
              <span className="tabular ml-1.5 opacity-70">
                {results.filter((r) => r.verdict === key).length}
              </span>
            )}
          </button>
        ))}
        <span className="ml-auto flex flex-wrap gap-2">
          <ButtonLink
            variant="secondary"
            href={reportUrl(jobId, true)}
            disabled={results.length === 0}
          >
            Download problems (CSV)
          </ButtonLink>
          <ButtonLink
            variant="secondary"
            href={reportUrl(jobId, false)}
            disabled={results.length === 0}
          >
            Download all (CSV)
          </ButtonLink>
        </span>
      </div>

      <Card>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-muted">
                <th className="pb-2 font-medium">From YouTube Music</th>
                <th className="pb-2 font-medium">Found on Spotify</th>
                <th className="w-24 pb-2 font-medium">Verdict</th>
                <th className="w-16 pb-2 text-right font-medium">Score</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((result, index) => (
                <ResultRow
                  key={`${result.source.video_id}-${index}`}
                  result={result}
                />
              ))}
            </tbody>
          </table>
        </div>

        {rows.length === 0 && (
          <p className="py-4 text-sm text-muted">
            {running ? "Working…" : "Nothing in this category."}
          </p>
        )}
      </Card>
    </div>
  );
}

/** Proof the job is alive: what it is doing, and a clock that keeps ticking.
 *
 * The song count can sit still for a minute while the library is read from
 * YouTube, and a frozen screen is indistinguishable from a crash. These three
 * things all change on their own, so the page is never silent.
 */
function LiveActivity({ job }: { job: JobProgress }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const since = new Date(job.updated_at).getTime();
  const quiet = Math.max(0, Math.round((now - since) / 1000));

  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-3 text-xs">
      <span className="flex items-center gap-1.5 font-medium">
        <Spinner />
        {job.activity ?? "Working…"}
      </span>
      <span className="tabular text-muted">
        {job.counters.searches.toLocaleString()} Spotify searches used
      </span>
      <span className="tabular ml-auto text-muted">
        {quiet < 90
          ? `updated ${quiet}s ago`
          : `no change for ${Math.floor(quiet / 60)}m — it may be waiting on Spotify`}
      </span>
    </div>
  );
}

/** Why the run stopped on the daily limit, and when it is worth trying again. */
function QuotaStopped({ job }: { job: JobProgress }) {
  const { counters } = job;
  const left = counters.total - counters.processed;

  return (
    <div className="rounded-lg border border-amber-400/30 bg-amber-400/10 px-4 py-3.5 text-sm text-amber-100">
      <p className="font-semibold">
        Stopped for today — Spotify&apos;s daily limit is used up
      </p>

      <dl className="mt-2.5 grid gap-x-6 gap-y-1 sm:grid-cols-[auto_1fr]">
        <dt className="font-medium">Done so far</dt>
        <dd className="tabular">
          {counters.processed.toLocaleString()} of{" "}
          {counters.total.toLocaleString()} songs, {counters.matched} matched
        </dd>

        <dt className="font-medium">Still to do</dt>
        <dd className="tabular">{left.toLocaleString()} songs</dd>

        <dt className="font-medium">Searches used</dt>
        <dd className="tabular">
          {counters.searches.toLocaleString()} — the limit is about 1,000 a day
        </dd>

        <dt className="font-medium">Try again</dt>
        <dd>
          {job.quota_resets_at ? (
            <QuotaCountdown until={job.quota_resets_at} />
          ) : (
            "Spotify did not say when. The limit resets about once a day, so try tomorrow."
          )}
        </dd>
      </dl>

      <p className="mt-2.5">
        Nothing is lost. Every match already found is saved, and your Spotify
        playlists were not touched by this. Come back and press{" "}
        <span className="font-medium">Resume</span> to carry on from song{" "}
        {(counters.processed + 1).toLocaleString()}.
      </p>
    </div>
  );
}

/** Counts down in hours and minutes, then says it is worth trying again. */
function QuotaCountdown({ until }: { until: string }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const left = Math.max(0, Math.round((new Date(until).getTime() - now) / 1000));
  if (left === 0) return <span className="font-medium">Now — press Resume.</span>;

  const hours = Math.floor(left / 3600);
  const minutes = Math.floor((left % 3600) / 60);
  const seconds = left % 60;
  const clock =
    hours > 0
      ? `${hours}h ${minutes.toString().padStart(2, "0")}m ${seconds.toString().padStart(2, "0")}s`
      : `${minutes}m ${seconds.toString().padStart(2, "0")}s`;

  return (
    <span className="tabular font-medium">
      in {clock}{" "}
      <span className="font-normal">
        (
        {new Date(until).toLocaleTimeString(undefined, {
          hour: "2-digit",
          minute: "2-digit",
        })}
        )
      </span>
    </span>
  );
}

function Countdown({ until }: { until: string }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const left = Math.max(0, Math.round((new Date(until).getTime() - now) / 1000));
  if (left === 0) return <span className="ml-1">Trying again now…</span>;

  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  return (
    <span className="tabular ml-1 font-medium">
      Trying again in {minutes}:{seconds.toString().padStart(2, "0")}
    </span>
  );
}

/** One row of the report. Memoised: with 1,700 results, re-rendering every row
 *  on every heartbeat was most of what made the page feel slow. */
const ResultRow = memo(function ResultRow({ result }: { result: MatchResult }) {
  const { source, best } = result;
  return (
    <tr className="border-b border-line align-top last:border-0">
      <td className="py-2 pr-4">
        <div>{source.title}</div>
        <div className="text-xs text-muted">
          {source.artists.join(", ") || "unknown artist"} ·{" "}
          {formatDuration(source.duration_seconds)}
        </div>
      </td>
      <td className="py-2 pr-4">
        {best ? (
          <>
            <div>
              {best.url ? (
                <a
                  href={best.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-spotify underline underline-offset-2"
                >
                  {best.title}
                </a>
              ) : (
                best.title
              )}
            </div>
            <div className="text-xs text-muted">
              {best.artists.join(", ")} · {formatDuration(best.duration_seconds)}
            </div>
          </>
        ) : (
          <span className="text-xs text-muted">{result.reason}</span>
        )}
      </td>
      <td className="py-2">
        <VerdictBadge verdict={result.verdict} />
      </td>
      <td className="tabular py-2 text-right text-muted">
        {result.score > 0 ? result.score.toFixed(2) : "—"}
      </td>
    </tr>
  );
});

function statusLabel(job: JobProgress): string {
  switch (job.status) {
    case "pending":
      return "starting";
    case "running":
      return job.notice ? "waiting" : "running";
    case "completed":
      return "finished";
    case "cancelled":
      return "stopped";
    case "failed":
      return "failed";
  }
}
