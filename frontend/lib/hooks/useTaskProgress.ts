"use client";

/**
 * useTaskProgress — 任务进度管理 Hook
 *
 * 用于轮询多个任务的状态，返回各任务的当前进度，
 * 支持取消轮询，当所有任务完成时触发回调。
 *
 * Usage:
 *   const { tasks, summary, isPolling, startPolling, stopPolling, refresh } =
 *     useTaskProgress(taskIds, { interval: 2000, onAllComplete });
 */

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import {
  getTask,
  summarizeTaskProgress,
  type TaskInfo,
  type TaskProgressSummary,
} from "../taskApi";

export interface UseTaskProgressOptions {
  /** 轮询间隔（毫秒），默认 2000 */
  interval?: number;
  /** 所有任务完成时的回调 */
  onAllComplete?: (tasks: TaskInfo[]) => void;
  /** 单个任务状态变化回调 */
  onTaskUpdate?: (task: TaskInfo) => void;
  /** 错误回调 */
  onError?: (error: Error, taskId: string) => void;
  /** 自动开始轮询，默认 true */
  autoStart?: boolean;
}

export interface UseTaskProgressReturn {
  /** 各任务的最新状态，key 为 taskId */
  tasks: Record<string, TaskInfo>;
  /** 进度汇总统计 */
  summary: TaskProgressSummary;
  /** 是否正在轮询 */
  isPolling: boolean;
  /** 启动轮询 */
  startPolling: () => void;
  /** 停止轮询 */
  stopPolling: () => void;
  /** 手动刷新一次所有任务状态 */
  refresh: () => Promise<void>;
  /** 最后一次错误 */
  error: Error | null;
}

const TERMINAL_STATUSES = ["success", "failed", "cancelled"] as const;

function areAllTerminal(tasks: Record<string, TaskInfo>): boolean {
  const values = Object.values(tasks);
  if (values.length === 0) return false;
  return values.every((t) =>
    TERMINAL_STATUSES.includes(t.status as (typeof TERMINAL_STATUSES)[number]),
  );
}

export function useTaskProgress(
  taskIds: string[],
  options: UseTaskProgressOptions = {},
): UseTaskProgressReturn {
  const {
    interval = 2000,
    onAllComplete,
    onTaskUpdate,
    onError,
    autoStart = true,
  } = options;

  const [tasks, setTasks] = useState<Record<string, TaskInfo>>({});
  const [isPolling, setIsPolling] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  const taskIdsRef = useRef(taskIds);
  const intervalRef = useRef(interval);
  const onAllCompleteRef = useRef(onAllComplete);
  const onTaskUpdateRef = useRef(onTaskUpdate);
  const onErrorRef = useRef(onError);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stoppedRef = useRef(false);
  const completedFiredRef = useRef(false);
  const isMountedRef = useRef(true);

  useEffect(() => {
    taskIdsRef.current = taskIds;
  }, [taskIds]);

  useEffect(() => {
    intervalRef.current = interval;
  }, [interval]);

  useEffect(() => {
    onAllCompleteRef.current = onAllComplete;
  }, [onAllComplete]);

  useEffect(() => {
    onTaskUpdateRef.current = onTaskUpdate;
  }, [onTaskUpdate]);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    return () => {
      isMountedRef.current = false;
      stoppedRef.current = true;
      if (pollTimerRef.current) {
        clearTimeout(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, []);

  const pollOnceRef = useRef<(() => Promise<void>) | null>(null);

  const fetchAll = useCallback(async () => {
    const ids = taskIdsRef.current;
    if (ids.length === 0) return;

    const results = await Promise.allSettled(
      ids.map((id) => getTask(id)),
    );

    const newTasks: Record<string, TaskInfo> = {};
    for (let i = 0; i < ids.length; i++) {
      const id = ids[i];
      const result = results[i];
      if (result.status === "fulfilled") {
        newTasks[id] = result.value;
        onTaskUpdateRef.current?.(result.value);
      } else {
        const err = new Error(String(result.reason));
        setError(err);
        onErrorRef.current?.(err, id);
      }
    }

    if (isMountedRef.current) {
      setTasks((prev) => ({ ...prev, ...newTasks }));
    }
  }, []);

  useEffect(() => {
    pollOnceRef.current = async () => {
      if (stoppedRef.current) return;

      await fetchAll();

      setTasks((currentTasks) => {
        if (areAllTerminal(currentTasks)) {
          if (!completedFiredRef.current) {
            completedFiredRef.current = true;
            const taskList = Object.values(currentTasks);
            onAllCompleteRef.current?.(taskList);
            if (pollTimerRef.current) {
              clearTimeout(pollTimerRef.current);
              pollTimerRef.current = null;
            }
            setIsPolling(false);
          }
        }
        return currentTasks;
      });

      if (stoppedRef.current) return;

      pollTimerRef.current = setTimeout(() => {
        pollOnceRef.current?.();
      }, intervalRef.current);
    };
  }, [fetchAll]);

  const startPolling = useCallback(() => {
    stoppedRef.current = false;
    completedFiredRef.current = false;
    setIsPolling(true);
    setError(null);

    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
    }

    pollOnceRef.current?.();
  }, []);

  const stopPolling = useCallback(() => {
    stoppedRef.current = true;
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
    setIsPolling(false);
  }, []);

  const refresh = useCallback(async () => {
    await fetchAll();
  }, [fetchAll]);

  useEffect(() => {
    if (autoStart && taskIds.length > 0) {
      queueMicrotask(() => startPolling());
    }
    return () => {
      stopPolling();
    };
  }, [autoStart, taskIds.length, startPolling, stopPolling]);

  const summary = useMemo(() => {
    const taskList = Object.values(tasks);
    return summarizeTaskProgress(taskList);
  }, [tasks]);

  return {
    tasks,
    summary,
    isPolling,
    startPolling,
    stopPolling,
    refresh,
    error,
  };
}
