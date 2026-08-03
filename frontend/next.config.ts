import type { NextConfig } from "next";

/**
 * ClipMind 前端配置
 *
 * 静态导出模式（output: "export"）：
 * - 构建产物在 out/ 目录，供 Tauri 加载或 Python 后端托管
 * - 图片优化关闭（静态导出不支持服务端优化）
 * - API 地址通过环境变量注入，默认指向本地后端
 */
const nextConfig: NextConfig = {
  // 静态导出：生成纯 HTML/CSS/JS，无需 Node.js 运行时
  output: "export",

  // 静态导出不支持图片优化
  images: {
    unoptimized: true,
    remotePatterns: [
      { protocol: "https", hostname: "**.hdslb.com" },
      { protocol: "https", hostname: "**.bilivideo.com" },
      { protocol: "https", hostname: "**.douyinpic.com" },
      { protocol: "https", hostname: "**.iesdouyin.com" },
    ],
  },

  // 后端 API 地址：构建时注入。
  // 注意：打包模式下前端会被后端同源托管（窗口导航到 127.0.0.1:8000），
  // detectApiBaseUrl() 会返回相对路径 ""，此环境变量仅在开发模式（next dev）下生效。
  // lib.rs 的 --proxy-bypass-list 显式包含 127.0.0.1 和 localhost，两者均可。
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000",
  },

  // 静态导出忽略 trailing slash
  trailingSlash: true,
};

export default nextConfig;
