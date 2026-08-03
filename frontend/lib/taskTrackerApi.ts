/**
 * Task Tracker API — 统一任务追踪门面
 *
 * 提供对 taskApi 的统一封装，使用更符合业务语义的命名，
 * 方便组件层调用而无需直接处理底层 API 细节。
 *
 * 所有方法均返回 Promise，支持 AbortSignal 取消。
 */

import {
  getTask,
  listTasks,
  cancelTask,
  retryTask,
  deleteTask,
  batchRetry,
  batchDelete,
  batchCancel,
  getTaskError,
  pollTaskStatus,
  summarizeTaskProgress,
  getRetryableTasks,
  getCancelableTasks,
  getDeletableTasks,
  type TaskInfo,
  type TaskFilters,
  type TaskList,
  type ErrorDetail,
  type BatchResult,
  type TaskProgressSummary,
  type PollOptions,
} from "./taskApi";

export type {
  TaskInfo,
  TaskFilters,
  TaskList,
  ErrorDetail,
  BatchResult,
  TaskProgressSummary,
  TaskStatus,
} from "./taskApi";

export const taskTrackerApi = {
  /**
   * 获取单个任务的完整信息
   */
  async getTask(taskId: string): Promise<TaskInfo> {
    return getTask(taskId);
  },

  /**
   * 查询任务列表，支持按平台/状态/视频过滤
   */
  async listTasks(filters?: TaskFilters): Promise<TaskList> {
    return listTasks(filters);
  },

  /**
   * 取消正在进行的任务
   */
  async cancelTask(taskId: string): Promise<void> {
    return cancelTask(taskId);
  },

  /**
   * 重试失败的任务（单个）
   */
  async retryTask(taskId: string): Promise<TaskInfo> {
    return retryTask(taskId);
  },

  /**
   * 删除已完成的任务记录
   */
  async deleteTask(taskId: string): Promise<void> {
    return deleteTask(taskId);
  },

  /**
   * 批量重试失败的任务
   */
  async batchRetry(taskIds: string[]): Promise<BatchResult> {
    return batchRetry(taskIds);
  },

  /**
   * 批量删除任务
   */
  async batchDelete(taskIds: string[]): Promise<BatchResult> {
    return batchDelete(taskIds);
  },

  /**
   * 批量取消正在进行的任务
   */
  async batchCancel(taskIds: string[]): Promise<BatchResult> {
    return batchCancel(taskIds);
  },

  /**
   * 获取任务的错误详情和建议
   */
  async getTaskError(taskId: string): Promise<ErrorDetail> {
    return getTaskError(taskId);
  },

  /**
   * 轮询单个任务状态，直到进入终态
   */
  pollTaskStatus(
    taskId: string,
    interval = 2000,
    options?: Omit<PollOptions, "interval">,
  ): { promise: Promise<TaskInfo>; abort: () => void } {
    return pollTaskStatus(taskId, { ...options, interval });
  },

  /**
   * 汇总任务列表的状态统计
   */
  summarizeProgress(tasks: TaskInfo[]): TaskProgressSummary {
    return summarizeTaskProgress(tasks);
  },

  /**
   * 从任务列表中过滤出可重试的任务
   */
  getRetryableTasks(tasks: TaskInfo[]): TaskInfo[] {
    return getRetryableTasks(tasks);
  },

  /**
   * 从任务列表中过滤出可取消的任务
   */
  getCancelableTasks(tasks: TaskInfo[]): TaskInfo[] {
    return getCancelableTasks(tasks);
  },

  /**
   * 从任务列表中过滤出可删除的任务
   */
  getDeletableTasks(tasks: TaskInfo[]): TaskInfo[] {
    return getDeletableTasks(tasks);
  },
};

export type TaskTrackerApi = typeof taskTrackerApi;
