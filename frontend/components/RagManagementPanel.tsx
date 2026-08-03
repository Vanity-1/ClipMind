"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import {
  knowledgeApi,
  type VideoListItem,
  type VideoIngestItem,
  type BuildStatus,
} from "@/lib/api";
import { taskTrackerApi, type ErrorDetail } from "@/lib/taskTrackerApi";

// ============================================================================
// Props
// ============================================================================

interface RagManagementPanelProps {
  isOpen: boolean;
  onClose: () => void;
  biliSessionId: string;
  douyinSessionId?: string;
  /** 出库/删除操作完成后回调，用于通知外部刷新状态 */
  onDataChanged?: () => void;
}

// ============================================================================
// 内部类型与常量
// ============================================================================

type PlatformFilter = "all" | "bilibili" | "douyin";
type StatusFilter = "all" | "processed" | "pending" | "failed";
type ToastType = "success" | "error" | "info";

const PLATFORM_FILTERS: { key: PlatformFilter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "bilibili", label: "B站" },
  { key: "douyin", label: "抖音" },
];

const STATUS_FILTERS: { key: StatusFilter; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "processed", label: "已入库" },
  { key: "pending", label: "待入库" },
  { key: "failed", label: "失败" },
];

function videoKey(v: VideoListItem): string {
  return `${v.platform}:${v.bvid}`;
}

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds || 0));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function platformLabel(platform: string): string {
  return platform === "douyin" ? "抖音" : "B站";
}

function statusOf(v: VideoListItem): { text: string; cls: string; icon: string } {
  if (v.is_processed) return { text: "已入库", cls: "processed", icon: "✓" };
  if (v.process_error) return { text: "失败", cls: "failed", icon: "✕" };
  return { text: "待入库", cls: "pending", icon: "○" };
}

// ============================================================================
// 组件
// ============================================================================

export default function RagManagementPanel({
  isOpen,
  onClose,
  biliSessionId,
  douyinSessionId,
  onDataChanged,
}: RagManagementPanelProps) {
  const [videos, setVideos] = useState<VideoListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [platformFilter, setPlatformFilter] = useState<PlatformFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [tagFilter, setTagFilter] = useState<string>("");
  const [ingesting, setIngesting] = useState(false);
  const [progress, setProgress] = useState<BuildStatus | null>(null);
  const [filterChanging, setFilterChanging] = useState(false); // 跟踪过滤器切换的加载状态

  const [confirmingKey, setConfirmingKey] = useState<string | null>(null);
  const reqIdRef = useRef(0); // 请求 ID，用于竞态保护
  const [removingKey, setRemovingKey] = useState<string | null>(null);

  const [showBatchToolbar, setShowBatchToolbar] = useState(false);
  const [batchOperating, setBatchOperating] = useState(false);
  const [batchProgress, setBatchProgress] = useState<{
    total: number;
    current: number;
    message: string;
  } | null>(null);

  const [errorDetailKey, setErrorDetailKey] = useState<string | null>(null);
  const [errorDetail, setErrorDetail] = useState<ErrorDetail | null>(null);

  const [taskSummary, setTaskSummary] = useState<{
    pending: number;
    running: number;
    success: number;
    failed: number;
    total: number;
  }>({ pending: 0, running: 0, success: 0, failed: 0, total: 0 });

  const [toast, setToast] = useState<{ type: ToastType; msg: string } | null>(
    null,
  );

  // SubTask 10.3: 当前入库任务 ID，用于取消入库
  const [ingestTaskId, setIngestTaskId] = useState<string | null>(null);

  const mountedRef = useRef(true);
  const pollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const selectAllRef = useRef<HTMLInputElement>(null);
  // SubTask 10.3: 取消标志，阻止取消后的轮询继续 setState
  const cancelledRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (pollTimeoutRef.current) clearTimeout(pollTimeoutRef.current);
    };
  }, []);

  const showToast = useCallback((type: ToastType, msg: string) => {
    setToast({ type, msg });
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(timer);
  }, [toast]);

  const loadVideos = useCallback(async (silent = false) => {
    if (!silent) {
      setLoading(true);
      setFilterChanging(true); // 显示过滤器切换加载状态
    }
    // 竞态保护：每次请求递增 ID，响应到达时校验是否为最新请求
    const reqId = ++reqIdRef.current;
    try {
      const data = await knowledgeApi.listAllVideos(
        biliSessionId,
        douyinSessionId,
        platformFilter === "all" ? undefined : platformFilter,
        statusFilter === "all" ? undefined : statusFilter,
        tagFilter || undefined,
      );
      // 丢弃过期响应：切换标签后旧请求返回的数据不覆盖新数据
      if (reqId !== reqIdRef.current) return;
      if (mountedRef.current) {
        setVideos(data);
        setTaskSummary({
          pending: data.filter((v) => !v.is_processed && !v.process_error)
            .length,
          running: 0,
          success: data.filter((v) => v.is_processed).length,
          failed: data.filter((v) => v.process_error).length,
          total: data.length,
        });
      }
    } catch {
      if (reqId !== reqIdRef.current) return;
      if (mountedRef.current) {
        // 静默刷新（自动轮询）失败时不清空列表、不弹 toast，避免每 5s 干扰用户
        if (!silent) {
          showToast("error", "加载视频列表失败");
          setVideos([]);
        }
      }
    } finally {
      if (reqId !== reqIdRef.current) return;
      if (mountedRef.current && !silent) {
        setLoading(false);
        setFilterChanging(false); // 隐藏过滤器切换加载状态
      }
    }
  }, [
    biliSessionId,
    douyinSessionId,
    platformFilter,
    statusFilter,
    tagFilter,
    showToast,
  ]);

  useEffect(() => {
    if (isOpen) loadVideos();
  }, [isOpen, loadVideos]);

  // 自动刷新：面板打开且无入库任务进行中时，每 5s 静默拉取最新列表
  useEffect(() => {
    if (!isOpen || ingesting) return;
    autoRefreshRef.current = setInterval(() => {
      loadVideos(true);
    }, 5000);
    return () => {
      if (autoRefreshRef.current) {
        clearInterval(autoRefreshRef.current);
        autoRefreshRef.current = null;
      }
    };
  }, [isOpen, ingesting, loadVideos]);

  const visibleKeys = videos.map(videoKey);
  const allSelected =
    visibleKeys.length > 0 && visibleKeys.every((k) => selected.has(k));
  const someSelected = visibleKeys.some((k) => selected.has(k));

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = someSelected && !allSelected;
    }
  }, [someSelected, allSelected, videos]);

  useEffect(() => {
    setShowBatchToolbar(selected.size >= 2);
  }, [selected]);

  const toggleSelect = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleSelectAll = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allSelected) {
        visibleKeys.forEach((k) => next.delete(k));
      } else {
        visibleKeys.forEach((k) => next.add(k));
      }
      return next;
    });
  };

  const changePlatform = (p: PlatformFilter) => {
    if (p === platformFilter) return;
    setPlatformFilter(p);
    setSelected(new Set());
    setConfirmingKey(null);
    setVideos([]);
  };

  const changeStatus = (s: StatusFilter) => {
    if (s === statusFilter) return;
    setStatusFilter(s);
    setSelected(new Set());
    setConfirmingKey(null);
    setVideos([]); // 清空旧数据，避免新请求返回前渲染旧标签的数据
  };

  const changeTag = (t: string) => {
    if (t === tagFilter) return;
    setTagFilter(t);
    setSelected(new Set());
    setConfirmingKey(null);
    setVideos([]);
  };

  // 从视频列表中提取所有可用标签（去重排序）
  const availableTags = useMemo(() => {
    const tagSet = new Set<string>();
    for (const v of videos) {
      if (v.tags && Array.isArray(v.tags)) {
        for (const t of v.tags) {
          if (t) tagSet.add(t);
        }
      }
    }
    return Array.from(tagSet).sort((a, b) => a.localeCompare(b, "zh"));
  }, [videos]);

  const ingestTargets = videos.filter(
    (v) => !v.is_processed && selected.has(videoKey(v)),
  );

  const handleIngest = async () => {
    if (ingesting || ingestTargets.length === 0) return;

    // SubTask 8.4: 全局入库状态感知，检查是否有进行中任务
    try {
      const running = await taskTrackerApi.listTasks({ status: "running" });
      if (running.tasks.length > 0) {
        showToast("info", "已有入库任务进行中，请等待完成");
        return;
      }
    } catch {
      // 查询失败不阻塞，由后续轮询兜底
    }

    const payload: VideoIngestItem[] = ingestTargets.map((v) => ({
      bvid: v.bvid,
      platform: v.platform === "douyin" ? "douyin" : "bilibili",
      tags: v.tags ?? undefined,
    }));
    const hasDouyin = payload.some((p) => p.platform === "douyin");

    setIngesting(true);
    setProgress(null);
    setConfirmingKey(null);
    setIngestTaskId(null);
    cancelledRef.current = false;

    try {
      const res = await knowledgeApi.ingestVideos(
        { videos: payload },
        biliSessionId,
        hasDouyin ? douyinSessionId : undefined,
      );
      // SubTask 10.3: 保存 task_id 供取消使用
      setIngestTaskId(res.task_id);

      // 轮询健壮性：最大轮询时长 30 分钟，最多连续失败 3 次
      const startTime = Date.now();
      const MAX_POLL_DURATION = 30 * 60 * 1000;
      const MAX_FAILURES = 3;
      let consecutiveFailures = 0;

      const poll = async () => {
        if (!mountedRef.current) return;
        // SubTask 10.3: 已取消则停止轮询
        if (cancelledRef.current) return;
        // 超时保护：超过 30 分钟停止轮询
        if (Date.now() - startTime > MAX_POLL_DURATION) {
          if (mountedRef.current) {
            setIngesting(false);
            setIngestTaskId(null);
            showToast("error", "入库超时，请检查后端状态");
          }
          return;
        }
        let s: BuildStatus;
        try {
          s = await knowledgeApi.getBuildStatus(res.task_id, biliSessionId);
        } catch {
          consecutiveFailures++;
          if (consecutiveFailures >= MAX_FAILURES) {
            if (mountedRef.current) {
              setIngesting(false);
              setIngestTaskId(null);
              showToast("error", "查询入库状态失败，请检查网络后重试");
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
        setTaskSummary((prev) => ({
          ...prev,
          total: s.total_videos ?? prev.total,
          // running = 未处理的视频数（total - 已处理 - 失败）
          running: Math.max(
            0,
            (s.total_videos ?? 0) -
              (s.processed_videos ?? 0) -
              (s.failed ?? 0),
          ),
          success: s.succeeded ?? 0,
          failed: s.failed ?? 0,
        }));
        if (s.status === "running" || s.status === "pending") {
          pollTimeoutRef.current = setTimeout(poll, 1000);
        } else {
          setIngesting(false);
          setIngestTaskId(null);
          // SubTask 10.2: 区分部分失败/全部失败
          if (s.status === "completed") {
            const msg = s.failed && s.failed > 0
              ? `入库完成：成功 ${s.succeeded || 0}，失败 ${s.failed}`
              : (s.message || "入库完成");
            showToast("success", msg);
            setSelected(new Set());
            await loadVideos();
          } else {
            const msg = s.succeeded && s.succeeded > 0
              ? `部分失败：成功 ${s.succeeded}，失败 ${s.failed || 0}。${s.message || ""}`
              : `入库失败: ${s.message}`;
            showToast("error", msg);
          }
        }
      };
      poll();
    } catch (err) {
      if (mountedRef.current) {
        setIngesting(false);
        setIngestTaskId(null);
        const msg = err instanceof Error ? err.message : "入库失败";
        showToast("error", msg);
      }
    }
  };

  // SubTask 10.3: 取消入库
  const handleCancelIngest = async () => {
    if (!ingestTaskId) return;
    const taskId = ingestTaskId;
    // 停止轮询
    cancelledRef.current = true;
    if (pollTimeoutRef.current) {
      clearTimeout(pollTimeoutRef.current);
      pollTimeoutRef.current = null;
    }
    try {
      await taskTrackerApi.cancelTask(taskId);
    } catch (e) {
      console.error("取消入库失败", e);
    } finally {
      if (mountedRef.current) {
        setIngesting(false);
        setProgress(null);
        setIngestTaskId(null);
        showToast("info", "已取消入库");
        await loadVideos();
      }
    }
  };

  const handleRemove = async (v: VideoListItem) => {
    const key = videoKey(v);
    const sessionId =
      v.platform === "douyin" ? douyinSessionId : biliSessionId;
    if (!sessionId) {
      showToast("error", "缺少会话信息，无法出库");
      return;
    }
    setRemovingKey(key);
    try {
      await knowledgeApi.removeVideoFromRag(v.bvid, v.platform, sessionId);
      if (mountedRef.current) {
        showToast("success", "出库成功");
        setConfirmingKey(null);
        await loadVideos();
        onDataChanged?.();
      }
    } catch (err) {
      if (mountedRef.current) {
        const msg = err instanceof Error ? err.message : "出库失败";
        showToast("error", msg);
      }
    } finally {
      if (mountedRef.current) setRemovingKey(null);
    }
  };

  const handleBatchRetry = async () => {
    // SubTask 8.1: ingesting 守卫，防止与入库任务并发
    if (ingesting) return;
    if (batchOperating || selected.size === 0) return;
    const failedKeys = Array.from(selected).filter((key) => {
      const v = videos.find((v) => videoKey(v) === key);
      return v?.process_error;
    });
    if (failedKeys.length === 0) {
      showToast("info", "选中的任务中没有可重试的失败项");
      return;
    }

    setBatchOperating(true);
    setBatchProgress({
      total: failedKeys.length,
      current: 0,
      message: "正在重试...",
    });

    try {
      const results = await taskTrackerApi.batchRetry(failedKeys);
      if (mountedRef.current) {
        showToast(
          "success",
          `重试完成：成功 ${results.succeeded} 个，失败 ${results.failed} 个`,
        );
        setSelected(new Set());
        await loadVideos();
      }
    } catch (err) {
      if (mountedRef.current) {
        const msg = err instanceof Error ? err.message : "批量重试失败";
        showToast("error", msg);
      }
    } finally {
      if (mountedRef.current) {
        setBatchOperating(false);
        setBatchProgress(null);
      }
    }
  };

  const handleBatchDelete = async () => {
    // SubTask 8.1: ingesting 守卫，防止与入库任务并发
    if (ingesting) return;
    if (batchOperating || selected.size === 0) return;
    const keys = Array.from(selected);
    if (keys.length === 0) return;

    setBatchOperating(true);
    setBatchProgress({
      total: keys.length,
      current: 0,
      message: "正在删除...",
    });

    try {
      const results = await taskTrackerApi.batchDelete(keys);
      if (mountedRef.current) {
        showToast(
          "success",
          `删除完成：成功 ${results.succeeded} 个，失败 ${results.failed} 个`,
        );
        setSelected(new Set());
        await loadVideos();
        onDataChanged?.();
      }
    } catch (err) {
      if (mountedRef.current) {
        const msg = err instanceof Error ? err.message : "批量删除失败";
        showToast("error", msg);
      }
    } finally {
      if (mountedRef.current) {
        setBatchOperating(false);
        setBatchProgress(null);
      }
    }
  };

  const handleBatchCancel = async () => {
    // SubTask 8.1: ingesting 守卫，防止与入库任务并发
    if (ingesting) return;
    if (batchOperating || selected.size === 0) return;

    setBatchOperating(true);
    setBatchProgress({
      total: selected.size,
      current: 0,
      message: "正在查询可取消的任务...",
    });

    try {
      // SubTask 8.3: 查询进行中的任务，匹配选中视频的 task_id
      // 之前错误地传入 platform:bvid 复合键，batchCancel 需要 UUID task_id
      const selectedBvids = new Set(
        videos
          .filter((v) => selected.has(videoKey(v)))
          .map((v) => v.bvid),
      );
      const taskIds: string[] = [];
      const inProgressStatuses = ["pending", "running", "retrying"] as const;
      for (const status of inProgressStatuses) {
        try {
          const res = await taskTrackerApi.listTasks({ status });
          for (const task of res.tasks) {
            if (
              selectedBvids.has(task.video_id) &&
              !taskIds.includes(task.task_id)
            ) {
              taskIds.push(task.task_id);
            }
          }
        } catch {
          // 忽略单个状态查询失败
        }
      }

      if (taskIds.length === 0) {
        if (mountedRef.current) {
          showToast("info", "选中的视频没有可取消的进行中任务");
        }
        return;
      }

      setBatchProgress({
        total: taskIds.length,
        current: 0,
        message: "正在取消...",
      });

      const results = await taskTrackerApi.batchCancel(taskIds);
      if (mountedRef.current) {
        showToast(
          "success",
          `取消完成：成功 ${results.succeeded} 个，失败 ${results.failed} 个`,
        );
        setSelected(new Set());
        await loadVideos();
      }
    } catch (err) {
      if (mountedRef.current) {
        const msg = err instanceof Error ? err.message : "批量取消失败";
        showToast("error", msg);
      }
    } finally {
      if (mountedRef.current) {
        setBatchOperating(false);
        setBatchProgress(null);
      }
    }
  };

  const handleRetrySingle = async (v: VideoListItem) => {
    // SubTask 8.1: ingesting 守卫，防止与入库任务并发
    if (ingesting) return;
    const sessionId =
      v.platform === "douyin" ? douyinSessionId : biliSessionId;
    if (!sessionId) {
      showToast("error", "缺少会话信息，无法重试");
      return;
    }
    // SubTask 8.4: 全局入库状态感知，检查是否有进行中任务
    try {
      const running = await taskTrackerApi.listTasks({ status: "running" });
      if (running.tasks.length > 0) {
        showToast("info", "已有入库任务进行中，请等待完成");
        return;
      }
    } catch {
      // 查询失败不阻塞，由后续轮询兜底
    }
    try {
      const payload: VideoIngestItem[] = [
        {
          bvid: v.bvid,
          platform: v.platform === "douyin" ? "douyin" : "bilibili",
          tags: v.tags ?? undefined,
        },
      ];
      await knowledgeApi.ingestVideos(
        { videos: payload },
        biliSessionId,
        v.platform === "douyin" ? douyinSessionId : undefined,
      );
      if (mountedRef.current) {
        showToast("success", "已发起重试，请稍后刷新查看结果");
        await loadVideos();
      }
    } catch (err) {
      if (mountedRef.current) {
        const msg = err instanceof Error ? err.message : "重试失败";
        showToast("error", msg);
      }
    }
  };

  useEffect(() => {
    if (!errorDetailKey) {
      setErrorDetail(null);
      return;
    }
    // 错误详情直接从已加载的视频列表中读取，无需调用 task_tracker API。
    // task_tracker 使用 UUID 作为 task_id，而前端持有的是 platform:bvid 复合键，
    // 直接读取视频列表项的错误字段即可满足展示需求。
    const v = videos.find((item) => videoKey(item) === errorDetailKey);
    if (!v) {
      setErrorDetail(null);
      return;
    }

    // 根据错误阶段生成默认建议（如果后端未提供suggestion）
    const getDefaultSuggestion = (stage: string): string | null => {
      switch (stage) {
        case "download":
          return "网络问题，请检查视频链接是否可访问，或稍后重试";
        case "asr":
          return "音频转写失败，可能视频无字幕或音频损坏，建议检查视频内容";
        case "embedding":
          return "向量化失败，请检查Embedding服务是否正常，或联系管理员";
        case "sync":
          return "同步失败，可能是收藏夹权限变更或网络问题";
        default:
          return null;
      }
    };

    const stage = v.last_error_stage || "unknown";
    const defaultSuggestion = getDefaultSuggestion(stage);

    setErrorDetail({
      task_id: videoKey(v),
      error_message: v.process_error || v.last_error_detail || "未知错误",
      error_stage: stage,
      retry_count: v.retry_count || 0,
      trace_id: null,
      failed_at: null,
      suggestion: defaultSuggestion, // 使用前端生成的默认建议
    });
  }, [errorDetailKey, videos]);

  const handleDeleteSingle = async (v: VideoListItem) => {
    const key = videoKey(v);
    const sessionId =
      v.platform === "douyin" ? douyinSessionId : biliSessionId;
    if (!sessionId) {
      showToast("error", "缺少会话信息，无法删除");
      return;
    }
    try {
      await knowledgeApi.deleteVideo(v.bvid, v.platform, sessionId);
      if (mountedRef.current) {
        showToast("success", "删除成功");
        setSelected((prev) => {
          const next = new Set(prev);
          next.delete(key);
          return next;
        });
        await loadVideos();
        onDataChanged?.();
      }
    } catch (err) {
      if (mountedRef.current) {
        const msg = err instanceof Error ? err.message : "删除失败";
        showToast("error", msg);
      }
    }
  };

  if (!isOpen) return null;

  const selectedCount = selected.size;

  return (
    <>
      <div className="rag-overlay">
        <div className="rag-backdrop" onClick={onClose} />
        <aside className="rag-panel" role="dialog" aria-label="RAG 入库管理">
          {/* Header */}
          <div className="rag-header">
            <div>
              <div className="rag-header-title">RAG 入库管理</div>
              <div className="rag-header-subtitle">
                Cross-Platform · Vector Ingest
              </div>
            </div>
            <button
              className="btn-icon"
              onClick={onClose}
              title="关闭"
              aria-label="关闭面板"
            >
              <svg
                width="16"
                height="16"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Progress Summary Bar */}
          <div className="rag-progress-summary">
            <div className="rag-summary-item summary-total">
              <span className="rag-summary-count">{taskSummary.total}</span>
              <span className="rag-summary-label">总计</span>
            </div>
            <div className="rag-summary-item summary-pending">
              <span className="rag-summary-count">{taskSummary.pending}</span>
              <span className="rag-summary-label">待入库</span>
            </div>
            <div className="rag-summary-item summary-running">
              <span className="rag-summary-count">{taskSummary.running}</span>
              <span className="rag-summary-label">进行中</span>
            </div>
            <div className="rag-summary-item summary-success">
              <span className="rag-summary-count">{taskSummary.success}</span>
              <span className="rag-summary-label">已入库</span>
            </div>
            <div className="rag-summary-item summary-failed">
              <span className="rag-summary-count">{taskSummary.failed}</span>
              <span className="rag-summary-label">失败</span>
            </div>
            {ingesting && (
              <div className="rag-summary-progress">
                <div className="rag-progress-bar">
                  <div
                    className="rag-progress-fill"
                    style={{
                      width: `${Math.min(
                        100,
                        Math.max(0, progress?.progress ?? 0),
                      )}%`,
                    }}
                  />
                </div>
                <span className="rag-progress-text">
                  {progress?.progress ?? 0}%
                </span>
              </div>
            )}
          </div>

          {/* Batch Operation Toolbar */}
          {showBatchToolbar && selected.size >= 2 && (
            <div className="rag-batch-toolbar">
              <span className="rag-batch-label">
                已选 {selected.size} 项
              </span>
              <div className="rag-batch-actions">
                <button
                  className="rag-batch-btn btn-retry"
                  onClick={handleBatchRetry}
                  disabled={batchOperating}
                  title="批量重试选中的失败任务"
                >
                  {batchOperating ? "处理中..." : "批量重试"}
                </button>
                <button
                  className="rag-batch-btn btn-cancel"
                  onClick={handleBatchCancel}
                  disabled={batchOperating}
                  title="批量取消选中的运行中任务"
                >
                  批量取消
                </button>
                <button
                  className="rag-batch-btn btn-delete"
                  onClick={handleBatchDelete}
                  disabled={batchOperating}
                  title="批量删除选中的任务"
                >
                  批量删除
                </button>
                <button
                  className="rag-batch-btn btn-clear"
                  onClick={() => setSelected(new Set())}
                  disabled={batchOperating}
                  title="清除选择"
                >
                  清除
                </button>
              </div>
              {batchProgress && (
                <div className="rag-batch-progress">
                  <div className="rag-progress-bar small">
                    <div
                      className="rag-progress-fill"
                      style={{
                        width: `${Math.round(
                          (batchProgress.current / batchProgress.total) * 100,
                        )}%`,
                      }}
                    />
                  </div>
                  <span>{batchProgress.message}</span>
                </div>
              )}
            </div>
          )}

          {/* Body */}
          <div className="rag-body">
            {/* 过滤器切换时的加载提示 */}
            {filterChanging && (
              <div className="rag-filter-loading" role="status" aria-live="polite">
                <span className="rag-filter-loading-text">加载中...</span>
              </div>
            )}
            <div className="rag-sticky-header">
              <div className="rag-toolbar">
                <div className="rag-filter-group">
                  {PLATFORM_FILTERS.map((f) => (
                    <button
                      key={f.key}
                      className={`rag-filter-btn${platformFilter === f.key ? " active" : ""}`}
                      onClick={() => changePlatform(f.key)}
                      disabled={ingesting}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
                <div className="rag-filter-group">
                  {STATUS_FILTERS.map((f) => (
                    <button
                      key={f.key}
                      className={`rag-filter-btn${statusFilter === f.key ? " active" : ""}`}
                      onClick={() => changeStatus(f.key)}
                      disabled={ingesting}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>
                {availableTags.length > 0 && (
                  <select
                    className="rag-tag-select"
                    value={tagFilter}
                    onChange={(e) => changeTag(e.target.value)}
                    disabled={ingesting}
                    title="按标签筛选"
                    aria-label="标签筛选"
                  >
                    <option value="">全部标签</option>
                    {availableTags.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                )}
                <button
                  className="rag-action-btn"
                  onClick={() => loadVideos()}
                  disabled={loading || ingesting}
                  title="刷新列表"
                >
                  {loading ? "刷新中..." : "刷新"}
                </button>
              </div>

              {ingesting && progress && (
                <div className="rag-progress">
                  {/* SubTask 10.1: 显示 current_step */}
                  <span className="text-sm">{progress.current_step}</span>
                  <div className="rag-progress-bar">
                    <div
                      className="rag-progress-fill"
                      style={{
                        width: `${Math.min(100, Math.max(0, progress.progress))}%`,
                      }}
                    />
                  </div>
                  <div className="rag-progress-meta">
                    <span>{progress.progress}%</span>
                    <span>
                      {progress.processed_videos}/{progress.total_videos}
                    </span>
                    {progress.succeeded != null && (
                      <span style={{ color: "var(--neon)" }}>
                        成功 {progress.succeeded}
                      </span>
                    )}
                    {progress.failed != null && progress.failed > 0 && (
                      <span style={{ color: "var(--danger)" }}>
                        失败 {progress.failed}
                      </span>
                    )}
                  </div>
                  {progress.current_video_title && (
                    <div className="rag-progress-meta">
                      <span
                        title={progress.current_video_title}
                        style={{
                          color: "var(--muted)",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          maxWidth: "100%",
                        }}
                      >
                        当前：{progress.current_video_title}
                      </span>
                    </div>
                  )}
                  {/* SubTask 10.3: 取消入库按钮 */}
                  <button
                    className="rag-action-btn"
                    onClick={handleCancelIngest}
                    disabled={!ingestTaskId}
                    title="取消入库"
                  >
                    取消入库
                  </button>
                </div>
              )}
            </div>

            {/* Video List */}
            {(loading || filterChanging) && videos.length === 0 ? (
              <div className="rag-empty">加载中...</div>
            ) : filterChanging ? (
              <div className="rag-empty">加载中...</div>
            ) : videos.length === 0 ? (
              <div className="rag-empty">暂无符合条件的视频</div>
            ) : (
              <div className="rag-table">
                <div className="rag-table-head">
                  <input
                    ref={selectAllRef}
                    type="checkbox"
                    className="rag-checkbox"
                    checked={allSelected}
                    onChange={toggleSelectAll}
                    aria-label="全选当前列表"
                  />
                  <span>视频</span>
                  <span>状态</span>
                  <span>操作</span>
                </div>

                {videos.map((v) => {
                  const key = videoKey(v);
                  const st = statusOf(v);
                  const isConfirming = confirmingKey === key;
                  const isRemoving = removingKey === key;
                  const isSelected = selected.has(key);
                  const hasError = !!v.process_error;

                  return (
                    <div
                      className={`rag-table-row${isSelected ? " selected" : ""}${hasError ? " row-failed" : ""}`}
                      key={key}
                    >
                      <input
                        type="checkbox"
                        className="rag-checkbox"
                        checked={isSelected}
                        onChange={() => toggleSelect(key)}
                        aria-label={`选择 ${v.title}`}
                      />

                      <div className="rag-video-cell">
                        <div className="rag-video-title" title={v.title}>
                          {v.title}
                        </div>
                        {hasError && (
                          <div className="rag-error-summary">
                            <span className="rag-error-icon">⚠</span>
                            <span className="rag-error-text">
                              {v.process_error?.length ?? 0 > 50
                                ? v.process_error?.slice(0, 50) + "..."
                                : v.process_error}
                            </span>
                          </div>
                        )}
                        <div className="rag-video-meta">
                          <span
                            className={`rag-platform-badge ${v.platform === "douyin" ? "douyin" : "bilibili"}`}
                          >
                            {platformLabel(v.platform)}
                          </span>
                          <span>{v.author}</span>
                          <span>{v.folder_title}</span>
                          <span>{formatDuration(v.duration)}</span>
                          {v.tags && v.tags.length > 0 && (
                            <span className="rag-video-tags">
                              {v.tags.map((t) => (
                                <span key={t} className="rag-tag-badge">
                                  {t}
                                </span>
                              ))}
                            </span>
                          )}
                        </div>
                      </div>

                      <span
                        className={`rag-status-badge ${st.cls}`}
                        title={v.process_error || undefined}
                      >
                        <span className="rag-status-icon">{st.icon}</span>
                        {st.text}
                      </span>

                      <div className="rag-row-actions">
                        {hasError && (
                          <button
                            className="rag-action-btn btn-error-detail"
                            onClick={() => setErrorDetailKey(key)}
                            title="查看错误详情"
                          >
                            详情
                          </button>
                        )}
                        {hasError && (
                          <button
                            className="rag-action-btn btn-retry-single"
                            onClick={() => handleRetrySingle(v)}
                            disabled={batchOperating}
                            title="重试此任务"
                          >
                            重试
                          </button>
                        )}
                        {!v.is_processed && !hasError && (
                          <button
                            className="rag-action-btn btn-ingest-single"
                            onClick={async () => {
                              // SubTask 8.1: ingesting 守卫，防止并发
                              if (ingesting) return;
                              // SubTask 8.4: 全局入库状态感知
                              try {
                                const running =
                                  await taskTrackerApi.listTasks({
                                    status: "running",
                                  });
                                if (running.tasks.length > 0) {
                                  showToast(
                                    "info",
                                    "已有入库任务进行中，请等待完成",
                                  );
                                  return;
                                }
                              } catch {
                                // 查询失败不阻塞
                              }
                              const payload: VideoIngestItem[] = [
                                {
                                  bvid: v.bvid,
                                  platform:
                                    v.platform === "douyin"
                                      ? "douyin"
                                      : "bilibili",
                                  tags: v.tags ?? undefined,
                                },
                              ];
                              const hasDouyin =
                                payload[0].platform === "douyin";
                              // SubTask 9.4: 清理可能存在的旧定时器，避免并发轮询
                              if (pollTimeoutRef.current) {
                                clearTimeout(pollTimeoutRef.current);
                                pollTimeoutRef.current = null;
                              }
                              setIngesting(true);
                              setIngestTaskId(null);
                              cancelledRef.current = false;
                              try {
                                const res =
                                  await knowledgeApi.ingestVideos(
                                    { videos: payload },
                                    biliSessionId,
                                    hasDouyin
                                      ? douyinSessionId
                                      : undefined,
                                  );
                                // SubTask 10.3: 保存 task_id 供取消使用
                                setIngestTaskId(res.task_id);
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
                                  if (
                                    Date.now() - startTime >
                                    MAX_POLL_DURATION
                                  ) {
                                    if (mountedRef.current) {
                                      setIngesting(false);
                                      setIngestTaskId(null);
                                      showToast(
                                        "error",
                                        "入库超时，请检查后端状态",
                                      );
                                    }
                                    return;
                                  }
                                  let s: BuildStatus;
                                  try {
                                    s =
                                      await knowledgeApi.getBuildStatus(
                                        res.task_id,
                                        biliSessionId,
                                      );
                                  } catch {
                                    consecutiveFailures++;
                                    if (
                                      consecutiveFailures >= MAX_FAILURES
                                    ) {
                                      if (mountedRef.current) {
                                        setIngesting(false);
                                        setIngestTaskId(null);
                                        showToast(
                                          "error",
                                          "查询入库状态失败，请检查网络后重试",
                                        );
                                      }
                                      return;
                                    }
                                    // 失败后 2 秒重试
                                    pollTimeoutRef.current = setTimeout(
                                      poll,
                                      2000,
                                    );
                                    return;
                                  }
                                  if (!mountedRef.current) return;
                                  if (cancelledRef.current) return;
                                  consecutiveFailures = 0; // 成功时重置
                                  setProgress(s);
                                  if (
                                    s.status === "running" ||
                                    s.status === "pending"
                                  ) {
                                    pollTimeoutRef.current = setTimeout(
                                      poll,
                                      1000,
                                    );
                                  } else {
                                    if (mountedRef.current) {
                                      setIngesting(false);
                                      setIngestTaskId(null);
                                      // SubTask 10.2: 区分部分失败/全部失败
                                      if (s.status === "completed") {
                                        const msg =
                                          s.failed && s.failed > 0
                                            ? `入库完成：成功 ${s.succeeded || 0}，失败 ${s.failed}`
                                            : (s.message ||
                                              "入库完成");
                                        showToast("success", msg);
                                        await loadVideos();
                                      } else {
                                        const msg =
                                          s.succeeded && s.succeeded > 0
                                            ? `部分失败：成功 ${s.succeeded}，失败 ${s.failed || 0}。${s.message || ""}`
                                            : `入库失败: ${s.message}`;
                                        showToast("error", msg);
                                      }
                                    }
                                  }
                                };
                                poll();
                              } catch (err) {
                                if (mountedRef.current) {
                                  setIngesting(false);
                                  setIngestTaskId(null);
                                  const msg =
                                    err instanceof Error
                                      ? err.message
                                      : "入库失败";
                                  showToast("error", msg);
                                }
                              }
                            }}
                            disabled={ingesting}
                            title="入库此视频"
                          >
                            入库
                          </button>
                        )}
                        {isConfirming ? (
                          <div className="rag-confirm-inline">
                            <button
                              className="rag-action-btn"
                              style={{ color: "var(--danger)" }}
                              onClick={() => handleRemove(v)}
                              disabled={isRemoving}
                            >
                              {isRemoving ? "出库中..." : "确认出库"}
                            </button>
                            <button
                              className="rag-action-btn"
                              onClick={() => setConfirmingKey(null)}
                              disabled={isRemoving}
                            >
                              取消
                            </button>
                          </div>
                        ) : v.is_processed ? (
                          <button
                            className="rag-action-btn"
                            onClick={() => setConfirmingKey(key)}
                            disabled={ingesting || isRemoving}
                            title="从 RAG 中移除"
                          >
                            出库
                          </button>
                        ) : null}
                        <button
                          className="rag-action-btn btn-delete-single"
                          onClick={() => handleDeleteSingle(v)}
                          disabled={batchOperating}
                          title="删除此任务"
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="rag-footer">
            <span>已选 {selectedCount} 个</span>
            <button
              className="btn btn-primary"
              onClick={handleIngest}
              disabled={ingesting || ingestTargets.length === 0}
              title={
                ingestTargets.length === 0
                  ? "请选择待入库的视频"
                  : `将入库 ${ingestTargets.length} 个视频`
              }
            >
              {ingesting
                ? `入库中${progress ? ` ${progress.progress}%` : ""}`
                : `入库选中${ingestTargets.length > 0 ? ` (${ingestTargets.length})` : ""}`}
            </button>
          </div>

          {/* Error Detail Modal */}
          {errorDetailKey && (
            <div
              className="modal-backdrop"
              onClick={() => {
                setErrorDetailKey(null);
                setErrorDetail(null);
              }}
            >
              <div
                className="modal-card error-detail-modal"
                onClick={(e) => e.stopPropagation()}
              >
                <div className="modal-title">错误详情</div>
                {errorDetail ? (
                  <div className="error-detail-content">
                    <div className="error-detail-row">
                      <span className="error-detail-label">任务 ID</span>
                      <span className="error-detail-value">
                        {errorDetail.task_id}
                      </span>
                    </div>
                    <div className="error-detail-row">
                      <span className="error-detail-label">错误信息</span>
                      <span className="error-detail-value error">
                        {errorDetail.error_message}
                      </span>
                    </div>
                    {errorDetail.error_stage && (
                      <div className="error-detail-row">
                        <span className="error-detail-label">错误阶段</span>
                        <span className="error-detail-value">
                          {errorDetail.error_stage}
                        </span>
                      </div>
                    )}
                    <div className="error-detail-row">
                      <span className="error-detail-label">重试次数</span>
                      <span className="error-detail-value">
                        {errorDetail.retry_count}
                      </span>
                    </div>
                    <div className="error-detail-row">
                      <span className="error-detail-label">追踪 ID</span>
                      <span className="error-detail-value mono">
                        {errorDetail.trace_id}
                      </span>
                    </div>
                    {errorDetail.suggestion && (
                      <div className="error-detail-row">
                        <span className="error-detail-label">建议</span>
                        <span className="error-detail-value suggestion">
                          {errorDetail.suggestion}
                        </span>
                      </div>
                    )}
                    <div className="error-detail-actions">
                      <button
                        className="btn btn-outline"
                        onClick={() => {
                          setErrorDetailKey(null);
                          setErrorDetail(null);
                        }}
                      >
                        关闭
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="error-detail-empty">无法加载错误详情</div>
                )}
              </div>
            </div>
          )}
        </aside>
      </div>

      {toast && (
        <div className={`rag-toast ${toast.type}`} role="status">
          {toast.msg}
        </div>
      )}
    </>
  );
}
