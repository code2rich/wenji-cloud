"""MCP 接口（Streamable HTTP，路径 /mcp）：供其他服务查询录音、转写、纪要和脑图。

鉴权由 main.py 的中间件统一处理：请求头 Authorization: Bearer <WENJI_TOKEN>。
"""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import library
from .db import DB

READ_ONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)


def build(db: DB) -> MCPServer:
    server = MCPServer(
        name="wenji",
        title="闻记",
        instructions=("查询个人录音库：先用 list_recordings 或 search_transcripts 找到录音 id，"
                      "再用 get_transcript / get_summary / get_mindmap 读取内容。"
                      "录音时间是本地时间；status 不是 done 的录音可能还没有转写或纪要。"),
    )

    def require(recording_id: int) -> dict:
        rec = library.get(db, recording_id)
        if not rec:
            raise ToolError(f"录音 {recording_id} 不存在")
        return rec

    @server.tool(annotations=READ_ONLY)
    def list_recordings(query: str = "", since: str = "", limit: int = 20) -> list[dict]:
        """列出录音，按录音时间倒序。

        Args:
            query: 关键词，匹配标题、文件名、纪要和转写正文；留空列出全部
            since: 只返回该日期之后的录音，格式 YYYY-MM-DD
            limit: 最多返回条数（1-500）
        """
        rows = library.list_recordings(db, query, since, limit)
        keys = ("id", "title", "recorded_at", "duration_s", "status", "has_summary", "has_mindmap", "snippet")
        return [{k: bool(r[k]) if k.startswith("has_") else r[k] for k in keys} for r in rows]

    @server.tool(annotations=READ_ONLY)
    def search_transcripts(query: str, limit: int = 20) -> list[dict]:
        """在所有转写正文中搜索关键词，返回命中的句子及所在录音、时间点（毫秒）和说话人。

        Args:
            query: 关键词（按原文子串匹配）
            limit: 最多返回条数（1-200）
        """
        if not query.strip():
            raise ToolError("query 不能为空")
        return library.search_segments(db, query, limit)

    @server.tool(annotations=READ_ONLY)
    def get_recording(recording_id: int) -> dict:
        """获取一条录音的元信息：标题、录音时间、时长、处理状态、说话人命名、所用模型。"""
        rec = require(recording_id)
        keys = ("id", "name", "title", "recorded_at", "duration_s", "status", "error", "speaker_names",
                "asr_provider", "summary_model", "created_at")
        return {k: rec[k] for k in keys} | {"has_summary": bool(rec["summary"]), "has_mindmap": bool(rec["mindmap"])}

    @server.tool(annotations=READ_ONLY)
    def get_transcript(recording_id: int) -> str:
        """获取完整转写稿，每行形如「[HH:MM:SS] 说话人：内容」，已应用说话人命名。"""
        rec = require(recording_id)
        text = library.transcript_text(db, rec)
        if not text:
            raise ToolError(f"录音 {recording_id} 还没有转写（状态：{rec['status']}）")
        return text

    @server.tool(annotations=READ_ONLY)
    def get_summary(recording_id: int) -> str:
        """获取纪要（Markdown），包含总结、章节概要、金句和待办事项。"""
        rec = require(recording_id)
        if not rec["summary"]:
            raise ToolError(f"录音 {recording_id} 还没有纪要（状态：{rec['status']}）")
        return rec["summary"]

    @server.tool(annotations=READ_ONLY)
    def get_mindmap(recording_id: int) -> str:
        """获取脑图大纲（Markdown：# 根节点、## 分支、- 子节点），可直接交给 markmap 等工具渲染。"""
        rec = require(recording_id)
        if not rec["mindmap"]:
            raise ToolError(f"录音 {recording_id} 还没有脑图（状态：{rec['status']}）")
        return rec["mindmap"]

    return server
