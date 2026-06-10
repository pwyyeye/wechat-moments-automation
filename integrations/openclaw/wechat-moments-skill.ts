/**
 * OpenClaw Skill: WeChat Moments Automation
 *
 * 通过 Telegram/WhatsApp/Discord 等消息平台远程控制微信朋友圈发布。
 *
 * 使用方式（在聊天中发送）：
 *   /publish 今天天气真好
 *   /publish 分享照片 | photo1.jpg photo2.jpg
 *   /status                    查看系统状态
 *   /schedule add 0 9 * * * | 早安
 *   /schedule list             查看定时任务
 *   /schedule remove <id>      取消定时任务
 *   /history                   查看发布历史
 *
 * 安装：
 *   1. 将本文件放入 OpenClaw skills 目录
 *   2. 确保 Python API Server 在 127.0.0.1:18080 运行
 *   3. 重启 OpenClaw Gateway
 *
 * Author: 版本无关微信自动化系统
 */

import { Skill, SkillContext, SkillResult } from "@openclaw/skills";

// ═══════════════════════════════════════════════════════════════
// 配置
// ═══════════════════════════════════════════════════════════════

const API_BASE_URL = process.env.WECHAT_MOMENTS_API || "http://127.0.0.1:18080";
const API_KEY = process.env.WECHAT_MOMENTS_API_KEY || "";

interface ApiResponse<T> {
  success?: boolean;
  status?: string;
  data?: T;
  error?: string;
}

// ═══════════════════════════════════════════════════════════════
// API 客户端
// ═══════════════════════════════════════════════════════════════

async function apiCall<T = any>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options.headers as Record<string, string>) || {}),
  };

  if (API_KEY) {
    headers["X-API-Key"] = API_KEY;
  }

  const response = await fetch(url, {
    ...options,
    headers,
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`API ${response.status}: ${errorText}`);
  }

  return response.json();
}

// ═══════════════════════════════════════════════════════════════
// 命令处理器
// ═══════════════════════════════════════════════════════════════

async function handlePublish(args: string): Promise<string> {
  // 解析参数: "今天天气真好 | photo1.jpg photo2.jpg"
  const parts = args.split("|").map(s => s.trim());
  const text = parts[0] || "";
  const images = parts[1] ? parts[1].split(/\s+/) : [];

  if (!text) {
    return "❌ 请提供朋友圈文字内容。\n用法: /publish 今天天气真好";
  }

  try {
    const result = await apiCall<any>("/api/publish", {
      method: "POST",
      body: JSON.stringify({ text, images }),
    });

    if (result.success) {
      const steps = Object.entries(result.step_times || {})
        .map(([k, v]) => `  ${k}: ${Number(v).toFixed(1)}s`)
        .join("\n");

      return [
        `✅ **朋友圈已发布**`,
        `⏱ 耗时: ${result.elapsed_seconds?.toFixed(1)}s`,
        ``,
        `步骤耗时:`,
        steps,
        ``,
        `📝 ${text.substring(0, 50)}${text.length > 50 ? "..." : ""}`,
        images.length > 0 ? `🖼 ${images.length} 张图片` : "",
      ].filter(Boolean).join("\n");
    } else {
      return `❌ 发布失败: ${result.error || "未知错误"}`;
    }
  } catch (e: any) {
    return `❌ API 调用失败: ${e.message}\n请确认 API Server 是否运行在 ${API_BASE_URL}`;
  }
}

async function handleStatus(): Promise<string> {
  try {
    const status = await apiCall<any>("/api/status");

    return [
      `**系统状态**: ${status.status === "running" ? "🟢 运行中" : "🔴 空闲"}`,
      ``,
      `**微信**: ${status.wechat?.logged_in ? "✅ 已登录" : "❌ 未登录"}`,
      `**风控**: ${status.risk?.level || "未知"}`,
      `**今日发布**: ${status.daily?.posts_used || 0}/${status.daily?.posts_limit || 10}`,
      `**运行时长**: ${Math.floor((status.uptime_seconds || 0) / 60)} 分钟`,
    ].join("\n");
  } catch (e: any) {
    return `❌ API 不可用: ${e.message}`;
  }
}

async function handleSchedule(action: string, args: string): Promise<string> {
  const parts = args.trim().split("|").map(s => s.trim());

  switch (action) {
    case "add": {
      const cronExpr = parts[0] || "";
      const text = parts[1] || "";
      const images = parts[2] ? parts[2].split(/\s+/) : [];

      if (!cronExpr || !text) {
        return "用法: /schedule add <cron> | <文字> | <图片>\n例如: /schedule add 0 9 * * * | 早安";
      }

      try {
        const result = await apiCall<any>("/api/schedule", {
          method: "POST",
          body: JSON.stringify({ text, images, cron: cronExpr }),
        });
        return `✅ 定时任务已创建\nID: ${result.id}\nCron: ${cronExpr}\n下次执行: ${result.next_run || "计算中..."}`;
      } catch (e: any) {
        return `❌ 创建失败: ${e.message}`;
      }
    }

    case "list": {
      try {
        const schedules = await apiCall<any[]>("/api/schedule");
        if (!schedules.length) return "📋 暂无定时任务";

        return [
          "**定时任务列表**:",
          ...schedules.map((s: any) =>
            `  ${s.enabled ? "🟢" : "🔴"} \`${s.id}\` ${s.cron}\n     ${s.text?.substring(0, 40)}`
          ),
        ].join("\n");
      } catch (e: any) {
        return `❌ 查询失败: ${e.message}`;
      }
    }

    case "remove": {
      const id = parts[0];
      if (!id) return "用法: /schedule remove <id>";
      try {
        await apiCall(`/api/schedule/${id}`, { method: "DELETE" });
        return `✅ 定时任务 ${id} 已取消`;
      } catch (e: any) {
        return `❌ 取消失败: ${e.message}`;
      }
    }

    default:
      return "用法: /schedule <add|list|remove> ...";
  }
}

async function handleHistory(): Promise<string> {
  try {
    const history = await apiCall<any[]>("/api/history?limit=10");
    if (!history.length) return "📋 暂无发布历史";

    return [
      "**最近发布**:",
      ...history.map((h: any) =>
        `  ${h.success ? "✅" : "❌"} ${h.timestamp?.substring(11, 19)} ${h.text?.substring(0, 30)}`
      ),
    ].join("\n");
  } catch (e: any) {
    return `❌ 查询失败: ${e.message}`;
  }
}

// ═══════════════════════════════════════════════════════════════
// Skill 定义
// ═══════════════════════════════════════════════════════════════

const wechatMomentsSkill: Skill = {
  metadata: {
    name: "wechat-moments",
    version: "0.1.0",
    description: "远程控制微信朋友圈发布 — 支持文字+图片发布、定时任务、状态监控",
    author: "WeChat Moments Automation",
    keywords: ["wechat", "moments", "朋友圈", "publish", "schedule"],
  },

  commands: {
    publish: {
      description: "发布朋友圈",
      usage: "/publish <文字内容> | <图片路径1 图片路径2>",
      handler: async (ctx: SkillContext): Promise<SkillResult> => {
        const result = await handlePublish(ctx.args || "");
        return { text: result };
      },
    },

    status: {
      description: "查看系统状态",
      usage: "/status",
      handler: async (ctx: SkillContext): Promise<SkillResult> => {
        const result = await handleStatus();
        return { text: result };
      },
    },

    schedule: {
      description: "管理定时发布任务",
      usage: "/schedule <add|list|remove> ...",
      handler: async (ctx: SkillContext): Promise<SkillResult> => {
        const args = ctx.args || "";
        const [action, ...rest] = args.split(/\s+/);
        const result = await handleSchedule(action, rest.join(" "));
        return { text: result };
      },
    },

    history: {
      description: "查看发布历史",
      usage: "/history",
      handler: async (ctx: SkillContext): Promise<SkillResult> => {
        const result = await handleHistory();
        return { text: result };
      },
    },
  },

  // 自然语言意图识别（OpenClaw LLM 调用此 Skill 的方式）
  intents: {
    "发布朋友圈": {
      patterns: [
        "发朋友圈",
        "发布朋友圈",
        "发一条朋友圈",
        "帮我发朋友圈",
        "post to moments",
      ],
      handler: async (ctx: SkillContext, extracted: Record<string, string>) => {
        const text = extracted.text || ctx.args || "";
        const result = await handlePublish(text);
        return { text: result };
      },
    },
    "查看状态": {
      patterns: [
        "朋友圈状态",
        "发布状态",
        "系统状态",
        "wechat status",
      ],
      handler: async () => {
        const result = await handleStatus();
        return { text: result };
      },
    },
  },

  // 初始化
  onInit: async () => {
    console.log(`[WeChat Moments] API: ${API_BASE_URL}`);
    try {
      const health = await apiCall<any>("/health");
      console.log(`[WeChat Moments] API 连接正常`);
    } catch (e) {
      console.warn(`[WeChat Moments] API 不可用: ${API_BASE_URL}`);
    }
  },
};

export default wechatMomentsSkill;
