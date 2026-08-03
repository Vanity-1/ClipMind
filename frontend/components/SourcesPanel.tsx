"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import {
  FavoriteFolder,
  Video,
  favoritesApi,
  knowledgeApi,
  BuildStatus,
  FolderStatus,
  OrganizePreviewResponse,
  openExternal,
} from "@/lib/api";
import { listTasks } from "@/lib/taskApi";
import OrganizePreviewModal from "@/components/OrganizePreviewModal";
import ExportMarkdownModal from "@/components/ExportMarkdownModal";

interface Props {
  sessionId: string;
  onBuildDone?: () => void;
  onSelectionChange?: (folderIds: number[]) => void;
  /** 当外部数据变更（如 RAG 管理面板出库/删除）时递增此值，触发状态刷新 */
  refreshSignal?: number;
}

interface ExportTarget {
  video: Video;
  folderId: number;
}

export default function SourcesPanel({ sessionId, onBuildDone, onSelectionChange, refreshSignal }: Props) {
  const [folders, setFolders] = useState<(FavoriteFolder & { videos?: Video[]; expanded?: boolean; loading?: boolean; count_source?: "bili" | "filtered" | "db" })[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [building, setBuilding] = useState(false);
  const [progress, setProgress] = useState<BuildStatus | null>(null);
  const [statusMap, setStatusMap] = useState<Record<number, FolderStatus>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [organizeOpen, setOrganizeOpen] = useState(false);
  const [organizeLoading, setOrganizeLoading] = useState(false);
  const [organizePreview, setOrganizePreview] = useState<OrganizePreviewResponse | null>(null);
  const [organizeMessage, setOrganizeMessage] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [exportTarget, setExportTarget] = useState<ExportTarget | null>(null);
  // SubTask 10.3: 当前入库任务 ID，用于取消入库
  const [buildingTaskId, setBuildingTaskId] = useState<string | null>(null);
  const pollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // F7: 跟踪组件挂载状态，卸载后停止所有 setState，避免内存泄漏与 React 警告
  const mountedRef = useRef(true);
  // SubTask 10.3: 取消标志，阻止取消后的轮询继续 setState
  const cancelledRef = useRef(false);
  // SubTask 10.5: folders 引用，供异步回调读取最新展开状态
  const foldersRef = useRef(folders);
  useEffect(() => { foldersRef.current = folders; }, [folders]);
  useEffect(() => () => { mountedRef.current = false; }, []);

  // 加载收藏夹列表（从B站获取）
  const loadFolders = useCallback(async () => {
    setLoading(true);
    try {
      const data = await favoritesApi.getList(sessionId);
      setFolders(data.map((f) => ({ ...f, count_source: "bili" })));
    } catch (error) {
      console.error(error);
      setMessage("加载收藏夹列表失败，请检查登录状态或稍后重试");
    }
    setLoading(false);
  }, [sessionId]);

  // 加载入库状态（从本地数据库）
  const loadStatuses = useCallback(async () => {
    try {
      const data = await knowledgeApi.getFolderStatus(sessionId);
      const map: Record<number, FolderStatus> = {};
      data.forEach((item) => {
        map[item.media_id] = item;
      });
      setStatusMap(map);
      setFolders((prev) =>
        prev.map((f) => {
          const s = map[f.media_id];
          if (!s?.media_count) return f;
          if (f.count_source === "filtered") return f;
          return { ...f, count_source: "bili" };
        })
      );
    } catch (error) {
      console.error(error);
      setMessage("加载入库状态失败，可尝试刷新");
    }
  }, [sessionId]);

  useEffect(() => {
    void loadFolders().then(loadStatuses);
  }, [loadFolders, loadStatuses]);

  useEffect(() => () => { if (pollTimeoutRef.current) clearTimeout(pollTimeoutRef.current); }, []);

  // 刷新
  const refresh = useCallback(async () => {
    setMessage(null);
    await loadFolders();
    await loadStatuses();
  }, [loadFolders, loadStatuses]);

  // SubTask 10.5: 刷新已展开收藏夹的视频列表（入库完成后调用）
  const reloadExpandedFolders = useCallback(async () => {
    const expanded = foldersRef.current.filter((f) => f.expanded);
    await Promise.all(
      expanded.map(async (f) => {
        try {
          const res = await favoritesApi.getAllVideos(f.media_id, sessionId);
          if (mountedRef.current) {
            setFolders((prev) =>
              prev.map((folder) =>
                folder.media_id === f.media_id
                  ? {
                      ...folder,
                      videos: res.videos,
                      media_count: res.total,
                      count_source: "filtered" as const,
                    }
                  : folder,
              ),
            );
          }
        } catch (e) {
          console.error("刷新收藏夹视频失败", e);
        }
      }),
    );
  }, [sessionId]);

  // 外部数据变更信号（如 RAG 管理面板出库/删除后），刷新入库状态和已展开列表
  useEffect(() => {
    if (refreshSignal && refreshSignal > 0) {
      void loadStatuses();
      // 同时刷新已展开收藏夹的视频列表，确保删除/出库后列表同步更新
      void reloadExpandedFolders();
    }
  }, [refreshSignal, loadStatuses, reloadExpandedFolders]);

  const openOrganizePreview = async (folderId: number) => {
    setOrganizeMessage(null);
    setOrganizePreview(null);
    setOrganizeOpen(true);
    setOrganizeLoading(true);
    try {
      const res = await favoritesApi.organizePreview(folderId, sessionId);
      setOrganizePreview(res);
    } catch (e) {
      console.error("整理预览失败", e);
      setOrganizeMessage("预览失败，请稍后重试");
    } finally {
      setOrganizeLoading(false);
    }
  };

  // 展开收藏夹查看视频
  const toggleExpand = async (id: number) => {
    setFolders((prev) =>
      prev.map((f) => {
        if (f.media_id !== id) return f;
        if (f.expanded) return { ...f, expanded: false };
        if (f.videos) return { ...f, expanded: true };
        return { ...f, expanded: true, loading: true };
      })
    );

    const folder = folders.find((f) => f.media_id === id);
    if (!folder?.videos) {
      try {
        const res = await favoritesApi.getAllVideos(id, sessionId);
        setFolders((prev) =>
          prev.map((f) =>
            f.media_id === id ? { ...f, videos: res.videos, loading: false, media_count: res.total, count_source: "filtered" } : f
          )
        );
      } catch (e) {
        console.error("加载收藏夹视频失败", e);
        setFolders((prev) =>
          prev.map((f) => (f.media_id === id ? { ...f, loading: false } : f))
        );
        setMessage("加载收藏夹视频失败，可尝试重新展开");
      }
    }
  };

  // 选择收藏夹
  const toggleSelect = (id: number) => {
    const s = new Set(selected);
    if (s.has(id)) {
      s.delete(id);
    } else {
      s.add(id);
    }
    setSelected(s);
    onSelectionChange?.(Array.from(s));
  };

  const selectedFolders = useMemo(
    () => folders.filter((folder) => selected.has(folder.media_id)),
    [folders, selected]
  );

  const selectedVideoCount = selectedFolders.reduce((sum, folder) => {
    const status = statusMap[folder.media_id];
    return sum + (status?.media_count ?? folder.media_count ?? 0);
  }, 0);

  const openBuildConfirm = () => {
    if (selected.size === 0 || building) return;
    setConfirmOpen(true);
  };

  // 构建/更新知识库（统一操作）
  const startBuildKnowledge = async () => {
    // SubTask 8.2: building 守卫，防止并发
    if (building) return;
    if (selected.size === 0) return;

    // SubTask 8.4: 全局入库状态感知，检查是否有进行中任务
    try {
      const running = await listTasks({ status: "running" });
      if (running.tasks.length > 0) {
        setMessage("已有入库任务进行中，请等待完成");
        return;
      }
    } catch {
      // 查询失败不阻塞，由后续轮询兜底
    }

    setConfirmOpen(false);
    setBuilding(true);
    setMessage(null);
    setProgress(null);
    setBuildingTaskId(null);
    cancelledRef.current = false;

    try {
      const res = await knowledgeApi.build({ folder_ids: Array.from(selected) }, sessionId);
      // SubTask 10.3: 保存 task_id 供取消使用
      setBuildingTaskId(res.task_id);

      // 轮询健壮性：最大轮询时长 30 分钟，最多连续失败 3 次
      const startTime = Date.now();
      const MAX_POLL_DURATION = 30 * 60 * 1000;
      const MAX_FAILURES = 3;
      let consecutiveFailures = 0;

      const poll = async () => {
        // F7: 组件已卸载则停止轮询与 setState
        if (!mountedRef.current) return;
        // SubTask 10.3: 已取消则停止轮询
        if (cancelledRef.current) return;
        // 超时保护：超过 30 分钟停止轮询
        if (Date.now() - startTime > MAX_POLL_DURATION) {
          if (mountedRef.current) {
            setBuilding(false);
            setBuildingTaskId(null);
            setMessage("入库超时，请检查后端状态");
          }
          return;
        }
        let s: BuildStatus;
        try {
          s = await knowledgeApi.getBuildStatus(res.task_id, sessionId);
        } catch {
          consecutiveFailures++;
          if (consecutiveFailures >= MAX_FAILURES) {
            if (mountedRef.current) {
              setBuilding(false);
              setBuildingTaskId(null);
              setMessage("查询构建状态失败，请检查网络后重试");
            }
            return;
          }
          // 失败后 2 秒重试
          pollTimeoutRef.current = setTimeout(poll, 2000);
          return;
        }
        if (!mountedRef.current) return;
        if (cancelledRef.current) return;
        consecutiveFailures = 0; // 成功时重置
        setProgress(s);

        if (s.status === "running" || s.status === "pending") {
          pollTimeoutRef.current = setTimeout(poll, 1000);
        } else {
          setBuilding(false);
          setBuildingTaskId(null);
          // SubTask 10.2: 区分部分失败/全部失败
          if (s.status === "completed") {
            const msg = s.failed && s.failed > 0
              ? `入库完成：成功 ${s.succeeded || 0}，失败 ${s.failed}`
              : (s.message || "构建完成");
            setMessage(msg);
            await loadStatuses();
            // SubTask 10.5: 刷新已展开收藏夹的视频列表
            await reloadExpandedFolders();
            onBuildDone?.();
          } else if (s.status === "failed") {
            // SubTask 10.6: failed 时也刷新 loadStatuses
            const msg = s.succeeded && s.succeeded > 0
              ? `部分失败：成功 ${s.succeeded}，失败 ${s.failed || 0}。${s.message || ""}`
              : `构建失败: ${s.message}`;
            setMessage(msg);
            await loadStatuses();
          }
        }
      };
      poll();
    } catch (e) {
      console.error("构建知识库失败", e);
      if (mountedRef.current) {
        setBuilding(false);
        setBuildingTaskId(null);
        setMessage("构建失败，请重试");
      }
    }
  };

  // SubTask 10.3: 取消入库
  const handleCancelBuild = async () => {
    if (!buildingTaskId) return;
    const taskId = buildingTaskId;
    // 停止轮询
    cancelledRef.current = true;
    if (pollTimeoutRef.current) {
      clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
    try {
      await knowledgeApi.cancel(taskId);
    } catch (e) {
      console.error("取消入库失败", e);
      // 取消API失败时，提示用户任务可能已结束
      if (mountedRef.current) {
        setMessage("取消失败，任务可能已结束");
      }
    } finally {
      if (mountedRef.current) {
        setBuilding(false);
        setProgress(null);
        setBuildingTaskId(null);
        // 如果取消API失败，不显示"已取消入库"，保持之前的错误消息
        if (!message) {
          setMessage("已取消入库");
        }
        await loadStatuses();
        await reloadExpandedFolders();
      }
    }
  };

  // 格式化时间
  const formatTime = (value?: string | null) => {
    if (!value) return null;
    try {
      let dateStr = value;
      if (!value.includes('T') && !value.includes('Z')) {
        dateStr = value.replace(' ', 'T') + 'Z';
      }
      const date = new Date(dateStr);
      if (Number.isNaN(date.getTime())) return null;

      const month = String(date.getMonth() + 1).padStart(2, '0');
      const day = String(date.getDate()).padStart(2, '0');
      const hour = String(date.getHours()).padStart(2, '0');
      const minute = String(date.getMinutes()).padStart(2, '0');
      return `${month}/${day} ${hour}:${minute}`;
    } catch (e) {
      console.error("格式化时间失败", e);
      return null;
    }
  };

  // 获取收藏夹状态
  const getFolderStatus = (mediaId: number, totalInBilibili: number) => {
    const status = statusMap[mediaId];
    const indexedCount = status?.indexed_count ?? 0;
    const failedCount = status?.failed_count ?? 0;
    const lastSync = status?.last_sync_at;
    const folder = folders.find((f) => f.media_id === mediaId);
    const countSource = folder?.count_source ?? "bili";
    let totalCount = totalInBilibili;
    if (countSource === "filtered") {
      totalCount = folder?.media_count ?? totalInBilibili;
    } else if (status?.media_count != null) {
      totalCount = status.media_count;
    }

    // 未入库：从未同步过
    if (!lastSync) {
      return { label: "未入库", className: "empty", indexedCount };
    }

    if (failedCount > 0) {
      const label = indexedCount > 0 ? "部分入库失败" : "入库失败";
      return { label, className: "partial", indexedCount, totalCount };
    }

    // 已入库：有同步时间
    if (indexedCount >= totalCount) {
      return { label: "已入库", className: "ok", indexedCount, totalCount };
    }

    // 有更新：B站收藏夹比本地多
    if (indexedCount < totalCount && indexedCount > 0) {
      return { label: "有更新", className: "partial", indexedCount, totalCount };
    }

    // 已同步过但当前无已入库视频（出库或删除后），显示"待入库"而非"入库失败"
    if (totalCount > 0 && indexedCount === 0 && failedCount === 0) {
      return { label: "待入库", className: "empty", indexedCount, totalCount };
    }

    if (totalCount > 0 && indexedCount === 0) {
      return { label: "入库失败", className: "partial", indexedCount, totalCount };
    }

    // 空收藏夹已完成同步
    return { label: "已入库", className: "ok", indexedCount, totalCount };
  };

  // 计算按钮文字
  const getButtonText = () => {
    if (building) return progress?.current_step || "处理中...";
    if (selected.size === 0) return "选择收藏夹";

    // 检查选中的是否有未入库的
    const hasUnindexed = Array.from(selected).some((id) => {
      const folder = folders.find((f) => f.media_id === id);
      if (!folder) return false;
      return !statusMap[id]?.last_sync_at;
    });

    if (hasUnindexed) {
      return `入库 (${selected.size})`;
    }
    return `更新 (${selected.size})`;
  };

  return (
    <div className="panel-inner">
      <div className="panel-header">
        <div>
          <div className="panel-title">收藏夹</div>
          <div className="panel-subtitle">{folders.length} 个</div>
        </div>
        <div className="panel-actions">
          <button
            onClick={() => {
              const def = folders.find((f) => f.is_default || f.title === "默认收藏夹");
              if (def) {
                openOrganizePreview(def.media_id);
              } else {
                setOrganizeMessage("未找到默认收藏夹");
              }
            }}
            className="btn btn-ghost"
            disabled={loading || organizeLoading}
          >
            {organizeLoading ? "整理中..." : "快速整理默认收藏夹"}
          </button>
          <button onClick={refresh} className="btn btn-ghost" disabled={loading}>
            {loading ? "加载中..." : "刷新"}
          </button>
        </div>
      </div>

      <div className="panel-body">
        <div className="sources-scroll">
          {loading ? (
            <div className="text-center text-sm text-[var(--muted)] py-6">加载中...</div>
          ) : folders.length === 0 ? (
            <div className="text-center text-sm text-[var(--muted)] py-6">暂无收藏夹</div>
          ) : (
            <div className="space-y-2">
              {folders.map((f) => {
                const status = getFolderStatus(f.media_id, f.media_count);
                const lastSync = formatTime(statusMap[f.media_id]?.last_sync_at);

                return (
                  <div key={f.media_id} className={`folder-card ${selected.has(f.media_id) ? "selected" : ""}`}>
                    <div className="folder-head" onClick={() => toggleExpand(f.media_id)}>
                      <input
                        type="checkbox"
                        checked={selected.has(f.media_id)}
                        onChange={() => toggleSelect(f.media_id)}
                        onClick={(e) => e.stopPropagation()}
                        className="w-4 h-4 accent-[var(--accent)]"
                      />
                      <div className="folder-meta">
                        <div className="folder-title" title={f.title}>{f.title}</div>
                      <div className="folder-count">
                        {status.indexedCount}/{status.totalCount ?? f.media_count} 个视频
                        {lastSync && ` · ${lastSync}`}
                      </div>
                      </div>
                      <span className={`status-pill ${status.className}`}>{status.label}</span>
                      <div className="folder-toggle">
                        <svg className={`w-4 h-4 transition-transform ${f.expanded ? "rotate-90" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                        </svg>
                      </div>
                    </div>

                    {f.expanded && (
                      <div className="folder-list">
                        {f.loading ? (
                          <div className="text-xs text-[var(--muted)]">加载中...</div>
                        ) : f.videos?.length === 0 ? (
                          <div className="text-xs text-[var(--muted)]">暂无视频</div>
                        ) : (
                          f.videos?.map((v) => (
                            <div key={v.bvid} className="video-item">
                              <button
                                className="video-export-button"
                                onClick={() => setExportTarget({ video: v, folderId: f.media_id })}
                                title="导出 Markdown"
                                aria-label={`导出 ${v.title}`}
                              >
                                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                                  <path d="M12 3v11m0 0 4-4m-4 4-4-4M5 17v3h14v-3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
                                </svg>
                              </button>
                              <span
                                className="video-title-link"
                                title={`${v.title} · 按住 Ctrl+左键 在浏览器打开原视频`}
                                style={{ cursor: "pointer" }}
                                onClick={(e) => {
                                  // 仅 Ctrl+左键 时通过后端 API 在系统默认浏览器打开
                                  if (e.ctrlKey || e.metaKey) {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    openExternal(`https://www.bilibili.com/video/${v.bvid}`);
                                  }
                                }}
                              >
                                {v.title}
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
        </div>
      </div>

      <div className="panel-footer">
        {/* 进度条 */}
        {progress && building && (
          <div className="mb-4">
            <div className="flex justify-between text-xs mb-2">
              <span className="text-[var(--muted)] truncate">{progress.current_step}</span>
              <span className="text-[var(--accent)]">{progress.progress}%</span>
            </div>
            <div className="build-progress-meta">
              {progress.total_folders ? (
                <span>
                  收藏夹 {progress.processed_folders ?? 0}/{progress.total_folders}
                </span>
              ) : null}
              {progress.total_videos ? (
                <span>
                  视频 {progress.processed_videos}/{progress.total_videos}
                </span>
              ) : null}
            </div>
            {progress.current_video_title && (
              <div className="build-current-title" title={progress.current_video_title}>
                当前：{progress.current_video_title}
              </div>
            )}
            <div className="progress">
              {/* SubTask 10.4: 进度条宽度钳制 0-100 */}
              <div className="progress-bar" style={{ width: `${Math.min(100, Math.max(0, progress.progress))}%` }} />
            </div>
            {/* SubTask 10.3: 取消入库按钮 */}
            <button
              onClick={handleCancelBuild}
              className="btn btn-outline w-full mt-2"
              disabled={!buildingTaskId}
            >
              取消入库
            </button>
          </div>
        )}

        {/* 消息 */}
        {message && <div className="text-xs text-[var(--muted)] mb-3">{message}</div>}
        {organizeMessage && <div className="text-xs text-[var(--muted)] mb-3">{organizeMessage}</div>}

        {/* 主按钮 */}
        <button
          onClick={openBuildConfirm}
          disabled={selected.size === 0 || building}
          className="btn btn-primary w-full"
        >
          {getButtonText()}
        </button>

        <p className="text-xs text-[var(--muted)] text-center mt-2">
          入库后可在右侧进行问答
        </p>
      </div>

      <OrganizePreviewModal
        open={organizeOpen}
        sessionId={sessionId}
        preview={organizePreview}
        loading={organizeLoading}
        errorMessage={organizeMessage}
        onClose={() => setOrganizeOpen(false)}
        onApplied={refresh}
      />

      <ExportMarkdownModal
        video={exportTarget?.video ?? null}
        folderId={exportTarget?.folderId ?? null}
        sessionId={sessionId}
        onClose={() => setExportTarget(null)}
        onIngested={async () => {
          await loadStatuses();
          onBuildDone?.();
        }}
      />

      {confirmOpen && (
        <div className="modal-backdrop" onClick={() => setConfirmOpen(false)}>
          <div className="modal-card build-confirm-modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-title">确认入库</div>
            <div className="modal-subtitle">
              将处理 {selectedFolders.length} 个收藏夹，约 {selectedVideoCount} 个视频
            </div>

            <div className="build-confirm-list">
              {selectedFolders.map((folder) => {
                const status = getFolderStatus(folder.media_id, folder.media_count);
                return (
                  <div key={folder.media_id} className="build-confirm-item">
                    <div>
                      <div className="build-confirm-title" title={folder.title}>
                        {folder.title}
                      </div>
                      <div className="build-confirm-meta">
                        {status.indexedCount}/{status.totalCount ?? folder.media_count} 个视频
                      </div>
                    </div>
                    <span className={`status-pill ${status.className}`}>{status.label}</span>
                  </div>
                );
              })}
            </div>

            {selectedVideoCount >= 50 && (
              <div className="build-confirm-warning">
                本次视频较多，可能触发较长 ASR/Embedding 处理，也会产生模型调用费用。建议首次使用先小批量验证。
              </div>
            )}

            <div className="organize-actions">
              <button className="btn btn-outline" onClick={() => setConfirmOpen(false)}>
                取消
              </button>
              <button className="btn btn-primary" onClick={startBuildKnowledge}>
                确认开始
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
