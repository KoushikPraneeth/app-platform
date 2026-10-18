"use client";

import React, { useEffect, useState, useCallback, useMemo } from "react";
import {
  Activity,
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Bookmark,
  CheckCircle2,
  ChevronDown,
  Database,
  Flame,
  HardDrive,
  Layers,
  MessageSquare,
  MessageSquareCode,
  RefreshCw,
  Send,
  Server,
  Share2,
  ShieldCheck,
  Terminal,
  TrendingUp,
  Zap,
} from "lucide-react";

interface CommunityPost {
  id: number;
  username: string;
  community: string;
  content: string;
  upvotes: number;
  created_at: string;
}

interface ReadinessState {
  ready: boolean;
  postgres: string;
  redis: string;
  lastChecked: string | null;
  error?: string;
}

const COMMUNITIES = [
  { id: "all", name: "All Feeds", icon: Flame, color: "text-amber-400" },
  { id: "r/kubernetes", name: "r/kubernetes", icon: Layers, color: "text-blue-400" },
  { id: "r/gitops", name: "r/gitops", icon: RefreshCw, color: "text-emerald-400" },
  { id: "r/azure", name: "r/azure", icon: Terminal, color: "text-sky-400" },
  { id: "r/terraform", name: "r/terraform", icon: Server, color: "text-purple-400" },
  { id: "r/general", name: "r/general", icon: MessageSquareCode, color: "text-slate-400" },
];

const API_BASE = typeof window !== "undefined" ? "" : (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000");

function getRelativeTime(isoString: string): string {
  try {
    const diff = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
    if (diff < 5) return "just now";
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  } catch {
    return "recently";
  }
}

function cleanUserHandle(rawUser: string): string {
  if (!rawUser) return "anonymous";
  return rawUser.replace(/^[@u\/]+/, "");
}

export default function ThreadOpsPlatform() {
  const [posts, setPosts] = useState<CommunityPost[]>([]);
  const [activeCommunity, setActiveCommunity] = useState<string>("all");
  const [cacheSource, setCacheSource] = useState<"cache" | "database" | null>(null);
  const [fetchLatency, setFetchLatency] = useState<number | null>(null);
  const [isLoadingFeed, setIsLoadingFeed] = useState<boolean>(false);
  const [isPosting, setIsPosting] = useState<boolean>(false);
  const [upvotingIds, setUpvotingIds] = useState<Record<number, boolean>>({});
  const [upvotedPostIds, setUpvotedPostIds] = useState<Set<number>>(new Set());
  const [downvotedPostIds, setDownvotedPostIds] = useState<Set<number>>(new Set());
  const [savedPostIds, setSavedPostIds] = useState<Set<number>>(new Set());

  // Form State
  const [username, setUsername] = useState<string>("alex_devops");
  const [selectedCommunity, setSelectedCommunity] = useState<string>("r/kubernetes");
  const [postContent, setPostContent] = useState<string>("");
  const [copiedPostId, setCopiedPostId] = useState<number | null>(null);

  // Notification State
  const [notification, setNotification] = useState<{
    type: "success" | "error" | "info";
    message: string;
  } | null>(null);

  const [systemHealth, setSystemHealth] = useState<ReadinessState>({
    ready: false,
    postgres: "checking...",
    redis: "checking...",
    lastChecked: null,
  });

  const [showArchWidget, setShowArchWidget] = useState<boolean>(true);

  // Readiness Probe Checker
  const checkHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/readyz`, { cache: "no-store" });
      const data = await res.json();
      if (res.ok && data.status === "ready") {
        setSystemHealth({
          ready: true,
          postgres: data.postgres || "connected",
          redis: data.redis || "connected",
          lastChecked: new Date().toLocaleTimeString(),
        });
      } else {
        setSystemHealth({
          ready: false,
          postgres: data.postgres || "error",
          redis: data.redis || "error",
          error: data.details || "Component unavailable",
          lastChecked: new Date().toLocaleTimeString(),
        });
      }
    } catch (err: unknown) {
      setSystemHealth({
        ready: false,
        postgres: "unreachable",
        redis: "unreachable",
        error: err instanceof Error ? err.message : "API offline",
        lastChecked: new Date().toLocaleTimeString(),
      });
    }
  }, []);

  // Fetch Community Feed
  const fetchPosts = useCallback(async () => {
    setIsLoadingFeed(true);
    const startTime = performance.now();
    try {
      const res = await fetch(`${API_BASE}/api/items`, { cache: "no-store" });
      const endTime = performance.now();
      setFetchLatency(Math.round(endTime - startTime));

      if (res.status === 429) {
        setNotification({
          type: "error",
          message: "Rate limit exceeded (Max 30 requests/minute).",
        });
        return;
      }

      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${res.statusText}`);
      }

      const data = await res.json();
      setPosts(data.items || []);
      setCacheSource(data.source === "cache" ? "cache" : "database");
    } catch (err: unknown) {
      setNotification({
        type: "error",
        message: err instanceof Error ? err.message : "Failed to load feed",
      });
    } finally {
      setIsLoadingFeed(false);
    }
  }, []);

  useEffect(() => {
    checkHealth();
    fetchPosts();

    const interval = setInterval(() => {
      checkHealth();
    }, 6000);

    return () => clearInterval(interval);
  }, [checkHealth, fetchPosts]);

  // Handle Post Creation
  const handleCreatePost = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!postContent.trim()) {
      setNotification({
        type: "info",
        message: "Please write something before posting.",
      });
      return;
    }

    setIsPosting(true);
    setNotification(null);

    const authorHandle = username.trim() ? `@${cleanUserHandle(username.trim())}` : "@alex_devops";

    try {
      const res = await fetch(`${API_BASE}/api/items`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: authorHandle,
          community: selectedCommunity,
          content: postContent.trim(),
        }),
      });

      if (res.status === 429) {
        setNotification({
          type: "error",
          message: "Rate limit triggered! Max 30 actions per minute.",
        });
        return;
      }

      if (!res.ok) {
        throw new Error(`Submission failed (HTTP ${res.status})`);
      }

      const created = await res.json();
      setNotification({
        type: "success",
        message: `Posted to ${created.community}!`,
      });

      setPostContent("");
      await fetchPosts();
    } catch (err: unknown) {
      setNotification({
        type: "error",
        message: err instanceof Error ? err.message : "Failed to publish post",
      });
    } finally {
      setIsPosting(false);
    }
  };

  // Handle Upvoting
  const handleUpvote = async (postId: number) => {
    if (upvotedPostIds.has(postId)) return;

    setUpvotingIds((prev) => ({ ...prev, [postId]: true }));
    setUpvotedPostIds((prev) => {
      const next = new Set(prev);
      next.add(postId);
      return next;
    });

    // Remove from downvoted if it was downvoted
    setDownvotedPostIds((prev) => {
      const next = new Set(prev);
      next.delete(postId);
      return next;
    });

    // Optimistic UI increment
    setPosts((prev) =>
      prev.map((p) => (p.id === postId ? { ...p, upvotes: (p.upvotes || 0) + 1 } : p))
    );

    try {
      const res = await fetch(`${API_BASE}/api/items/${postId}/upvote`, {
        method: "POST",
      });

      if (res.status === 429) {
        setNotification({
          type: "error",
          message: "Rate limit exceeded! Please slow down.",
        });
        return;
      }

      if (!res.ok) {
        throw new Error(`Upvote failed (HTTP ${res.status})`);
      }

      const data = await res.json();
      setPosts((prev) =>
        prev.map((p) => (p.id === postId ? { ...p, upvotes: data.upvotes } : p))
      );
    } catch (err: unknown) {
      setNotification({
        type: "error",
        message: err instanceof Error ? err.message : "Failed to upvote",
      });
    } finally {
      setUpvotingIds((prev) => ({ ...prev, [postId]: false }));
    }
  };

  // Handle Downvote (UI state toggle)
  const handleDownvote = (postId: number) => {
    if (downvotedPostIds.has(postId)) {
      setDownvotedPostIds((prev) => {
        const next = new Set(prev);
        next.delete(postId);
        return next;
      });
      setPosts((prev) =>
        prev.map((p) => (p.id === postId ? { ...p, upvotes: (p.upvotes || 0) + 1 } : p))
      );
    } else {
      setDownvotedPostIds((prev) => {
        const next = new Set(prev);
        next.add(postId);
        return next;
      });
      setUpvotedPostIds((prev) => {
        const next = new Set(prev);
        next.delete(postId);
        return next;
      });
      setPosts((prev) =>
        prev.map((p) => (p.id === postId ? { ...p, upvotes: Math.max(0, (p.upvotes || 0) - 1) } : p))
      );
    }
  };

  const handleShare = (postId: number) => {
    if (typeof window !== "undefined") {
      navigator.clipboard?.writeText(`${window.location.origin}/post/${postId}`);
    }
    setCopiedPostId(postId);
    setTimeout(() => setCopiedPostId(null), 2000);
  };

  const handleSave = (postId: number) => {
    setSavedPostIds((prev) => {
      const next = new Set(prev);
      if (next.has(postId)) {
        next.delete(postId);
      } else {
        next.add(postId);
      }
      return next;
    });
  };

  // Filtered Posts
  const filteredPosts = useMemo(() => {
    if (activeCommunity === "all") return posts;
    return posts.filter((p) => p.community.toLowerCase() === activeCommunity.toLowerCase());
  }, [posts, activeCommunity]);

  // Post counts per community
  const communityCounts = useMemo(() => {
    const counts: Record<string, number> = { all: posts.length };
    posts.forEach((p) => {
      const key = p.community.toLowerCase();
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  }, [posts]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 selection:bg-orange-500 selection:text-white">
      {/* 1. TOP SLIM NAVBAR WITH DEVOPS TELEMETRY (UNCHANGED) */}
      <header className="sticky top-0 z-50 border-b border-slate-800 bg-slate-950/80 backdrop-blur-md">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 h-14 flex items-center justify-between">
          {/* Brand Logo & Title */}
          <div className="flex items-center space-x-3">
            <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-orange-500 to-amber-600 flex items-center justify-center shadow-lg shadow-orange-500/20">
              <Zap className="w-4 h-4 text-white fill-white" />
            </div>
            <div className="flex items-baseline space-x-2">
              <span className="font-extrabold text-lg tracking-tight bg-gradient-to-r from-white via-slate-100 to-slate-400 bg-clip-text text-transparent">
                ThreadOps
              </span>
              <span className="hidden sm:inline-block text-xs font-medium text-slate-400">
                Cloud-Native Community & GitOps Feed
              </span>
            </div>
          </div>

          {/* Right: Live DevOps Telemetry Badges */}
          <div className="flex items-center space-x-2.5">
            {/* Cache Source Indicator Badge */}
            {cacheSource && (
              <div
                className={`hidden md:flex items-center space-x-1.5 px-2.5 py-1 rounded-full text-xs font-mono font-semibold border transition-all ${
                  cacheSource === "cache"
                    ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
                    : "bg-amber-500/10 border-amber-500/30 text-amber-400"
                }`}
                title="Cache-Aside validation status"
              >
                <span
                  className={`w-1.5 h-1.5 rounded-full ${
                    cacheSource === "cache" ? "bg-emerald-400 animate-pulse" : "bg-amber-400"
                  }`}
                />
                <span>{cacheSource === "cache" ? "REDIS CACHE (HIT)" : "POSTGRESQL DB (MISS)"}</span>
                {fetchLatency !== null && <span className="text-[10px] opacity-75">{fetchLatency}ms</span>}
              </div>
            )}

            {/* Deep Readiness Probe Badge */}
            <div
              className={`flex items-center space-x-1.5 px-2.5 py-1 rounded-full text-xs font-medium border ${
                systemHealth.ready
                  ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400"
                  : "bg-rose-500/10 border-rose-500/30 text-rose-400"
              }`}
            >
              <span
                className={`w-2 h-2 rounded-full ${
                  systemHealth.ready ? "bg-emerald-500 animate-pulse" : "bg-rose-500"
                }`}
              />
              <span className="hidden sm:inline">
                {systemHealth.ready ? "DB + Redis Connected" : "Degraded Health"}
              </span>
            </div>

            {/* Manual Refresh Button */}
            <button
              onClick={() => {
                fetchPosts();
                checkHealth();
              }}
              disabled={isLoadingFeed}
              className="p-1.5 rounded-lg border border-slate-800 bg-slate-900/60 hover:bg-slate-800 text-slate-300 hover:text-white transition-colors"
              title="Refresh Feed (checks cache headers)"
            >
              <RefreshCw className={`w-4 h-4 ${isLoadingFeed ? "animate-spin text-orange-400" : ""}`} />
            </button>
          </div>
        </div>
      </header>

      {/* 2. MAIN REDDIT 3-COLUMN LAYOUT */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-6">
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
          {/* ======================================================== */}
          {/* LEFT COLUMN: User Card & Communities (3 cols)            */}
          {/* ======================================================== */}
          <aside className="lg:col-span-3 space-y-4">
            {/* User Profile Card */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-4 shadow-xl backdrop-blur-sm">
              <div className="flex items-center space-x-3 mb-2">
                <div className="w-10 h-10 rounded-full bg-gradient-to-tr from-orange-500 to-amber-600 flex items-center justify-center text-white font-bold text-sm shadow-md">
                  {username.slice(0, 2).toUpperCase()}
                </div>
                <div>
                  <div className="font-bold text-sm text-slate-100 flex items-center space-x-1">
                    <span>u/{cleanUserHandle(username)}</span>
                  </div>
                  <div className="text-xs text-slate-400">DevOps Member</div>
                </div>
              </div>
            </div>

            {/* Communities Navigation */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-3 shadow-xl backdrop-blur-sm">
              <div className="px-3 py-2 text-[11px] font-bold uppercase tracking-wider text-slate-400">
                Feeds & Topics
              </div>
              <div className="space-y-1">
                {COMMUNITIES.map((comm) => {
                  const Icon = comm.icon;
                  const isActive = activeCommunity === comm.id;
                  const count = communityCounts[comm.id] || 0;
                  return (
                    <button
                      key={comm.id}
                      onClick={() => setActiveCommunity(comm.id)}
                      className={`w-full flex items-center justify-between px-3 py-2 rounded-xl text-xs font-medium transition-all ${
                        isActive
                          ? "bg-orange-500/10 text-orange-400 border border-orange-500/30 shadow-sm"
                          : "text-slate-300 hover:bg-slate-800/60 hover:text-white"
                      }`}
                    >
                      <div className="flex items-center space-x-2.5">
                        <Icon className={`w-4 h-4 ${comm.color}`} />
                        <span>{comm.name}</span>
                      </div>
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-slate-800/80 text-slate-400 font-mono">
                        {count}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          </aside>

          {/* ======================================================== */}
          {/* CENTER COLUMN: Create Post & Reddit Post Cards (6 col)   */}
          {/* ======================================================== */}
          <main className="lg:col-span-6 space-y-4">
            {/* Notifications / Toast */}
            {notification && (
              <div
                className={`p-3.5 rounded-xl border flex items-start space-x-3 text-xs ${
                  notification.type === "success"
                    ? "bg-emerald-950/40 border-emerald-500/30 text-emerald-300"
                    : notification.type === "error"
                    ? "bg-rose-950/40 border-rose-500/30 text-rose-300"
                    : "bg-cyan-950/40 border-cyan-500/30 text-cyan-300"
                }`}
              >
                {notification.type === "success" && (
                  <CheckCircle2 className="w-4 h-4 flex-shrink-0 text-emerald-400 mt-0.5" />
                )}
                {notification.type === "error" && (
                  <AlertCircle className="w-4 h-4 flex-shrink-0 text-rose-400 mt-0.5" />
                )}
                <div className="flex-1 font-medium">{notification.message}</div>
                <button
                  onClick={() => setNotification(null)}
                  className="opacity-70 hover:opacity-100 text-slate-300 text-xs"
                >
                  ✕
                </button>
              </div>
            )}

            {/* Reddit-Style Create Post Composer */}
            <div className="bg-slate-900/60 border border-slate-800 rounded-2xl p-4 shadow-xl backdrop-blur-sm space-y-3">
              <div className="flex items-center space-x-2 pb-2 border-b border-slate-800/80">
                {/* Editable Username */}
                <div className="flex items-center space-x-1 bg-slate-950 px-2.5 py-1 rounded-lg border border-slate-800 text-xs">
                  <span className="text-slate-500 font-semibold">u/</span>
                  <input
                    type="text"
                    value={cleanUserHandle(username)}
                    onChange={(e) => setUsername(e.target.value.trim())}
                    placeholder="username"
                    className="bg-transparent text-slate-200 outline-none w-28 font-medium"
                  />
                </div>

                <span className="text-slate-600 text-xs">to</span>

                {/* Community Selector */}
                <div className="relative">
                  <select
                    value={selectedCommunity}
                    onChange={(e) => setSelectedCommunity(e.target.value)}
                    className="appearance-none bg-slate-950 text-xs text-orange-400 font-semibold px-3 py-1 pr-7 rounded-lg border border-slate-800 outline-none cursor-pointer focus:border-orange-500"
                  >
                    <option value="r/kubernetes">r/kubernetes</option>
                    <option value="r/gitops">r/gitops</option>
                    <option value="r/azure">r/azure</option>
                    <option value="r/terraform">r/terraform</option>
                    <option value="r/general">r/general</option>
                  </select>
                  <ChevronDown className="w-3.5 h-3.5 text-slate-500 absolute right-2 top-2 pointer-events-none" />
                </div>
              </div>

              {/* Text Area */}
              <form onSubmit={handleCreatePost} className="space-y-3">
                <textarea
                  rows={3}
                  value={postContent}
                  onChange={(e) => setPostContent(e.target.value)}
                  placeholder="Create a post..."
                  className="w-full bg-slate-950/80 border border-slate-800 focus:border-orange-500/80 rounded-xl p-3 text-sm text-slate-100 placeholder-slate-500 outline-none resize-none transition-all"
                />

                {/* Submit Row */}
                <div className="flex items-center justify-end pt-1">
                  <button
                    type="submit"
                    disabled={isPosting || !postContent.trim()}
                    className="px-5 py-1.5 bg-gradient-to-r from-orange-500 to-amber-600 hover:from-orange-400 hover:to-amber-500 disabled:opacity-40 text-white text-xs font-bold rounded-full shadow-md shadow-orange-500/20 flex items-center space-x-1.5 transition-all"
                  >
                    {isPosting ? (
                      <>
                        <RefreshCw className="w-3.5 h-3.5 animate-spin" />
                        <span>Posting...</span>
                      </>
                    ) : (
                      <>
                        <Send className="w-3.5 h-3.5" />
                        <span>Post</span>
                      </>
                    )}
                  </button>
                </div>
              </form>
            </div>

            {/* Filter Active Bar */}
            {activeCommunity !== "all" && (
              <div className="flex items-center justify-between px-3 py-1.5 bg-slate-900/60 border border-slate-800 rounded-xl text-xs text-slate-300">
                <span>Showing posts in <strong>{activeCommunity}</strong></span>
                <button
                  onClick={() => setActiveCommunity("all")}
                  className="text-orange-400 hover:text-white underline text-[11px]"
                >
                  Show All
                </button>
              </div>
            )}

            {/* Reddit-Style Post Feed Cards */}
            <div className="space-y-3">
              {filteredPosts.length === 0 ? (
                <div className="bg-slate-900/40 border border-dashed border-slate-800 rounded-2xl p-10 text-center space-y-2">
                  <MessageSquareCode className="w-8 h-8 text-slate-600 mx-auto" />
                  <div className="text-sm font-medium text-slate-300">No posts in this feed yet</div>
                  <p className="text-xs text-slate-500 max-w-sm mx-auto">
                    Be the first to post using the box above.
                  </p>
                </div>
              ) : (
                filteredPosts.map((post) => {
                  const isUpvoted = upvotedPostIds.has(post.id);
                  const isDownvoted = downvotedPostIds.has(post.id);
                  const isUpvoting = upvotingIds[post.id] || false;
                  const isSaved = savedPostIds.has(post.id);

                  return (
                    <article
                      key={post.id}
                      className="bg-slate-900/70 border border-slate-800 hover:border-slate-700/80 rounded-xl overflow-hidden transition-all duration-200 hover:shadow-lg flex flex-row group"
                    >
                      {/* Signature Reddit Vertical Voting Column */}
                      <div className="w-10 sm:w-11 bg-slate-950/40 border-r border-slate-800/60 p-1.5 flex flex-col items-center justify-start pt-3 space-y-1 flex-shrink-0">
                        {/* Upvote Button */}
                        <button
                          onClick={() => handleUpvote(post.id)}
                          disabled={isUpvoting}
                          className={`p-1 rounded hover:bg-slate-800 transition-colors ${
                            isUpvoted
                              ? "text-orange-500 fill-orange-500"
                              : "text-slate-400 hover:text-orange-400"
                          }`}
                          title="Upvote"
                        >
                          <ArrowUp
                            className={`w-4 h-4 ${
                              isUpvoted ? "stroke-[3] text-orange-500" : ""
                            } ${isUpvoting ? "animate-bounce text-orange-400" : ""}`}
                          />
                        </button>

                        {/* Bold Vote Count */}
                        <span
                          className={`font-bold text-xs font-mono select-none ${
                            isUpvoted
                              ? "text-orange-500"
                              : isDownvoted
                              ? "text-blue-500"
                              : "text-slate-200"
                          }`}
                        >
                          {post.upvotes || 0}
                        </span>

                        {/* Downvote Button */}
                        <button
                          onClick={() => handleDownvote(post.id)}
                          className={`p-1 rounded hover:bg-slate-800 transition-colors ${
                            isDownvoted
                              ? "text-blue-500 fill-blue-500"
                              : "text-slate-500 hover:text-blue-400"
                          }`}
                          title="Downvote"
                        >
                          <ArrowDown
                            className={`w-4 h-4 ${
                              isDownvoted ? "stroke-[3] text-blue-500" : ""
                            }`}
                          />
                        </button>
                      </div>

                      {/* Main Card Content */}
                      <div className="flex-1 p-3.5 sm:p-4 space-y-2 min-w-0">
                        {/* Reddit Card Header */}
                        <div className="flex items-center space-x-1.5 text-xs text-slate-400 flex-wrap">
                          <span
                            onClick={() => setActiveCommunity(post.community)}
                            className="font-bold text-slate-200 hover:underline cursor-pointer"
                          >
                            {post.community}
                          </span>
                          <span className="text-slate-600">•</span>
                          <span>Posted by u/{cleanUserHandle(post.username)}</span>
                          <span className="text-slate-600">•</span>
                          <span>{getRelativeTime(post.created_at)}</span>
                        </div>

                        {/* Post Body */}
                        <p className="text-sm sm:text-base text-slate-100 font-normal leading-relaxed whitespace-pre-wrap break-words py-1">
                          {post.content}
                        </p>

                        {/* Reddit Bottom Action Bar */}
                        <div className="flex items-center space-x-1 pt-2 border-t border-slate-800/40 text-slate-400 text-xs font-semibold">
                          <button className="flex items-center space-x-1.5 px-2.5 py-1.5 rounded hover:bg-slate-800/80 hover:text-slate-200 transition-colors">
                            <MessageSquare className="w-3.5 h-3.5" />
                            <span>Comments</span>
                          </button>
                          <button
                            onClick={() => handleShare(post.id)}
                            className="flex items-center space-x-1.5 px-2.5 py-1.5 rounded hover:bg-slate-800/80 hover:text-slate-200 transition-colors"
                          >
                            <Share2 className="w-3.5 h-3.5" />
                            <span>{copiedPostId === post.id ? "Copied Link!" : "Share"}</span>
                          </button>
                          <button
                            onClick={() => handleSave(post.id)}
                            className={`flex items-center space-x-1.5 px-2.5 py-1.5 rounded hover:bg-slate-800/80 hover:text-slate-200 transition-colors ${
                              isSaved ? "text-orange-400" : ""
                            }`}
                          >
                            <Bookmark
                              className={`w-3.5 h-3.5 ${isSaved ? "fill-orange-400 text-orange-400" : ""}`}
                            />
                            <span>{isSaved ? "Saved" : "Save"}</span>
                          </button>
                        </div>
                      </div>
                    </article>
                  );
                })
              )}
            </div>
          </main>

          {/* ======================================================== */}
          {/* RIGHT COLUMN: Trending & Architecture Widget (3 col)     */}
          {/* ======================================================== */}
          <aside className="lg:col-span-3 space-y-4">
            {/* Trending Communities */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-4 shadow-xl backdrop-blur-sm">
              <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-300 mb-3">
                <TrendingUp className="w-4 h-4 text-orange-400" />
                <span>Trending Communities</span>
              </div>
              <div className="space-y-2">
                <div
                  onClick={() => setActiveCommunity("r/kubernetes")}
                  className="cursor-pointer hover:bg-slate-800/40 p-2 rounded-xl transition-colors"
                >
                  <div className="text-xs font-semibold text-blue-400">r/kubernetes</div>
                  <div className="text-[11px] text-slate-400">Declarative cluster scaling & pods</div>
                </div>
                <div
                  onClick={() => setActiveCommunity("r/gitops")}
                  className="cursor-pointer hover:bg-slate-800/40 p-2 rounded-xl transition-colors"
                >
                  <div className="text-xs font-semibold text-emerald-400">r/gitops</div>
                  <div className="text-[11px] text-slate-400">ArgoCD & pull-based delivery</div>
                </div>
                <div
                  onClick={() => setActiveCommunity("r/azure")}
                  className="cursor-pointer hover:bg-slate-800/40 p-2 rounded-xl transition-colors"
                >
                  <div className="text-xs font-semibold text-sky-400">r/azure</div>
                  <div className="text-[11px] text-slate-400">AKS, ACR & cloud infrastructure</div>
                </div>
                <div
                  onClick={() => setActiveCommunity("r/terraform")}
                  className="cursor-pointer hover:bg-slate-800/40 p-2 rounded-xl transition-colors"
                >
                  <div className="text-xs font-semibold text-purple-400">r/terraform</div>
                  <div className="text-[11px] text-slate-400">Modular HCL & remote state</div>
                </div>
              </div>
            </div>

            {/* Architecture Under The Hood Widget (Collapsible for Demos) */}
            <div className="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-4 shadow-xl backdrop-blur-sm">
              <div
                onClick={() => setShowArchWidget(!showArchWidget)}
                className="flex items-center justify-between cursor-pointer select-none"
              >
                <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-slate-300">
                  <Activity className="w-4 h-4 text-orange-400" />
                  <span>Architecture Status</span>
                </div>
                <ChevronDown
                  className={`w-4 h-4 text-slate-400 transition-transform ${
                    showArchWidget ? "rotate-180" : ""
                  }`}
                />
              </div>

              {showArchWidget && (
                <div className="mt-3 pt-3 border-t border-slate-800/80 space-y-2.5 text-xs">
                  <div className="flex items-center justify-between">
                    <span className="text-slate-400 flex items-center space-x-1.5">
                      <Server className="w-3.5 h-3.5 text-cyan-400" />
                      <span>web-client</span>
                    </span>
                    <span className="font-mono text-[11px] text-slate-300">Next.js :3000</span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-slate-400 flex items-center space-x-1.5">
                      <Zap className="w-3.5 h-3.5 text-amber-400" />
                      <span>core-api</span>
                    </span>
                    <span className="font-mono text-[11px] text-slate-300">FastAPI :8000</span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-slate-400 flex items-center space-x-1.5">
                      <ShieldCheck className="w-3.5 h-3.5 text-purple-400" />
                      <span>notify-service</span>
                    </span>
                    <span className="font-mono text-[11px] text-slate-300">Audit :8001</span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-slate-400 flex items-center space-x-1.5">
                      <Database className="w-3.5 h-3.5 text-blue-400" />
                      <span>PostgreSQL 15</span>
                    </span>
                    <span className="font-mono text-[11px] text-emerald-400">
                      {systemHealth.postgres}
                    </span>
                  </div>

                  <div className="flex items-center justify-between">
                    <span className="text-slate-400 flex items-center space-x-1.5">
                      <HardDrive className="w-3.5 h-3.5 text-emerald-400" />
                      <span>Redis 7</span>
                    </span>
                    <span className="font-mono text-[11px] text-emerald-400">
                      {systemHealth.redis}
                    </span>
                  </div>

                  <div className="pt-2 border-t border-slate-800/60 text-[10px] text-slate-500 font-mono">
                    <div>Scraped by kube-prometheus:</div>
                    <div className="text-slate-400">:8000/metrics · :8001/metrics</div>
                  </div>
                </div>
              )}
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
}
