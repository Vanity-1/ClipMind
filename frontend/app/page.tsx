"use client";

import { useState, useEffect, useRef, useCallback, useMemo, useSyncExternalStore } from "react";
import LoginModal from "@/components/LoginModal";
import DemoFlowModal from "@/components/DemoFlowModal";
import SourcesPanel from "@/components/SourcesPanel";
import ChatPanel from "@/components/ChatPanel";
import DouyinPanel from "@/components/DouyinPanel";
import SettingsPanel from "@/components/SettingsPanel";
import ModelMarketPanel from "@/components/ModelMarketPanel";
import RagManagementPanel from "@/components/RagManagementPanel";
import ThemeToggle from "@/components/ThemeToggle";
import { UserInfo, authApi, douyinApi } from "@/lib/api";

const AUTH_CHANGE_EVENT = "bili-auth-change";

// 安全访问 localStorage：在禁用 Cookie / 隐私模式 / 权限受限时降级为内存存储
const memoryFallback: Record<string, string> = {};
function safeGetItem(key: string): string {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return memoryFallback[key] || "";
  }
}
function safeSetItem(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    memoryFallback[key] = value;
  }
}
function safeRemoveItem(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    delete memoryFallback[key];
  }
}

function readStoredAuth() {
  if (typeof window === "undefined") return "";
  const biliSession = safeGetItem("bili_session");
  const biliUser = safeGetItem("bili_user");
  const douyinSession = safeGetItem("douyin_session");
  const douyinUser = safeGetItem("douyin_user");
  return JSON.stringify({
    bili_session: biliSession || "",
    bili_user: biliUser || "",
    douyin_session: douyinSession || "",
    douyin_user: douyinUser || "",
  });
}

function subscribeStoredAuth(callback: () => void) {
  if (typeof window === "undefined") return () => {};
  window.addEventListener("storage", callback);
  window.addEventListener(AUTH_CHANGE_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(AUTH_CHANGE_EVENT, callback);
  };
}

function notifyAuthChanged() {
  window.dispatchEvent(new Event(AUTH_CHANGE_EVENT));
}

export default function Home() {
  const authSnapshot = useSyncExternalStore(subscribeStoredAuth, readStoredAuth, () => "");
  const auth = useMemo(() => {
    if (!authSnapshot) return null;
    try {
      const parsed = JSON.parse(authSnapshot) as {
        bili_session: string;
        bili_user: string;
        douyin_session: string;
        douyin_user: string;
      };
      return parsed;
    } catch {
      return null;
    }
  }, [authSnapshot]);

  const biliLoggedIn = !!(auth?.bili_session && auth?.bili_user);
  const douyinLoggedIn = !!(auth?.douyin_session && auth?.douyin_user);
  const anyLoggedIn = biliLoggedIn || douyinLoggedIn;

  const [showLogin, setShowLogin] = useState(false);
  const [loginDefaultTab, setLoginDefaultTab] = useState<"bilibili" | "douyin">("bilibili");
  const [showDemo, setShowDemo] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showModelMarket, setShowModelMarket] = useState(false);
  const [showRagManagement, setShowRagManagement] = useState(false);
  const [statsKey, setStatsKey] = useState(0);
  const [ragDataSignal, setRagDataSignal] = useState(0);
  // 按平台分别管理 folderIds，避免跨平台错乱
  const [biliFolderIds, setBiliFolderIds] = useState<number[]>([]);
  const [douyinFolderIds, setDouyinFolderIds] = useState<number[]>([]);
  const [platform, setPlatform] = useState<"bilibili" | "douyin" | "both">("bilibili");

  // 切换平台模式时清空选中的收藏夹，避免跨平台 folder_ids 错乱
  const switchPlatform = useCallback((next: "bilibili" | "douyin" | "both") => {
    setBiliFolderIds([]);
    setDouyinFolderIds([]);
    setPlatform(next);
  }, []);

  // 拖拽调整宽度
  const [leftWidth, setLeftWidth] = useState(320);
  const [isDragging, setIsDragging] = useState(false);
  const containerRef = useRef<HTMLElement>(null);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!isDragging || !containerRef.current) return;
    const containerRect = containerRef.current.getBoundingClientRect();
    const newWidth = e.clientX - containerRect.left;
    const min = 200;
    const max = containerRect.width * 0.5;
    setLeftWidth(Math.max(min, Math.min(max, newWidth)));
  }, [isDragging]);

  const handleMouseUp = useCallback(() => {
    setIsDragging(false);
  }, []);

  useEffect(() => {
    if (isDragging) {
      window.addEventListener("mousemove", handleMouseMove);
      window.addEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    } else {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isDragging, handleMouseMove, handleMouseUp]);

  const onBiliLogin = (sid: string, info: UserInfo) => {
    setShowLogin(false);
    safeSetItem("bili_session", sid);
    safeSetItem("bili_user", info.uname);
    notifyAuthChanged();
  };

  const onDouyinLogin = (sid: string, user: { uid: string; nickname: string; avatar: string }) => {
    setShowLogin(false);
    if (sid) safeSetItem("douyin_session", sid);
    safeSetItem("douyin_user", user.nickname || user.uid || "抖音用户");
    notifyAuthChanged();
  };

  const onLogout = () => {
    if (biliLoggedIn && auth?.bili_session) {
      authApi.logout(auth.bili_session).catch(() => {});
      safeRemoveItem("bili_session");
      safeRemoveItem("bili_user");
    }
    if (douyinLoggedIn) {
      // 主动调用抖音登出接口清理后端 session，不依赖 DouyinPanel 是否挂载
      douyinApi.logout().catch(() => {});
      safeRemoveItem("douyin_session");
      safeRemoveItem("douyin_user");
    }
    notifyAuthChanged();
  };

  return (
    <div className="app-shell">
      <header className="app-topbar">
        <div className="brand">
          <div className="brand-mark">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M4 6h16M4 12h16M4 18h10" />
            </svg>
          </div>
          <div>
            <span className="brand-title">ClipMind</span>
            <span className="brand-subtitle">Save • Learn • Ask</span>
          </div>
        </div>

        <div className="topbar-actions">
          <ThemeToggle />
          <button onClick={() => setShowSettings(true)} className="btn-icon" title="应用设置" aria-label="设置">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
          </button>
          <button onClick={() => setShowModelMarket(true)} className="btn-icon" title="模型市场" aria-label="模型市场">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
              <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
              <line x1="12" y1="22.08" x2="12" y2="12" />
            </svg>
          </button>
          {anyLoggedIn && (
            <button onClick={() => setShowRagManagement(true)} className="btn-icon" title="RAG 入库管理" aria-label="RAG管理">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                <ellipse cx="12" cy="5" rx="9" ry="3" />
                <path d="M3 5v14a9 3 0 0 0 18 0V5" />
                <path d="M3 12a9 3 0 0 0 18 0" />
              </svg>
            </button>
          )}
          {anyLoggedIn ? (
            <>
              <span className="user-chip">
                {biliLoggedIn && (
                  <>
                    <span>B站</span>
                    <strong>{auth?.bili_user}</strong>
                  </>
                )}
                {biliLoggedIn && douyinLoggedIn && <span style={{ margin: "0 6px", opacity: 0.4 }}>·</span>}
                {douyinLoggedIn && (
                  <>
                    <span>抖音</span>
                    <strong>{auth?.douyin_user}</strong>
                  </>
                )}
              </span>
              <button onClick={onLogout} className="btn-icon" title="退出登录">
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
              </button>
            </>
          ) : (
            <div style={{ display: "flex", gap: 8 }}>
              <button onClick={() => { setLoginDefaultTab("bilibili"); setShowLogin(true); }} className="btn btn-primary" style={{ fontSize: 13 }}>
                登录B站
              </button>
              <button onClick={() => { setLoginDefaultTab("douyin"); setShowLogin(true); }} className="btn btn-primary" style={{ fontSize: 13, background: "#fe2c55" }}>
                登录抖音
              </button>
            </div>
          )}
        </div>
      </header>

      <main className="app-main">
        {!anyLoggedIn ? (
          <section className="hero">
            <div className="hero-content">
              <span className="hero-kicker">让你的收藏夹不再吃灰</span>
              <h1 className="hero-title">把&ldquo;收藏&rdquo;变成真正可用的知识</h1>
              <p className="hero-desc">
                很多人收藏了大量学习视频，却迟迟没看、没整理、也找不到重点。<br />
                这里把碎片化内容接入 AI：自动提炼、语义检索、对话式回顾，让收藏真正提升效率。
              </p>

              <div className="hero-actions">
                <button className="btn btn-primary btn-lg" onClick={() => { setLoginDefaultTab("bilibili"); setShowLogin(true); }}>
                  扫码登录开始构建
                </button>
                <button className="btn btn-outline" onClick={() => setShowDemo(true)}>
                  体验检索流程
                </button>
              </div>
            </div>

            <div className="hero-features">
              <div className="pipeline-row">
                {[
                  { icon: "1", title: "同步", desc: "接入收藏夹" },
                  { icon: "2", title: "提炼", desc: "整理要点" },
                  { icon: "3", title: "检索", desc: "语义查找" },
                  { icon: "4", title: "回顾", desc: "对话复习" },
                ].map((item, i) => (
                  <div key={i} className="pipeline-card">
                    <span className="pipeline-icon">{item.icon}</span>
                    <div className="pipeline-text">
                      <strong>{item.title}</strong>
                      <span>{item.desc}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </section>
        ) : (
          <section className="workspace" ref={containerRef}>
            {/* === B站模式 === */}
            {platform === "bilibili" && (
              <>
                <aside className="panel panel-sources" style={{ width: leftWidth, flexShrink: 0 }} aria-label="收藏夹面板">
                  <SourcesPanel
                    sessionId={auth?.bili_session || ""}
                    onBuildDone={() => setStatsKey((v) => v + 1)}
                    onSelectionChange={setBiliFolderIds}
                    refreshSignal={ragDataSignal}
                  />
                </aside>

                <div className="resizer" onMouseDown={handleMouseDown} style={{ cursor: "col-resize" }} />

                <section className="panel panel-chat" style={{ flex: 1 }}>
                  <ChatPanel statsKey={statsKey} sessionId={auth?.bili_session || ""} folderIds={biliFolderIds} platform="bilibili" />
                </section>

                <div style={{ position: "absolute", top: 12, left: leftWidth + 30, zIndex: 10 }}>
                  <div className="platform-toggle">
                    <button className="platform-btn active">📺 B站</button>
                    <button className="platform-btn douyin" onClick={() => switchPlatform("douyin")}>🎵 抖音</button>
                    <button className="platform-btn both" onClick={() => switchPlatform("both")}>双面板</button>
                  </div>
                </div>
              </>
            )}

            {/* === 抖音模式 === */}
            {platform === "douyin" && (
              <>
                <aside className="panel panel-sources" style={{ width: leftWidth, flexShrink: 0 }} aria-label="抖音收藏面板">
                  <DouyinPanel onSelectionChange={setDouyinFolderIds} />
                </aside>

                <div className="resizer" onMouseDown={handleMouseDown} style={{ cursor: "col-resize" }} />

                <section className="panel panel-chat" style={{ flex: 1 }}>
                  <ChatPanel statsKey={statsKey} sessionId={auth?.douyin_session || ""} folderIds={douyinFolderIds} platform="douyin" />
                </section>

                <div style={{ position: "absolute", top: 12, left: leftWidth + 30, zIndex: 10 }}>
                  <div className="platform-toggle">
                    <button className="platform-btn" onClick={() => switchPlatform("bilibili")}>📺 B站</button>
                    <button className="platform-btn douyin active">🎵 抖音</button>
                    <button className="platform-btn both" onClick={() => switchPlatform("both")}>双面板</button>
                  </div>
                </div>
              </>
            )}

            {/* === 双面板模式 === */}
            {platform === "both" && (
              <>
                <aside className="panel panel-sources" style={{ width: leftWidth, flexShrink: 0, display: "flex", flexDirection: "column" }} aria-label="收藏夹面板">
                  <div style={{ flex: 1, overflow: "auto", borderBottom: "1px solid var(--border)" }}>
                    <SourcesPanel
                      sessionId={auth?.bili_session || ""}
                      onBuildDone={() => setStatsKey((v) => v + 1)}
                      onSelectionChange={setBiliFolderIds}
                      refreshSignal={ragDataSignal}
                    />
                  </div>
                  <div style={{ flex: 1, overflow: "auto" }}>
                    <DouyinPanel onSelectionChange={setDouyinFolderIds} />
                  </div>
                </aside>

                <div className="resizer" onMouseDown={handleMouseDown} style={{ cursor: "col-resize" }} />

                <section className="panel panel-chat" style={{ flex: 1 }}>
                  <ChatPanel statsKey={statsKey} sessionId="" folderIds={[...biliFolderIds, ...douyinFolderIds]} platform={undefined} />
                </section>

                <div style={{ position: "absolute", top: 12, left: leftWidth + 30, zIndex: 10 }}>
                  <div className="platform-toggle">
                    <button className="platform-btn" onClick={() => switchPlatform("bilibili")}>📺 B站</button>
                    <button className="platform-btn douyin" onClick={() => switchPlatform("douyin")}>🎵 抖音</button>
                    <button className="platform-btn both active">双面板</button>
                  </div>
                </div>
              </>
            )}
          </section>
        )}
      </main>

      <footer className="app-footer">
        <p>ClipMind © 2026 · Dark Tech Edition · AI-Powered Knowledge Base</p>
      </footer>

      <LoginModal
        isOpen={showLogin}
        onClose={() => setShowLogin(false)}
        onBiliSuccess={onBiliLogin}
        onDouyinSuccess={onDouyinLogin}
        defaultTab={loginDefaultTab}
      />
      <DemoFlowModal isOpen={showDemo} onClose={() => setShowDemo(false)} />
      <SettingsPanel
        isOpen={showSettings}
        onClose={() => setShowSettings(false)}
        onOpenModelMarket={() => {
          setShowSettings(false);
          setShowModelMarket(true);
        }}
      />
      <ModelMarketPanel isOpen={showModelMarket} onClose={() => setShowModelMarket(false)} />
      <RagManagementPanel
        isOpen={showRagManagement}
        onClose={() => setShowRagManagement(false)}
        biliSessionId={auth?.bili_session || ""}
        douyinSessionId={auth?.douyin_session || undefined}
        onDataChanged={() => setRagDataSignal((v) => v + 1)}
      />
    </div>
  );
}


