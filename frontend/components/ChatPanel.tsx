"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { chatApi, knowledgeApi, KnowledgeStats, API_BASE_URL, openExternal } from "@/lib/api";
import ConfirmDialog from "./ConfirmDialog";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  sources?: Array<{ bvid: string; title: string; url: string }>;
  trace?: TraceEvent[];
  traceOpen?: boolean;
  traceDone?: boolean;
}

interface TraceEvent {
  type: "status" | "scope" | "retrieval" | "snippet";
  stage?: string;
  message?: string;
  title?: string;
  preview?: string;
  url?: string;
  folder_count?: number;
  video_count?: number;
  vector_count?: number;
  keyword_count?: number;
  final_count?: number;
  elapsed_ms?: number;
}

type StreamEvent =
  | TraceEvent
  | { type: "token"; content: string }
  | { type: "sources"; items: Array<{ bvid: string; title: string; url: string }> }
  | { type: "done" }
  | { type: "error"; message: string };

interface Props {
  statsKey?: number;
  sessionId?: string;
  folderIds?: number[];
  platform?: string;
}

function ExecutionTrace({
  events,
  open,
  done,
  onToggle,
}: {
  events: TraceEvent[];
  open: boolean;
  done: boolean;
  onToggle: () => void;
}) {
  const snippets = events.filter((event) => event.type === "snippet");
  const steps = events.filter((event) => event.type !== "snippet");
  const latest = steps.at(-1)?.message || (done ? "执行完成" : "正在处理");

  return (
    <div className={`execution-trace ${done ? "done" : "running"}`}>
      <button
        type="button"
        className="execution-trace-head"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className="execution-trace-signal" aria-hidden="true" />
        <span className="execution-trace-title">{done ? "执行过程" : latest}</span>
        {done && <span className="execution-trace-summary">{steps.length} 个步骤</span>}
        <svg className={open ? "open" : ""} viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path d="m6 8 4 4 4-4" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {open && (
        <div className="execution-trace-body">
          <div className="execution-steps">
            {steps.map((event, index) => (
              <div className="execution-step" key={`${event.type}-${event.stage}-${index}`}>
                <span className="execution-step-mark">{index + 1}</span>
                <span>{event.message}</span>
                {event.elapsed_ms != null && (
                  <small>{(event.elapsed_ms / 1000).toFixed(2)}s</small>
                )}
              </div>
            ))}
          </div>

          {snippets.length > 0 && (
            <div className="execution-snippets">
              <div className="execution-snippets-label">召回片段</div>
              {snippets.map((event, index) => (
                <div
                  className="execution-snippet"
                  key={`${event.title}-${index}`}
                  title="按住 Ctrl+左键 在浏览器打开"
                  style={{ cursor: "pointer" }}
                  onClick={(e) => {
                    if ((e.ctrlKey || e.metaKey) && event.url) {
                      e.preventDefault();
                      e.stopPropagation();
                      openExternal(event.url);
                    }
                  }}
                >
                  <strong>{event.title}</strong>
                  <span>{event.preview}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ChatPanel({ statsKey, sessionId, folderIds, platform }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [platformFilter, setPlatformFilter] = useState<"bilibili" | "douyin" | "all">((platform || "all") as "bilibili" | "douyin" | "all");
  useEffect(() => { setPlatformFilter((platform || "all") as "bilibili" | "douyin" | "all"); }, [platform]);
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [clearConfirmOpen, setClearConfirmOpen] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  // 用户主动向上滚动时暂停自动滚动，回到底部附近后恢复
  const autoScrollRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    knowledgeApi.getStats().then(setStats).catch(() => { });
  }, [statsKey]);

  // 监听滚动：用户主动向上滚动时关闭自动滚动，回到底部附近时恢复
  useEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const handleScroll = () => {
      const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
      autoScrollRef.current = distance < 80;
    };
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => el.removeEventListener("scroll", handleScroll);
  }, []);

  // 仅在自动滚动开启时（用户未向上翻看历史）才回到底部
  useEffect(() => {
    if (!autoScrollRef.current) return;
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // 组件卸载时中止未完成的请求
  useEffect(() => () => { abortRef.current?.abort(); }, []);

  const stopGeneration = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    setLoading(false);
  }, []);

  const send = async () => {
    if (!input.trim() || loading) return;
    const q = input.trim();
    setInput("");
    // 使用 randomUUID 避免同毫秒内 Date.now 碰撞导致 React key 冲突
    const userId = typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const assistantId = typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID()
      : `${Date.now() + 1}-${Math.random().toString(36).slice(2)}`;
    setMessages((prev) => [
      ...prev,
      { id: userId, role: "user", content: q },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        sources: [],
        trace: [{ type: "status", stage: "connecting", message: "正在连接问答服务" }],
        traceOpen: true,
        traceDone: false,
      },
    ]);
    setLoading(true);

    // 为本次请求创建 AbortController，支持停止生成
    const controller = new AbortController();
    abortRef.current = controller;

    let receivedEvent = false;
    try {
      const response = await fetch(`${API_BASE_URL}/chat/ask/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: q,
          session_id: sessionId,
          folder_ids: folderIds,
          // 后端契约：platform=null 表示"全部平台"
          // 与 chatApi.ask 行为一致，避免 "all" 字符串在前后端产生语义歧义
          platform: platformFilter === "all" ? null : platformFilter,
        }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error("流式接口不可用");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let answerBuffer = "";
      let pendingBuffer = "";
      let pendingSources: Array<{ bvid: string; title: string; url: string }> = [];
      let streamCompleted = false;
      // 流式渲染节流：累积 token，每 80ms 批量刷新一次，避免每个 token 触发重渲染
      let lastFlushTime = 0;
      const FLUSH_INTERVAL = 80;
      const flushContent = () => {
        lastFlushTime = Date.now();
        const content = answerBuffer;
        setMessages((prev) =>
          prev.map((message) =>
            message.id === assistantId ? { ...message, content } : message
          )
        );
      };

      const applyEvent = (event: StreamEvent) => {
        receivedEvent = true;
        if (event.type === "token") {
          answerBuffer += event.content;
          // 节流：距上次刷新超过 80ms 才更新 state
          const now = Date.now();
          if (now - lastFlushTime >= FLUSH_INTERVAL) {
            flushContent();
          }
          return;
        }
        if (event.type === "sources") {
          pendingSources = event.items;
          return;
        }
        if (event.type === "done") {
          streamCompleted = true;
          // 完成时强制刷新最终内容，确保不丢失尾部 token
          flushContent();
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantId
                ? { ...message, sources: pendingSources, traceOpen: false, traceDone: true }
                : message
            )
          );
          return;
        }
        if (event.type === "error") {
          streamCompleted = true;
          // 错误时也强制刷新
          flushContent();
          setMessages((prev) =>
            prev.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    content: answerBuffer || `错误: ${event.message}`,
                    trace: [...(message.trace || []), { type: "status", stage: "error", message: event.message }],
                    traceOpen: true,
                    traceDone: true,
                  }
                : message
            )
          );
          return;
        }
        setMessages((prev) =>
          prev.map((message) =>
            message.id === assistantId
              ? { ...message, trace: [...(message.trace || []), event] }
              : message
          )
        );
      };

      const parseLine = (line: string) => {
        if (!line.trim()) return;
        // F6: 容错 JSON 解析，避免后端偶尔输出非 JSON 行直接中断流式渲染
        let event: unknown;
        try {
          event = JSON.parse(line);
        } catch (err) {
          console.warn("[ChatPanel] 跳过无法解析的流式行:", line, err);
          return;
        }
        // 运行时 type guard：仅当 event 是对象且包含合法 type 字段时才应用
        if (!event || typeof event !== "object" || !("type" in event)) {
          console.warn("[ChatPanel] 跳过结构异常的流式事件:", line);
          return;
        }
        const evt = event as { type: unknown };
        const VALID_TYPES = new Set([
          "status", "retrieval", "snippet", "sources", "token", "trace",
          "error", "done", "route_fallback",
        ]);
        if (typeof evt.type !== "string" || !VALID_TYPES.has(evt.type)) {
          console.warn("[ChatPanel] 跳过未知 type 的流式事件:", evt.type);
          return;
        }
        applyEvent(event as StreamEvent);
      };

      // F5: 流式读取超时保护。若 90s 内无任何数据，判定连接异常并中断
      const READ_TIMEOUT_MS = 90_000;
      while (true) {
        let timer: ReturnType<typeof setTimeout> | null = null;
        let readResult: { value: Uint8Array | undefined; done: boolean };
        try {
          readResult = await Promise.race([
            reader.read().then((r) => {
              if (timer) clearTimeout(timer);
              return r;
            }),
            new Promise<never>((_, reject) => {
              timer = setTimeout(() => reject(new Error("流式响应超时")), READ_TIMEOUT_MS);
            }),
          ]);
        } catch (readErr) {
          if (timer) clearTimeout(timer);
          throw readErr;
        }
        const { value, done: doneReading } = readResult;
        if (value) {
          pendingBuffer += decoder.decode(value, { stream: !doneReading });
          const lines = pendingBuffer.split("\n");
          pendingBuffer = lines.pop() || "";
          for (const line of lines) {
            parseLine(line);
          }
        }
        if (doneReading) break;
      }
      if (pendingBuffer.trim()) {
        parseLine(pendingBuffer);
      }
      // 流结束后兜底刷新，确保节流期间累积的 token 全部写入
      flushContent();
      if (!streamCompleted) {
        throw new Error("流式响应意外结束");
      }
    } catch (streamError) {
      // 用户主动停止：保留已生成内容，不报错
      if (streamError instanceof DOMException && streamError.name === "AbortError") {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: m.content || "（已停止生成）",
                  traceOpen: false,
                  traceDone: true,
                }
              : m
          )
        );
        setLoading(false);
        abortRef.current = null;
        return;
      }
      if (!receivedEvent) {
        try {
          const res = await chatApi.ask(q, sessionId, folderIds, platformFilter);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    content: res.answer,
                    sources: res.sources,
                    trace: [...(m.trace || []), { type: "status", stage: "fallback", message: "流式过程不可用，已切换普通回答" }],
                    traceOpen: false,
                    traceDone: true,
                  }
                : m
            )
          );
        } catch (err) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId
                ? {
                    ...m,
                    content: `错误: ${err instanceof Error ? err.message : "请求失败"}`,
                    traceOpen: true,
                    traceDone: true,
                  }
                : m
            )
          );
        }
      } else {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId
              ? {
                  ...m,
                  content: m.content || `错误: ${streamError instanceof Error ? streamError.message : "流式响应失败"}`,
                  trace: [
                    ...(m.trace || []),
                    {
                      type: "status",
                      stage: "error",
                      message: streamError instanceof Error ? streamError.message : "流式响应失败",
                    },
                  ],
                  traceOpen: true,
                  traceDone: true,
                }
              : m
          )
        );
      }
    }
    setLoading(false);
    abortRef.current = null;
  };

  return (
    <div className="panel-inner">
      <div className="panel-header">
        <div>
          <div className="panel-title">对话工作台</div>
          {stats && (stats.total_videos ?? 0) > 0 && (
            <div className="panel-subtitle">已收录 {stats.total_videos} 个视频</div>
          )}
        </div>
        {platform === undefined && (
          <div className="platform-filter">
            <button className={`chip ${platformFilter === "all" ? "active" : ""}`} onClick={() => setPlatformFilter("all")}>全部</button>
            <button className={`chip ${platformFilter === "bilibili" ? "active" : ""}`} onClick={() => setPlatformFilter("bilibili")}>📺 B站</button>
            <button className={`chip ${platformFilter === "douyin" ? "active" : ""}`} onClick={() => setPlatformFilter("douyin")}>🎵 抖音</button>
          </div>
        )}
        {messages.length > 0 && (
          <button onClick={() => setClearConfirmOpen(true)} className="btn btn-ghost" title="清空">
            清空对话
          </button>
        )}
      </div>

      <div className="panel-body">
        <div className="chat-scroll" ref={scrollContainerRef}>
          {messages.length === 0 ? (
            <div className="empty-state">
              <div>
                <div className="status-pill">检索就绪</div>
                <p className="text-sm text-[var(--muted)] mt-3">把收藏夹变成可提问的知识库</p>
              </div>
              <div className="prompt-grid">
                {[
                  "总结收藏夹里最有价值的内容",
                  "有哪些适合快速复习的系列？",
                  "列出与某个主题相关的视频并给出关键点",
                  "按主题整理我的收藏夹内容",
                  "用一句话概括每个视频的重点",
                  "推荐3个最适合入门的学习视频",
                ].map((q, i) => (
                  <button key={i} onClick={() => setInput(q)} className="prompt-chip">
                    {q}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="chat-window">
              {messages.map((m) => (
                <div key={m.id} className={`message ${m.role}`}>
                  <div className="message-bubble">
                    {m.trace && m.trace.length > 0 && (
                      <ExecutionTrace
                        events={m.trace}
                        open={m.traceOpen ?? false}
                        done={m.traceDone ?? false}
                        onToggle={() =>
                          setMessages((prev) =>
                            prev.map((message) =>
                              message.id === m.id ? { ...message, traceOpen: !message.traceOpen } : message
                            )
                          )
                        }
                      />
                    )}
                    <ReactMarkdown className="markdown" remarkPlugins={[remarkGfm]}>
                      {m.content}
                    </ReactMarkdown>
                    {m.sources && m.sources.length > 0 && (
                      <div className="source-list">
                        {m.sources.map((s, i) => (
                          <span
                            key={i}
                            className="source-link"
                            title={`${s.title} · 按住 Ctrl+左键 在浏览器打开`}
                            style={{ cursor: "pointer" }}
                            onClick={(e) => {
                              if (e.ctrlKey || e.metaKey) {
                                e.preventDefault();
                                e.stopPropagation();
                                openExternal(s.url);
                              }
                            }}
                          >
                            {s.title}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ))}
              <div ref={endRef} />
            </div>
          )}
        </div>
      </div>

      <div className="panel-footer">
        <div className="flex gap-2">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="输入问题... (Enter 发送，Shift+Enter 换行)"
            className="input"
            rows={2}
            disabled={loading}
            style={{ resize: "vertical", minHeight: "44px", maxHeight: "160px", fontFamily: "inherit" }}
          />
          {loading ? (
            <button onClick={stopGeneration} className="btn btn-primary" title="停止生成">
              停止
            </button>
          ) : (
            <button onClick={send} disabled={!input.trim()} className="btn btn-primary">
              发送
            </button>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={clearConfirmOpen}
        title="清空对话"
        message="确定要清空所有对话记录吗？此操作不可撤销。"
        confirmText="清空"
        danger
        onConfirm={() => {
          setMessages([]);
          setClearConfirmOpen(false);
        }}
        onCancel={() => setClearConfirmOpen(false)}
      />
    </div>
  );
}
