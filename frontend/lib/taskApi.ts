/**
 * Task API — 批量操作与实时进度
 *
 * 增强任务 API 服务，提供：
 * - 轮询机制支持
 * - 批量操作 API（重试、删除、取消）
 * - 错误详情获取
 * - 按平台/状态过滤任务
 */

import { API_BASE_URL, ApiError } from "./api";

// ============================================================================
// 类型定义
// ============================================================================

export type TaskStatus =
  | "pending"
  | "running"
  | "success"
  | "failed"
  | "cancelled"
  | "retrying";

export interface TaskInfo {
  task_id: string;
  video_id: string;
  status: TaskStatus;
  current_step: string;
  progress: number;
  error_message?: string;
  error_stage?: string;
  retry_count: number;
  trace_id: string;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface TaskFilters {
  platform?: string;
  status?: TaskStatus;
  video_id?: string;
}

export interface TaskList {
  total: number;
  tasks: TaskInfo[];
}

export interface ErrorDetail {
  task_id: string;
  error_message: string;
  error_stage?: string;
  trace_id?: string | null;
  retry_count: number;
  failed_at?: string | null;
  suggestion?: string | null;
}

export interface BatchResult {
  total: number;
  succeeded: number;
  failed: number;
  results: Array<{
    task_id: string;
    status: "ok" | "fail";
    message?: string;
  }>;
}

export interface TaskProgressSummary {
  pending: number;
  running: number;
  success: number;
  failed: number;
  cancelled: number;
  retrying: number;
  total: number;
}

// ============================================================================
// 通用请求工具
// ============================================================================

interface TaskRequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

async function request<T>(path: string, options: TaskRequestOptions = {}): Promise<T> {
  const url = `${API_BASE_URL}${path}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...options.headers,
  };

  let response: Response;
  try {
    response = await fetch(url, {
      method: options.method || "GET",
      headers,
      body:
        options.body instanceof FormData
          ? options.body
          : options.body !== undefined
            ? JSON.stringify(options.body)
            : undefined,
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw err;
    }
    const rawMsg = err instanceof Error ? err.message : "网络请求失败";
    throw new ApiError(rawMsg, 0);
  }

  if (!response.ok) {
    let detail: unknown;
    try {
      detail = await response.json();
    } catch {
      try {
        detail = await response.text();
      } catch {
        detail = undefined;
      }
    }
    const message =
      (detail && typeof detail === "object" && "detail" in detail
        ? String((detail as Record<string, unknown>).detail)
        : null) || `请求失败 (${response.status})`;
    throw new ApiError(message, response.status, detail);
  }

  const contentLength = response.headers.get("content-length");
  if (contentLength === "0") {
    return {} as T;
  }

  try {
    return await response.json();
  } catch {
    return {} as T;
  }
}

function buildQuery(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const str = search.toString();
  return str ? `?${str}` : "";
}

// ============================================================================
// 轮询工具
// ============================================================================

export interface PollOptions {
  interval?: number;
  maxAttempts?: number;
  signal?: AbortSignal;
  onStatusChange?: (status: TaskStatus) => void;
}

/**
 * 轮询任务状态，直到任务完成或达到最大尝试次数。
 *
 * @param taskId 任务 ID
 * @param interval 轮询间隔（毫秒），默认 2000
 * @param maxAttempts 最大尝试次数，默认 0（无限）
 * @param signal 用于取消轮询的 AbortSignal
 * @param onStatusChange 状态变化回调
 */
export function pollTaskStatus(
  taskId: string,
  options: PollOptions = {},
): {
  promise: Promise<TaskInfo>;
  abort: () => void;
} {
  const {
    interval = 2000,
    maxAttempts = 0,
    signal,
    onStatusChange,
  } = options;

  const abortController = new AbortController();
  let currentAttempt = 0;

  const promise = new Promise<TaskInfo>((resolve, reject) => {
    const poll = async () => {
      if (abortController.signal.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }

      if (signal?.aborted) {
        reject(new DOMException("Aborted", "AbortError"));
        return;
      }

      if (maxAttempts > 0 && currentAttempt >= maxAttempts) {
        reject(new Error(`轮询超时：已达到最大尝试次数 ${maxAttempts}`));
        return;
      }

      currentAttempt++;

      try {
        const task = await getTask(taskId);
        onStatusChange?.(task.status);

        const terminalStatuses: TaskStatus[] = [
          "success",
          "failed",
          "cancelled",
        ];

        if (terminalStatuses.includes(task.status)) {
          resolve(task);
          return;
        }

        setTimeout(poll, interval);
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) {
          reject(err);
          return;
        }
        setTimeout(poll, interval);
      }
    };

    poll();
  });

  const abort = () => {
    abortController.abort();
  };

  if (signal) {
    signal.addEventListener("abort", abort);
  }

  return { promise, abort };
}

// ============================================================================
// 任务 API
// ============================================================================

/**
 * 获取单个任务信息
 */
export async function getTask(taskId: string): Promise<TaskInfo> {
  return request<TaskInfo>(`/api/tasks/${encodeURIComponent(taskId)}`);
}

/**
 * 按条件查询任务列表
 */
export async function listTasks(
  filters: TaskFilters = {},
): Promise<TaskList> {
  return request<TaskList>(
    `/api/tasks${buildQuery({
      platform: filters.platform,
      status: filters.status,
      video_id: filters.video_id,
    })}`,
  );
}

/**
 * 获取任务错误详情
 */
export async function getTaskError(taskId: string): Promise<ErrorDetail> {
  return request<ErrorDetail>(
    `/api/tasks/${encodeURIComponent(taskId)}/error`,
  );
}

/**
 * 取消任务
 */
export async function cancelTask(taskId: string): Promise<void> {
  return request<void>(
    `/api/tasks/${encodeURIComponent(taskId)}/cancel`,
    { method: "POST" },
  );
}

/**
 * 重试单个任务
 */
export async function retryTask(taskId: string): Promise<TaskInfo> {
  return request<TaskInfo>(
    `/api/tasks/${encodeURIComponent(taskId)}/retry`,
    { method: "POST" },
  );
}

/**
 * 删除单个任务
 */
export async function deleteTask(taskId: string): Promise<void> {
  return request<void>(
    `/api/tasks/${encodeURIComponent(taskId)}`,
    { method: "DELETE" },
  );
}

// ============================================================================
// 批量操作 API
// ============================================================================

/**
 * 批量重试任务
 */
export async function batchRetry(taskIds: string[]): Promise<BatchResult> {
  return request<BatchResult>("/api/tasks/batch/retry", {
    method: "POST",
    body: { task_ids: taskIds },
  });
}

/**
 * 批量删除任务
 */
export async function batchDelete(taskIds: string[]): Promise<BatchResult> {
  return request<BatchResult>("/api/tasks/batch/delete", {
    method: "POST",
    body: { task_ids: taskIds },
  });
}

/**
 * 批量取消任务
 */
export async function batchCancel(taskIds: string[]): Promise<BatchResult> {
  return request<BatchResult>("/api/tasks/batch/cancel", {
    method: "POST",
    body: { task_ids: taskIds },
  });
}

// ============================================================================
// 统计工具
// ============================================================================

/**
 * 汇总多个任务的状态统计
 */
export function summarizeTaskProgress(
  tasks: TaskInfo[],
): TaskProgressSummary {
  const summary: TaskProgressSummary = {
    pending: 0,
    running: 0,
    success: 0,
    failed: 0,
    cancelled: 0,
    retrying: 0,
    total: tasks.length,
  };

  for (const task of tasks) {
    summary[task.status]++;
  }

  return summary;
}

/**
 * 过滤出可重试的任务
 */
export function getRetryableTasks(tasks: TaskInfo[]): TaskInfo[] {
  return tasks.filter(
    (t) => t.status === "failed" && t.retry_count < 3,
  );
}

/**
 * 过滤出可取消的任务
 */
export function getCancelableTasks(tasks: TaskInfo[]): TaskInfo[] {
  return tasks.filter(
    (t) => t.status === "pending" || t.status === "running" || t.status === "retrying",
  );
}

/**
 * 过滤出可删除的任务
 */
export function getDeletableTasks(tasks: TaskInfo[]): TaskInfo[] {
  return tasks.filter(
    (t) => ["success", "failed", "cancelled"].includes(t.status),
  );
}
