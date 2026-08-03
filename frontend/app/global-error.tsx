"use client";

import { useEffect } from "react";

/**
 * 全局错误边界：捕获根 layout 或根 segment 抛出的未处理错误，
 * 避免整页白屏，并提供重试入口。
 *
 * Next.js App Router 约定：本文件必须导出 default 组件，且自身会成为
 * 新的根 layout 替代者，因此需要自带 <html> 与 <body>。
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // 上报到日志（当前仅 console，可替换为 Sentry / 自建上报）
    console.error("[GlobalError]", error);
  }, [error]);

  return (
    <html lang="zh-CN">
      <body className="antialiased">
        <div
          style={{
            minHeight: "100vh",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "24px",
            background: "var(--bg, #fafafa)",
            color: "var(--ink, #222)",
          }}
        >
          <div
            style={{
              maxWidth: 480,
              width: "100%",
              padding: 32,
              border: "1px solid var(--border, #e5e5e5)",
              borderRadius: 16,
              background: "var(--card, #fff)",
              boxShadow: "0 12px 36px rgba(0,0,0,0.06)",
            }}
          >
            <div style={{ fontSize: 13, color: "#888", marginBottom: 8 }}>
              APPLICATION ERROR
            </div>
            <h1 style={{ fontSize: 20, margin: "0 0 12px", fontWeight: 600 }}>
              页面出现异常
            </h1>
            <p style={{ fontSize: 14, color: "#666", margin: "0 0 20px", lineHeight: 1.6 }}>
              抱歉，应用遇到未预期的错误。可尝试重新加载；若问题持续出现，请清除本地登录态后重试。
            </p>
            {error?.message && (
              <pre
                style={{
                  fontSize: 12,
                  color: "#999",
                  background: "#f7f7f7",
                  padding: 12,
                  borderRadius: 8,
                  margin: "0 0 20px",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  maxHeight: 160,
                  overflow: "auto",
                }}
              >
                {error.message}
                {error.digest ? `\n[digest: ${error.digest}]` : ""}
              </pre>
            )}
            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={() => reset()}
                style={{
                  flex: 1,
                  padding: "10px 14px",
                  borderRadius: 10,
                  border: "none",
                  background: "var(--accent, #2563eb)",
                  color: "#fff",
                  fontSize: 14,
                  cursor: "pointer",
                }}
              >
                重试
              </button>
              <button
                onClick={() => {
                  if (typeof window === "undefined") return;
                  window.location.reload();
                }}
                style={{
                  flex: 1,
                  padding: "10px 14px",
                  borderRadius: 10,
                  border: "1px solid var(--border, #e5e5e5)",
                  background: "transparent",
                  color: "var(--ink, #222)",
                  fontSize: 14,
                  cursor: "pointer",
                }}
              >
                刷新页面
              </button>
            </div>
          </div>
        </div>
      </body>
    </html>
  );
}
