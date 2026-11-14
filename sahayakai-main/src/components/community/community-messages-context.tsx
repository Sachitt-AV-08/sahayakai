"use client";

/**
 * Open/closed state and data for the floating Community Messages panel.
 *
 * One provider in the app shell, so the header's Messages button, the panel
 * and the homepage's VIDYA layout all read the same state and respond in the
 * same render. The feed is fetched once per signed-in session (the same
 * GET /api/groups/feed the Community page uses) and refreshed when the panel
 * opens after it has gone stale; nothing else in the app shell polls it.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import { useAuth } from "@/context/auth-context";
import { useLanguage } from "@/context/language-context";
import { getUnifiedFeedAction } from "@/lib/api/groups";
import { countNewerThan, newestTimestamp, pickTickerItems, type TickerItem } from "@/lib/community-feed-ticker";
import type { FeedItem } from "@/types/community";

const FEED_LIMIT = 20;
const STALE_MS = 2 * 60 * 1000;
const SEEN_KEY = "sahayak.communityMessages.seenAt";

export type CommunityFeedStatus = "idle" | "loading" | "ready" | "error";

export interface CommunityMessagesState {
  /** Community is for signed-in teachers; signed out there is no button or panel. */
  signedIn: boolean;
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
  status: CommunityFeedStatus;
  items: TickerItem[];
  offline: boolean;
  /** Posts newer than the last time the panel was opened. */
  newCount: number;
  reload: () => void;
}

const CommunityMessagesContext = createContext<CommunityMessagesState | null>(null);

function readSeen(): number {
  try {
    const v = Number(localStorage.getItem(SEEN_KEY));
    return Number.isFinite(v) ? v : 0;
  } catch {
    return 0;
  }
}

function writeSeen(ms: number) {
  try {
    localStorage.setItem(SEEN_KEY, String(ms));
  } catch {
    // storage blocked: the indicator just resets next visit
  }
}

export function CommunityMessagesProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const { language } = useLanguage();
  const pathname = usePathname();
  const uid = user?.uid ?? null;

  const [open, setOpenState] = useState(false);
  const [status, setStatus] = useState<CommunityFeedStatus>("idle");
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const [offline, setOffline] = useState(false);
  const [seenAt, setSeenAt] = useState(0);
  const loadedAt = useRef(0);
  const requestId = useRef(0);

  const load = useCallback(() => {
    if (!uid) return;
    const id = ++requestId.current;
    setStatus((s) => (s === "ready" ? s : "loading"));
    getUnifiedFeedAction(FEED_LIMIT)
      .then((data) => {
        if (id !== requestId.current) return;
        setFeed(Array.isArray(data) ? data : []);
        loadedAt.current = Date.now();
        setStatus("ready");
      })
      .catch(() => {
        if (id !== requestId.current) return;
        // Keep what was already shown; only an empty panel becomes an error.
        setStatus((s) => (s === "ready" ? s : "error"));
      });
  }, [uid]);

  // Load once per signed-in user; clear when signed out.
  useEffect(() => {
    setSeenAt(readSeen());
    if (!uid) {
      requestId.current++;
      setFeed([]);
      setStatus("idle");
      setOpenState(false);
      return;
    }
    load();
  }, [uid, load]);

  useEffect(() => {
    const sync = () => setOffline(typeof navigator !== "undefined" && navigator.onLine === false);
    sync();
    window.addEventListener("online", sync);
    window.addEventListener("offline", sync);
    return () => {
      window.removeEventListener("online", sync);
      window.removeEventListener("offline", sync);
    };
  }, []);

  // Back online with nothing to show: try again.
  useEffect(() => {
    if (!offline && status === "error") load();
  }, [offline]); // eslint-disable-line react-hooks/exhaustive-deps

  // The panel belongs to the page it was opened on: navigating (e.g. into a
  // post) closes it.
  const firstPath = useRef(pathname);
  useEffect(() => {
    if (firstPath.current === pathname) return;
    firstPath.current = pathname;
    setOpenState(false);
  }, [pathname]);

  const items = useMemo(
    () => pickTickerItems(feed, { viewerUid: uid, language }),
    [feed, uid, language],
  );

  const setOpen = useCallback(
    (next: boolean) => {
      setOpenState(next);
      if (next && uid && Date.now() - loadedAt.current > STALE_MS) load();
    },
    [uid, load],
  );
  const toggle = useCallback(() => setOpen(!open), [open, setOpen]);

  // Opening the panel marks what it shows as seen.
  useEffect(() => {
    if (!open || items.length === 0) return;
    const newest = newestTimestamp(items);
    if (newest > seenAt) {
      writeSeen(newest);
      setSeenAt(newest);
    }
  }, [open, items, seenAt]);

  // Escape closes the panel from anywhere.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.defaultPrevented) setOpenState(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const newCount = open ? 0 : countNewerThan(items, seenAt);

  const value = useMemo<CommunityMessagesState>(
    () => ({ signedIn: !!uid, open, setOpen, toggle, status, items, offline, newCount, reload: load }),
    [uid, open, setOpen, toggle, status, items, offline, newCount, load],
  );

  return <CommunityMessagesContext.Provider value={value}>{children}</CommunityMessagesContext.Provider>;
}

/** The panel state, or null outside the app shell (e.g. tests, landing page). */
export function useCommunityMessages(): CommunityMessagesState | null {
  return useContext(CommunityMessagesContext);
}
