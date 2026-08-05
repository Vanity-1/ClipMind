"use client";

import { useState, useEffect, useRef } from "react";
import LoginModal from "./LoginModal";
import {
  douyinApi,
  DouyinFolderInfo,
  DouyinFolderVideo,
  DouyinBatchIngestResponse,
  DouyinBatchIngestStatus,
  IngestStreamEvent,
  openExternal,
} from "@/lib/api";

interface DouyinPanelProps {
  onSelectionChange?: (folderIds: number[]) => void;
}

/** 从 localStorage 读取抖音 session_id，用于收藏夹相关接口鉴权 */
function getDouyinSessionId(): string | undefined {
  if (typeof window === "undefined") return undefined;
  try {
    return localStorage.getItem("douyin_session") || undefined;
  } catch {
    return undefined;
  }
}

export default function DouyinPanel({ onSelectionChange }: DouyinPanelProps) {
  const [url, setUrl] = useState("");
  const [parsing, setParsing] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [preview, setPreview] = useState<{video_id:string;title:string;author:string;duration:number;cover_url:string}|null>(null);
  const [msg, setMsg] = useState("");
  const [cookie, setCookie] = useState("");
  const [cookieLogging, setCookieLogging] = useState(false);
  const [showLogin, setShowLogin] = useState(false);
  const [douyinUser, setDouyinUser] = useState<{uid?:string;nickname?:string}|null>(null);
  const [syncing, setSyncing] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [syncResults, setSyncResults] = useState<{like: string; collect: string} | null>(null);
  const [showSyncLimitModal, setShowSyncLimitModal] = useState(false);
  const [batchIngesting, setBatchIngesting] = useState(false);
  const [batchResults, setBatchResults] = useState<DouyinBatchIngestResponse | null>(null);
  const [batchProgress, setBatchProgress] = useState<DouyinBatchIngestStatus | null>(null);
  const batchPollRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 单视频入库 SSE 步骤进度
  const [ingestSteps, setIngestSteps] = useState<IngestStreamEvent[] | null>(null);
  const ingestAbortRef = useRef<AbortController | null>(null);
  const [syncLimit, setSyncLimit] = useState<number>(() => {
    if (typeof window === "undefined") return 500;
    try {
      const saved = localStorage.getItem("douyin_sync_limit");
      return saved ? parseInt(saved, 10) : 500;
    } catch {
      return 500;
    }
  });
  // Folder management
  const [folders, setFolders] = useState<DouyinFolderInfo[]>([]);
  const [expandedFolder, setExpandedFolder] = useState<number | null>(null);
  const [folderVideos, setFolderVideos] = useState<DouyinFolderVideo[]>([]);

  // 挂载时初始化：检查登录状态，登录后由 checkAuth 内部加载文件夹
  useEffect(() => {
    (async () => {
      await checkAuth();
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // folders 变化时通知父组件当前选中的 folder_ids
  useEffect(() => {
    if (!onSelectionChange) return;
    const selected = folders.filter((f) => f.is_selected).map((f) => f.folder_id);
    onSelectionChange(selected);
  }, [folders, onSelectionChange]);

  useEffect(() => {
    if (!showSyncLimitModal) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape" && !syncing) setShowSyncLimitModal(false); };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [showSyncLimitModal, syncing]);

  // 组件卸载时清理轮询定时器与 SSE 流
  useEffect(() => {
    return () => {
      if (batchPollRef.current) {
        clearTimeout(batchPollRef.current);
        batchPollRef.current = null;
      }
      if (ingestAbortRef.current) {
        ingestAbortRef.current.abort();
        ingestAbortRef.current = null;
      }
    };
  }, []);

  function persistDouyinLogin(sessionId: string | undefined, nickname: string, uid?: string) {
    if (typeof window === "undefined") return;
    try {
      if (sessionId) localStorage.setItem("douyin_session", sessionId);
      localStorage.setItem("douyin_user", nickname || uid || "抖音用户");
    } catch {
      // 隐私模式或权限受限时忽略写入失败，登录态仍保留在组件内
    }
    window.dispatchEvent(new Event("bili-auth-change"));
  }

  function handleQRLoginSuccess(sessionId: string, user: {uid:string;nickname:string;avatar:string}) {
    setDouyinUser({uid: user.uid, nickname: user.nickname});
    setShowLogin(false);
    setLoginError("");
    setMsg("扫码登录成功 - " + user.nickname);
    persistDouyinLogin(sessionId, user.nickname, user.uid);
  }

  async function checkAuth(): Promise<boolean> {
    try {
      const r = await douyinApi.getAuthStatus();
      if (r.logged_in) {
        setDouyinUser({uid: r.uid, nickname: r.nickname});
        // 同步本地登录态，确保顶部状态正确显示
        if (typeof window !== "undefined") {
          try {
            const stored = localStorage.getItem("douyin_user");
            if (!stored) {
              localStorage.setItem("douyin_user", r.nickname || r.uid || "抖音用户");
              window.dispatchEvent(new Event("bili-auth-change"));
            }
          } catch {
            // 隐私模式或权限受限时忽略
          }
        }
        loadFolders();
        return true;
      }
      return false;
    } catch (e) {
      console.error("检查抖音登录状态失败", e);
      setMsg("检查抖音登录状态失败");
      return false;
    }
  }

  async function loadFolders() {
    try {
      const r = await douyinApi.listFolders(getDouyinSessionId());
      setFolders(r);
    } catch (e: any) {
      console.error("加载抖音收藏夹失败", e);
      if (e?.status === 401) {
        // session 过期，清除旧登录态，提示重新登录
        try { localStorage.removeItem("douyin_session"); } catch {}
        setDouyinUser(null);
        setMsg("抖音登录已过期，请重新扫码登录");
      } else {
        setMsg("加载抖音收藏夹失败，请稍后重试");
      }
    }
  }

  async function toggleFolderExpand(folderId: number) {
    if (expandedFolder === folderId) {
      setExpandedFolder(null);
      setFolderVideos([]);
      return;
    }
    setExpandedFolder(folderId);
    try {
      const r = await douyinApi.getFolderVideos(folderId, getDouyinSessionId());
      setFolderVideos(r.videos);
    } catch (e) {
      console.error("加载文件夹视频失败", e);
      setFolderVideos([]);
      setMsg("加载文件夹视频失败，可尝试重新展开");
    }
  }

  async function handleFolderSelect(folderId: number, selectAll: boolean) {
    try {
      await douyinApi.toggleFolderSelect(folderId, selectAll);
      await loadFolders();
      // 已展开时刷新视频列表，避免误用 toggle 折叠
      if (expandedFolder === folderId) {
        try {
          const vids = await douyinApi.getFolderVideos(folderId, getDouyinSessionId());
          setFolderVideos(vids.videos);
        } catch (e) {
          console.error("刷新文件夹视频失败", e);
          setMsg("刷新文件夹视频失败");
        }
      }
    } catch (e) {
      console.error("切换文件夹选择失败", e);
      setMsg("切换文件夹选择失败");
    }
  }

  async function handleVideoSelect(videoId: string, folderId: number, selected: boolean) {
    try {
      await douyinApi.toggleVideoSelect(videoId, folderId, selected);
      setFolderVideos(prev => prev.map(v => v.video_id === videoId ? {...v, is_selected: selected} : v));
    } catch (e) {
      console.error("切换视频选择失败", e);
      setMsg("切换视频选择失败");
    }
  }

  async function handleCookieLogin() {
    if (!cookie.trim()) return;
    setCookieLogging(true);
    setMsg("");
    setLoginError("");
    try {
      const r = await douyinApi.loginWithCookie({ cookie: cookie.trim() });
      setMsg(r.message);
      if (r.success) {
        setDouyinUser({uid: r.uid, nickname: r.nickname});
        setCookie("");
        persistDouyinLogin(undefined, r.nickname, r.uid);
      } else {
        setLoginError(r.message);
      }
    } catch (e) {
      const errMsg = e instanceof Error ? e.message : "登录失败";
      setMsg(errMsg);
      setLoginError(errMsg);
    } finally {
      setCookieLogging(false);
    }
  }

  async function handleLogout() {
    try {
      await douyinApi.logout();
    } catch (e) {
      console.error("logout failed", e);
    } finally {
      setDouyinUser(null);
      setFolders([]);
      setFolderVideos([]);
      setExpandedFolder(null);
      // 同步清除本地登录态，通知顶部状态刷新
      if (typeof window !== "undefined") {
        try {
          localStorage.removeItem("douyin_session");
          localStorage.removeItem("douyin_user");
        } catch {
          // 隐私模式或权限受限时忽略
        }
        window.dispatchEvent(new Event("bili-auth-change"));
      }
      setMsg("已退出登录");
    }
  }

  function handleSyncFavorites() {
    setShowSyncLimitModal(true);
  }

  async function executeSyncFavorites() {
    setShowSyncLimitModal(false);
    const clampedLimit = Math.max(1, Math.min(5000, syncLimit || 500));
    setSyncLimit(clampedLimit);
    try {
      localStorage.setItem("douyin_sync_limit", String(clampedLimit));
    } catch {
      // 隐私模式或权限受限时忽略写入失败
    }

    setSyncing(true);
    setMsg("");
    setSyncResults(null);
    try {
      const r = await douyinApi.syncFav(clampedLimit);
      // 优先检查后端显式失败（success=false），直接显示后端透传的失败原因
      if (r.success === false) {
        setMsg(r.message || "同步失败");
        return;
      }
      if (r.first_sync) {
        setMsg("首次同步，正在抓取全部收藏数据，请耐心等待...");
      }
      const likeMsg = r.like ? `同步喜欢 ${r.like.synced} 个，新增 ${r.like.new} 个` : "";
      const collectMsg = r.collect_flat ? `同步收藏 ${r.collect_flat.synced} 个，新增 ${r.collect_flat.new} 个` : "";
      if (likeMsg || collectMsg) {
        setSyncResults({ like: likeMsg, collect: collectMsg });
        setMsg("同步完成");
      } else {
        setMsg("没有找到喜欢或收藏的视频");
      }
      loadFolders();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "同步失败");
    } finally {
      setSyncing(false);
    }
  }

  async function handleParse() {
    if (!url.trim()) return;
    setParsing(true);
    setMsg("");
    setPreview(null);
    try {
      const r = await douyinApi.parse({ url: url.trim() });
      setPreview(r);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "解析失败");
    } finally {
      setParsing(false);
    }
  }

  async function handleIngest() {
    if (!preview) return;
    setIngesting(true);
    setMsg("");
    setIngestSteps([]);
    const controller = new AbortController();
    ingestAbortRef.current = controller;
    try {
      await douyinApi.ingestStream(
        {
          video_id: preview.video_id,
          title: preview.title,
          description: "",
          author: preview.author,
          duration: preview.duration,
          cover_url: preview.cover_url,
        },
        (evt) => {
          setIngestSteps((prev) => [...(prev ?? []), evt]);
          if (evt.event === "done") {
            setMsg(evt.message || "入库成功");
          } else if (evt.event === "error") {
            setMsg(evt.message || "入库失败");
          }
        },
        undefined,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        setPreview(null);
        setUrl("");
        loadFolders();
      }
    } catch (e) {
      if (controller.signal.aborted) return;
      setMsg(e instanceof Error ? e.message : "入库失败");
    } finally {
      setIngesting(false);
      if (ingestAbortRef.current === controller) {
        ingestAbortRef.current = null;
      }
    }
  }

  async function handleBatchIngest(folderId?: number) {
    setBatchIngesting(true);
    setMsg("");
    setBatchResults(null);
    setBatchProgress(null);
    if (batchPollRef.current) {
      clearTimeout(batchPollRef.current);
      batchPollRef.current = null;
    }
    try {
      // 收集当前文件夹中勾选的视频ID，按勾选数量入库
      const selectedVideoIds = folderId
        ? folderVideos.filter(v => v.is_selected && !v.is_processed).map(v => v.video_id)
        : [];
      // 有勾选视频时按 video_ids 入库；无勾选时按文件夹全部待入库视频入库（不限制20）
      const payload = selectedVideoIds.length > 0
        ? { folder_id: folderId, video_ids: selectedVideoIds }
        : { folder_id: folderId, limit: 5000 };
      const r = await douyinApi.ingestBatch(payload);
      if (!r.task_id) {
        // 后端立即返回：没有待入库视频
        setBatchResults(r);
        setMsg(r.message || "没有待入库的视频");
        return;
      }
      // 启动 1s 轮询
      const taskId = r.task_id;
      const poll = async () => {
        let s: DouyinBatchIngestStatus;
        try {
          s = await douyinApi.getIngestBatchStatus(taskId);
        } catch (e) {
          setBatchIngesting(false);
          setMsg(e instanceof Error ? e.message : "查询批量入库状态失败");
          return;
        }
        setBatchProgress(s);
        if (s.status === "running" || s.status === "pending") {
          batchPollRef.current = setTimeout(poll, 1000);
        } else {
          setBatchIngesting(false);
          if (s.status === "completed") {
            setMsg(s.message || `批量入库完成：成功 ${s.succeeded ?? 0} 个，失败 ${s.failed ?? 0} 个`);
          } else {
            setMsg(s.message || "批量入库失败");
          }
          // 同步一份 batchResults 以兼容旧 UI 文案
          setBatchResults({
            total_pending: s.total_videos,
            processed: s.processed_videos,
            succeeded: s.succeeded ?? 0,
            failed: s.failed ?? 0,
            results: [],
          });
          loadFolders();
        }
      };
      poll();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "批量入库失败");
      setBatchIngesting(false);
    }
  }

  return (
    <>
      {/* Unified LoginModal with douyin tab */}
      <LoginModal
        isOpen={showLogin}
        onClose={() => setShowLogin(false)}
        onDouyinSuccess={handleQRLoginSuccess}
        defaultTab="douyin"
      />
      {showSyncLimitModal && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" onClick={() => !syncing && setShowSyncLimitModal(false)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">设置获取上限</div>
            <div style={{ fontSize: 13, color: "var(--muted, #888)", margin: "6px 0 14px" }}>
              同时作用于喜欢和收藏的抓取上限（1-5000，设为 5000 可覆盖大多数账号）
            </div>
            <input
              type="number"
              min={1}
              max={5000}
              value={syncLimit}
              onChange={(e) => {
                const v = e.target.value;
                // 允许清空：输入为空时设为空字符串，不再强制保留 0
                if (v === "") {
                  setSyncLimit("" as unknown as number);
                } else {
                  const n = parseInt(v, 10);
                  if (!isNaN(n)) setSyncLimit(n);
                }
              }}
              style={{ width: "100%", padding: "8px 10px", borderRadius: 8, border: "1px solid var(--border, #ddd)", fontSize: 15, marginBottom: 16, boxSizing: "border-box" }}
            />
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button onClick={() => setShowSyncLimitModal(false)} disabled={syncing} className="btn btn-sm">取消</button>
              <button onClick={executeSyncFavorites} disabled={syncing} className="btn btn-primary btn-sm">
                {syncing ? "同步中..." : "开始同步"}
              </button>
            </div>
          </div>
        </div>
      )}
      <div className="panel-inner">
        <div className="panel-header">
          <div className="panel-title">🎵 抖音收藏管理</div>
          {douyinUser && (
            <div className="panel-subtitle">{douyinUser.nickname || douyinUser.uid}</div>
          )}
        </div>

        {/* Auth Section - consistent card style */}
        <div className="douyin-auth-card">
          {douyinUser ? (
            <div className="douyin-auth-logged-in">
              <div className="douyin-auth-user-row">
                <span className="status-pill ok">已登录</span>
                <span className="douyin-auth-nickname">{douyinUser.nickname || douyinUser.uid}</span>
              </div>
              <div className="douyin-auth-actions">
                <button onClick={handleSyncFavorites} disabled={syncing} className="btn btn-primary btn-sm">
                  {syncing ? "同步中..." : "同步收藏"}
                </button>
                <button
                  onClick={() => handleBatchIngest()}
                  disabled={batchIngesting}
                  className="btn btn-sm"
                  title="将勾选的或已同步但未入库的视频批量处理进知识库"
                >
                  {batchIngesting ? "入库中..." : "批量入库"}
                </button>
                <button onClick={handleLogout} className="btn btn-sm">退出</button>
              </div>
            </div>
          ) : (
            <div className="douyin-auth-form">
              <div className="douyin-auth-hint">
                <span className="douyin-auth-label">推荐：粘贴抖音 Cookie 快速登录</span>
              </div>
              <div className="douyin-auth-input-row">
                <input
                  value={cookie}
                  onChange={(e) => setCookie(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleCookieLogin()}
                  placeholder="在此粘贴抖音 Cookie..."
                  className="douyin-auth-input"
                />
                <button
                  onClick={handleCookieLogin}
                  disabled={cookieLogging || !cookie.trim()}
                  className="btn btn-primary btn-sm"
                >
                  {cookieLogging ? "验证中..." : "Cookie登录"}
                </button>
                <button
                  onClick={() => { setShowLogin(true); setLoginError(""); }}
                  className="btn btn-sm"
                >
                  扫码登录
                </button>
              </div>
              {loginError && (
                <div className="douyin-auth-error">{loginError}</div>
              )}
            </div>
          )}
        </div>

        {/* 批量入库进度条 */}
        {batchIngesting && batchProgress && (
          <div className="douyin-progress">
            <div className="douyin-progress-bar">
              <div
                className="douyin-progress-fill"
                style={{ width: `${Math.min(100, Math.max(0, batchProgress.progress))}%` }}
              />
            </div>
            <div className="douyin-progress-meta">
              <span>{batchProgress.progress}%</span>
              <span>{batchProgress.processed_videos}/{batchProgress.total_videos}</span>
              {batchProgress.succeeded != null && (
                <span style={{ color: "var(--neon, #00e5a8)" }}>成功 {batchProgress.succeeded}</span>
              )}
              {batchProgress.failed != null && batchProgress.failed > 0 && (
                <span style={{ color: "var(--danger, #ff5b6e)" }}>失败 {batchProgress.failed}</span>
              )}
            </div>
            {batchProgress.current_video_title && (
              <div className="douyin-progress-meta">
                <span
                  title={batchProgress.current_video_title}
                  style={{
                    color: "var(--muted, #888)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    maxWidth: "100%",
                  }}
                >
                  当前：{batchProgress.current_video_title}
                </span>
              </div>
            )}
          </div>
        )}

        {/* Folder List */}
        {douyinUser && folders.length > 0 && (
          <div className="douyin-folders-section">
            <div className="douyin-folders-header">文件夹</div>
            {folders.map((f) => {
              const statusLabel = f.status === "all_indexed" ? "已全部入库"
                : f.status === "partial" ? "部分入库" : "未入库";
              const statusClass = f.status === "all_indexed" ? "folder-status-ok"
                : f.status === "partial" ? "folder-status-partial" : "folder-status-none";
              const isExpanded = expandedFolder === f.folder_id;
              return (
                <div key={f.folder_id} className="douyin-folder-card">
                  <div className="douyin-folder-row">
                    <label className="douyin-folder-check">
                      <input
                        type="checkbox"
                        checked={f.is_selected ?? false}
                        onChange={(e) => handleFolderSelect(f.folder_id, e.target.checked)}
                      />
                    </label>
                    <span
                      className="douyin-folder-name"
                      onClick={() => toggleFolderExpand(f.folder_id)}
                      style={{cursor:"pointer",flex:1}}
                    >
                      {f.title}
                      <span style={{fontSize:12,color:"#999",marginLeft:8}}>
                        ({f.indexed_count}/{f.media_count})
                      </span>
                    </span>
                    <span className={`douyin-folder-status ${statusClass}`}>{statusLabel}</span>
                    {(f.status === "partial" || f.status === "none") && (
                      <button
                        onClick={(e) => { e.stopPropagation(); handleBatchIngest(f.folder_id); }}
                        disabled={batchIngesting}
                        className="btn btn-sm"
                        style={{ marginLeft: 6, fontSize: 11, padding: "2px 8px" }}
                        title="将此文件夹中未入库的视频处理进知识库"
                      >
                        入库
                      </button>
                    )}
                    <span
                      className="douyin-folder-expand"
                      onClick={() => toggleFolderExpand(f.folder_id)}
                      style={{cursor:"pointer",marginLeft:8,fontSize:12}}
                    >
                      {isExpanded ? "▲" : "▼"}
                    </span>
                  </div>
                  {isExpanded && (
                    <div className="douyin-folder-videos">
                      {folderVideos.length === 0 ? (
                        <div className="douyin-folder-empty">加载中...</div>
                      ) : (
                        folderVideos.map((v) => (
                          <div key={v.video_id} className="douyin-folder-video-row">
                            <label className="douyin-video-check">
                              <input
                                type="checkbox"
                                checked={v.is_selected}
                                onChange={(e) => handleVideoSelect(v.video_id, f.folder_id, e.target.checked)}
                              />
                            </label>
                            <span
                              className="douyin-video-title"
                              title={`${v.title} · 按住 Ctrl+左键 在浏览器打开原视频`}
                              style={{ color: "inherit", textDecoration: "none", cursor: "pointer" }}
                              onClick={(e) => {
                                // 仅 Ctrl+左键 时通过后端 API 在系统默认浏览器打开
                                // Tauri webview 中 <a target="_blank"> 会在内部 webview 打开，用户看不到窗口
                                if (e.ctrlKey || e.metaKey) {
                                  e.preventDefault();
                                  e.stopPropagation();
                                  openExternal(`https://www.douyin.com/video/${v.video_id}`);
                                }
                              }}
                            >
                              {v.title}
                            </span>
                            <span className="douyin-video-meta">@{v.author}</span>
                            <span className={`douyin-video-status ${v.is_processed ? "processed" : "pending"}`}>
                              {v.is_processed ? "已入库" : "待入库"}
                            </span>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}

        {/* URL Input */}
        <div className="douyin-input-row">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleParse()}
            placeholder="粘贴抖音分享链接... (例: https://v.douyin.com/xxx/)"
          />
          <button onClick={handleParse} disabled={parsing || !url.trim()} className="btn btn-primary btn-sm">
            {parsing ? "解析中..." : "解析"}
          </button>
        </div>

        {msg && (
          <div className={`douyin-msg ${msg.includes("成功") || msg.includes("完成") ? "success" : "error"}`}>
            {msg}
          </div>
        )}
        {syncResults && (
          <div className="douyin-msg success" style={{whiteSpace:"pre-line"}}>
            {syncResults.like && <div>{syncResults.like}</div>}
            {syncResults.collect && <div>{syncResults.collect}</div>}
          </div>
        )}
        {batchResults && batchResults.processed > 0 && (
          <div className="douyin-msg success" style={{whiteSpace:"pre-line"}}>
            <div>批量入库：处理 {batchResults.processed} 个，成功 {batchResults.succeeded} 个，失败 {batchResults.failed} 个</div>
            {batchResults.total_pending > batchResults.processed && (
              <div style={{fontSize:12,opacity:0.8}}>{`还有 ${batchResults.total_pending - batchResults.processed} 个待入库，可再次点击"批量入库"继续`}</div>
            )}
          </div>
        )}

        {preview && (
          <div className="douyin-preview-card">
            <div className="douyin-preview-title">{preview.title}</div>
            <div className="douyin-preview-meta">@{preview.author} · {preview.duration}s</div>
            <button onClick={handleIngest} disabled={ingesting} className="btn btn-primary btn-sm" style={{marginTop: 8}}>
              {ingesting ? "入库中..." : "入库到知识库"}
            </button>
            {ingestSteps && ingestSteps.length > 0 && (
              <div className="douyin-ingest-steps" aria-live="polite">
                {ingestSteps.map((s, i) => {
                  const tone =
                    s.event === "error" ? "error"
                      : s.event === "done" ? "done"
                      : s.status === "completed" ? "done"
                      : "running";
                  return (
                    <div key={i} className={`douyin-ingest-step tone-${tone}`}>
                      <span className="douyin-ingest-step-icon">
                        {tone === "done" ? "✓" : tone === "error" ? "✗" : "•"}
                      </span>
                      <span className="douyin-ingest-step-msg">{s.message}</span>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}
