#!/usr/bin/env python3
"""
多平台内容自动整理与周报生成 Agent
支持: Git/GitHub/GitLab 提交记录、本地文件变更、自定义条目
输出: Markdown / HTML / 纯文本
"""

import argparse
import json
import subprocess
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from abc import ABC, abstractmethod


# ============================================================
# 数据模型
# ============================================================

@dataclass
class WorkItem:
    """单条工作记录"""
    source: str           # 来源: git/github/gitlab/manual
    project: str          # 项目名
    title: str            # 标题/提交信息
    description: str = "" # 详细描述
    category: str = ""    # 分类: feat/fix/chore/docs/other
    url: str = ""         # 链接
    timestamp: str = ""   # 时间
    author: str = ""      # 作者
    tags: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class WeekReport:
    """周报"""
    title: str
    author: str
    start_date: str
    end_date: str
    summary: str = ""
    sections: dict = field(default_factory=dict)  # category -> [WorkItem]
    highlights: list = field(default_factory=list)
    next_week_plans: list = field(default_factory=list)


# ============================================================
# 数据采集器基类
# ============================================================

class Collector(ABC):
    """数据采集器基类"""

    @abstractmethod
    def collect(self, since: datetime, until: datetime) -> list[WorkItem]:
        pass


# ============================================================
# Git 提交记录采集
# ============================================================

class GitCollector(Collector):
    """从本地 Git 仓库采集提交记录"""

    CATEGORY_MAP = {
        "feat": "新功能",
        "fix": "Bug修复",
        "chore": "杂项",
        "docs": "文档",
        "refactor": "重构",
        "test": "测试",
        "ci": "CI/CD",
        "style": "样式",
        "perf": "性能",
    }

    def __init__(self, repo_path: str, project_name: str = "", author: str = ""):
        self.repo_path = Path(repo_path)
        self.project_name = project_name or self.repo_path.name
        self.author = author

    def _parse_category(self, message: str) -> str:
        match = re.match(r"^(\w+)(?:\(.*?\))?[:：]", message)
        if match:
            prefix = match.group(1).lower()
            return self.CATEGORY_MAP.get(prefix, prefix)
        return "其他"

    def collect(self, since: datetime, until: datetime) -> list[WorkItem]:
        items = []
        since_str = since.strftime("%Y-%m-%d")
        until_str = until.strftime("%Y-%m-%d")

        cmd = [
            "git", "-C", str(self.repo_path), "log",
            f"--since={since_str}", f"--until={until_str}",
            "--pretty=format:%H|%s|%an|%ai",
            "--no-merges",
        ]
        if self.author:
            cmd.extend(["--author", self.author])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[GitCollector] 采集失败 {self.repo_path}: {e}")
            return []

        output = result.stdout or ""
        for line in output.strip().splitlines():
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            commit_hash, message, author, date = parts
            items.append(WorkItem(
                source="git",
                project=self.project_name,
                title=message.strip(),
                category=self._parse_category(message),
                url=f"{self.repo_path}",
                timestamp=date.strip(),
                author=author.strip(),
            ))
        return items


# ============================================================
# GitHub API 采集 (Pull Requests / Issues)
# ============================================================

class GitHubCollector(Collector):
    """通过 GitHub CLI (gh) 采集 PR 和 Issue"""

    def __init__(self, repo: str, item_types: list[str] = None):
        self.repo = repo  # owner/repo
        self.item_types = item_types or ["pr", "issue"]

    def _run_gh(self, args: list[str]) -> list[dict]:
        cmd = ["gh"] + args + ["--json", "title,createdAt,url,author,state,labels"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace")
            return json.loads(result.stdout) if result.stdout.strip() else []
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"[GitHubCollector] gh 命令失败: {e}")
            return []

    def collect(self, since: datetime, until: datetime) -> list[WorkItem]:
        items = []

        if "pr" in self.item_types:
            prs = self._run_gh(["pr", "list", "-R", self.repo, "--state", "all", "-L", "100"])
            for pr in prs:
                created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
                if since <= created.replace(tzinfo=None) <= until:
                    items.append(WorkItem(
                        source="github",
                        project=self.repo,
                        title=f"[PR] {pr['title']}",
                        category="feat",
                        url=pr["url"],
                        timestamp=pr["createdAt"],
                        author=pr.get("author", {}).get("login", ""),
                        tags=[pr.get("state", "")],
                    ))

        if "issue" in self.item_types:
            issues = self._run_gh(["issue", "list", "-R", self.repo, "--state", "all", "-L", "100"])
            for issue in issues:
                created = datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))
                if since <= created.replace(tzinfo=None) <= until:
                    items.append(WorkItem(
                        source="github",
                        project=self.repo,
                        title=f"[Issue] {issue['title']}",
                        category="fix",
                        url=issue["url"],
                        timestamp=issue["createdAt"],
                        author=issue.get("author", {}).get("login", ""),
                        tags=[issue.get("state", "")],
                    ))
        return items


# ============================================================
# 自定义条目采集 (JSON / 手动输入)
# ============================================================

class ManualCollector(Collector):
    """从 JSON 文件或直接传入的条目列表采集"""

    def __init__(self, entries: list[dict] = None, json_path: str = ""):
        self.entries = entries or []
        self.json_path = json_path

    def collect(self, since: datetime, until: datetime) -> list[WorkItem]:
        items = list(self.entries)
        if self.json_path and Path(self.json_path).exists():
            with open(self.json_path, "r", encoding="utf-8") as f:
                items.extend(json.load(f))

        result = []
        for entry in items:
            result.append(WorkItem(
                source="manual",
                project=entry.get("project", "其他"),
                title=entry.get("title", ""),
                description=entry.get("description", ""),
                category=entry.get("category", "其他"),
                url=entry.get("url", ""),
                timestamp=entry.get("timestamp", ""),
                author=entry.get("author", ""),
                tags=entry.get("tags", []),
            ))
        return result


# ============================================================
# 内容整理器
# ============================================================

class Organizer:
    """将原始 WorkItem 整理成结构化周报"""

    CATEGORY_ORDER = ["新功能", "Bug修复", "重构", "文档", "测试", "CI/CD", "性能", "样式", "杂项", "其他"]

    def organize(self, items: list[WorkItem], title: str = "", author: str = "",
                 start_date: str = "", end_date: str = "") -> WeekReport:
        # 按项目分组 -> 按分类排序
        sections: dict[str, list[WorkItem]] = {}
        for item in items:
            key = item.category or "其他"
            sections.setdefault(key, []).append(item)

        # 排序
        sorted_sections = {}
        for cat in self.CATEGORY_ORDER:
            if cat in sections:
                sorted_sections[cat] = sections[cat]
        for cat in sections:
            if cat not in sorted_sections:
                sorted_sections[cat] = sections[cat]

        # 自动生成摘要
        total = len(items)
        projects = len(set(i.project for i in items))
        categories = len(sorted_sections)
        summary = f"本周共处理 {total} 项工作，涉及 {projects} 个项目，覆盖 {categories} 个类别。"

        return WeekReport(
            title=title or f"周报 ({start_date} ~ {end_date})",
            author=author,
            start_date=start_date,
            end_date=end_date,
            summary=summary,
            sections=sorted_sections,
        )


# ============================================================
# 渲染器
# ============================================================

class Renderer(ABC):
    @abstractmethod
    def render(self, report: WeekReport) -> str:
        pass


class MarkdownRenderer(Renderer):
    """输出 Markdown 格式"""

    def render(self, report: WeekReport) -> str:
        lines = [
            f"# {report.title}",
            "",
            f"- **作者**: {report.author}" if report.author else "",
            f"- **周期**: {report.start_date} ~ {report.end_date}",
            "",
            "## 摘要",
            "",
            report.summary,
            "",
        ]

        for category, items in report.sections.items():
            lines.append(f"## {category} ({len(items)})")
            lines.append("")
            for item in items:
                prefix = f"[{item.project}]" if item.project else ""
                url_part = f" [链接]({item.url})" if item.url else ""
                lines.append(f"- {prefix} {item.title}{url_part}")
            lines.append("")

        if report.highlights:
            lines.append("## 本周亮点")
            lines.append("")
            for h in report.highlights:
                lines.append(f"- {h}")
            lines.append("")

        if report.next_week_plans:
            lines.append("## 下周计划")
            lines.append("")
            for p in report.next_week_plans:
                lines.append(f"- {p}")
            lines.append("")

        lines.append("---")
        lines.append(f"_由 WeeklyReportAgent 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}_")
        return "\n".join(lines)


class HTMLRenderer(Renderer):
    """输出 HTML 格式"""

    def render(self, report: WeekReport) -> str:
        sections_html = ""
        for category, items in report.sections.items():
            rows = ""
            for item in items:
                url_td = f'<a href="{item.url}">链接</a>' if item.url else "-"
                rows += f"<tr><td>{item.project}</td><td>{item.title}</td><td>{item.category}</td><td>{url_td}</td></tr>\n"
            sections_html += f"""
            <h2>{category} ({len(items)})</h2>
            <table>
                <tr><th>项目</th><th>内容</th><th>分类</th><th>链接</th></tr>
                {rows}
            </table>"""

        highlights_html = ""
        if report.highlights:
            items = "".join(f"<li>{h}</li>" for h in report.highlights)
            highlights_html = f"<h2>本周亮点</h2><ul>{items}</ul>"

        plans_html = ""
        if report.next_week_plans:
            items = "".join(f"<li>{p}</li>" for p in report.next_week_plans)
            plans_html = f"<h2>下周计划</h2><ul>{items}</ul>"

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{report.title}</title>
<style>
    body {{ font-family: -apple-system, "Microsoft YaHei", sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #333; }}
    h1 {{ border-bottom: 2px solid #0366d6; padding-bottom: 8px; }}
    h2 {{ color: #0366d6; margin-top: 24px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
    th {{ background: #f6f8fa; }}
    tr:nth-child(even) {{ background: #fafbfc; }}
    .meta {{ color: #666; font-size: 14px; }}
    a {{ color: #0366d6; }}
</style>
</head>
<body>
<h1>{report.title}</h1>
<p class="meta">作者: {report.author} | 周期: {report.start_date} ~ {report.end_date}</p>
<h2>摘要</h2><p>{report.summary}</p>
{sections_html}
{highlights_html}
{plans_html}
<hr>
<p class="meta"><em>由 WeeklyReportAgent 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}</em></p>
</body>
</html>"""


class PlainTextRenderer(Renderer):
    """输出纯文本格式"""

    def render(self, report: WeekReport) -> str:
        lines = [
            report.title,
            "=" * len(report.title.encode("gbk", errors="replace")),
            f"作者: {report.author}",
            f"周期: {report.start_date} ~ {report.end_date}",
            "",
            f"摘要: {report.summary}",
            "",
        ]
        for category, items in report.sections.items():
            lines.append(f"【{category}】({len(items)}项)")
            lines.append("-" * 40)
            for item in items:
                prefix = f"[{item.project}] " if item.project else ""
                lines.append(f"  * {prefix}{item.title}")
            lines.append("")
        return "\n".join(lines)


# ============================================================
# 主 Agent
# ============================================================

class WeeklyReportAgent:
    """周报生成 Agent 主类"""

    RENDERERS = {
        "markdown": MarkdownRenderer,
        "html": HTMLRenderer,
        "text": PlainTextRenderer,
    }

    def __init__(self):
        self.collectors: list[Collector] = []
        self.organizer = Organizer()

    def add_git_repo(self, repo_path: str, project_name: str = "", author: str = ""):
        self.collectors.append(GitCollector(repo_path, project_name, author))
        return self

    def add_github(self, repo: str, item_types: list[str] = None):
        self.collectors.append(GitHubCollector(repo, item_types))
        return self

    def add_manual(self, entries: list[dict] = None, json_path: str = ""):
        self.collectors.append(ManualCollector(entries, json_path))
        return self

    def generate(self, start_date: str, end_date: str,
                 title: str = "", author: str = "",
                 output_format: str = "markdown",
                 output_path: str = "") -> str:
        since = datetime.strptime(start_date, "%Y-%m-%d")
        until = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

        # 采集
        all_items: list[WorkItem] = []
        for collector in self.collectors:
            items = collector.collect(since, until)
            print(f"  [{collector.__class__.__name__}] 采集到 {len(items)} 条记录")
            all_items.extend(items)

        if not all_items:
            print("警告: 未采集到任何工作记录")

        # 整理
        report = self.organizer.organize(all_items, title, author, start_date, end_date)

        # 渲染
        renderer_cls = self.RENDERERS.get(output_format, MarkdownRenderer)
        output = renderer_cls().render(report)

        # 输出
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"周报已保存至: {output_path}")

        return output


# ============================================================
# 配置文件加载
# ============================================================

def load_config(config_path: str) -> dict:
    """从 JSON 配置文件加载 Agent 配置"""
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_agent_from_config(config: dict) -> WeeklyReportAgent:
    """根据配置构建 Agent"""
    agent = WeeklyReportAgent()

    for repo in config.get("git_repos", []):
        agent.add_git_repo(
            repo_path=repo["path"],
            project_name=repo.get("name", ""),
            author=repo.get("author", ""),
        )

    for gh in config.get("github_repos", []):
        agent.add_github(
            repo=gh["repo"],
            item_types=gh.get("types", ["pr", "issue"]),
        )

    manual = config.get("manual_entries", {})
    if manual:
        agent.add_manual(
            entries=manual.get("entries", []),
            json_path=manual.get("json_path", ""),
        )

    return agent


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="多平台内容自动整理与周报生成 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 从当前 Git 仓库生成本周周报
  python weekly_report_agent.py --git . --start 2026-05-12 --end 2026-05-18

  # 使用配置文件
  python weekly_report_agent.py --config config.json

  # 多仓库 + GitHub + 自定义条目
  python weekly_report_agent.py --git ./project-a --git ./project-b \\
    --github owner/repo --manual entries.json \\
    --start 2026-05-12 --end 2026-05-18 --format html --output report.html
        """,
    )

    parser.add_argument("--config", "-c", help="JSON 配置文件路径")
    parser.add_argument("--git", action="append", default=[], help="Git 仓库路径 (可多次指定)")
    parser.add_argument("--github", action="append", default=[], help="GitHub 仓库 owner/repo (可多次指定)")
    parser.add_argument("--manual", help="自定义条目 JSON 文件路径")
    parser.add_argument("--start", "-s", help="开始日期 YYYY-MM-DD (默认本周一)")
    parser.add_argument("--end", "-e", help="结束日期 YYYY-MM-DD (默认本周日)")
    parser.add_argument("--title", "-t", help="周报标题")
    parser.add_argument("--author", "-a", help="作者名")
    parser.add_argument("--format", "-f", choices=["markdown", "html", "text"], default="markdown", help="输出格式")
    parser.add_argument("--output", "-o", help="输出文件路径 (不指定则打印到终端)")
    parser.add_argument("--generate-config", action="store_true", help="生成示例配置文件 config.example.json")

    args = parser.parse_args()

    # 生成示例配置
    if args.generate_config:
        example = {
            "git_repos": [
                {"path": "./my-project", "name": "我的项目", "author": ""},
                {"path": "../another-project", "name": "另一个项目"},
            ],
            "github_repos": [
                {"repo": "owner/repo", "types": ["pr", "issue"]},
            ],
            "manual_entries": {
                "json_path": "entries.json",
                "entries": [
                    {
                        "project": "产品",
                        "title": "完成需求评审",
                        "category": "其他",
                        "description": "评审了3个需求",
                    }
                ],
            },
            "report": {
                "title": "工作周报",
                "author": "张三",
                "start_date": "2026-05-12",
                "end_date": "2026-05-18",
                "format": "markdown",
                "output": "weekly-report.md",
            },
        }
        with open("config.example.json", "w", encoding="utf-8") as f:
            json.dump(example, f, ensure_ascii=False, indent=2)
        print("已生成 config.example.json，请根据实际情况修改后使用:")
        print("  python weekly_report_agent.py --config config.example.json")
        return

    # 从配置文件构建
    if args.config:
        config = load_config(args.config)
        agent = build_agent_from_config(config)
        report_cfg = config.get("report", {})
        start_date = args.start or report_cfg.get("start_date", "")
        end_date = args.end or report_cfg.get("end_date", "")
        title = args.title or report_cfg.get("title", "")
        author = args.author or report_cfg.get("author", "")
        output_format = args.format or report_cfg.get("format", "markdown")
        output_path = args.output or report_cfg.get("output", "")
    else:
        if not args.git and not args.github and not args.manual:
            parser.error("请至少指定一个数据源 (--git / --github / --manual)，或使用 --config 配置文件")

        agent = WeeklyReportAgent()
        for repo in args.git:
            agent.add_git_repo(repo)
        for gh in args.github:
            agent.add_github(gh)
        if args.manual:
            agent.add_manual(json_path=args.manual)

        start_date = args.start
        end_date = args.end
        title = args.title
        author = args.author
        output_format = args.format
        output_path = args.output

    # 默认本周
    today = datetime.now()
    if not start_date:
        monday = today - timedelta(days=today.weekday())
        start_date = monday.strftime("%Y-%m-%d")
    if not end_date:
        end_date = today.strftime("%Y-%m-%d")

    print(f"正在生成周报: {start_date} ~ {end_date}")
    print("-" * 40)

    result = agent.generate(
        start_date=start_date,
        end_date=end_date,
        title=title,
        author=author,
        output_format=output_format,
        output_path=output_path,
    )

    if not output_path:
        print("\n" + result)


if __name__ == "__main__":
    main()
