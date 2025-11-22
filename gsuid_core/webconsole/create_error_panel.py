import json
from pathlib import Path
from typing import Dict, List, Optional

from gsuid_core.data_store import error_mark_path

Report_Path = error_mark_path


def load_error_logs(report_path: Path) -> List[Dict]:
    """
    读取路径下所有json文件并按时间倒序排列
    """
    logs = []
    if not report_path.exists():
        return logs

    # 遍历所有 json 文件
    for file_path in report_path.glob("*.json"):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # 为了列表展示更清晰，我们可以提取简短的标题
                # 如果没有 event 字段，就用文件名
                data['id'] = file_path.stem
                logs.append(data)
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

    # 按时间戳倒序排列 (最新的在最上面)
    logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return logs


def generate_error_schema(logs: Optional[List[Dict]] = None) -> Dict:
    """
    生成 Amis 页面配置 JSON
    """

    # 定义主色调
    primary_color = "#CE5050"

    if logs is None:
        logs = load_error_logs(Report_Path)

    # --- 重点修复：将 CSS 定义为纯字符串，而不是字典 ---
    css_styles = f"""
    /* 页面标题红线 */
    .cxd-Page-header {{
        border-bottom-color: {primary_color};
    }}
    .cxd-Page-title {{
        color: {primary_color};
    }}

    /* 折叠面板样式自定义 */
    .cxd-Collapse {{
        border-left: 4px solid {primary_color};
        margin-bottom: 10px;
        background-color: #fff;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
    }}

    /* 标题栏背景微红 */
    .cxd-Collapse-header {{
        background: #FFF5F5;
    }}

    /* 错误堆栈代码块样式修正 */
    .error-stack-code .cxd-Code {{
        border: 1px solid #ffccc7;
        font-family: Consolas, Monaco, monospace;
        font-size: 12px;
    }}
    """

    IGNORE_KEYWORDS = [
        "CancelledError",  # 异步任务取消
        "KeyboardInterrupt",  # Ctrl+C
        "anyio.WouldBlock",  # 异步流阻塞
        "SystemExit",  # 系统退出
        "GeneratorExit",  # 生成器退出
        "uvicorn.error",  # 有时候 uvicorn 自身的关闭日志
    ]

    # 构建折叠面板列表
    collapse_items = []
    for log in logs:
        # 获取标题，如果过长截断
        event_title = log.get('event', '未知错误')

        full_error_text = str(log.get('event', '')) + str(
            log.get('exception', '')
        )

        if any(keyword in full_error_text for keyword in IGNORE_KEYWORDS):
            continue

        collapse_items.append(
            {
                "type": "collapse",
                "headingClassName": "bg-red-50",
                "header": {
                    "type": "tpl",
                    "tpl": f"""
                <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                    <div style="display: flex; align-items: center; overflow: hidden;">
                        <span style="font-weight: bold; color: {primary_color}; margin-right: 8px;">[错误]</span>
                        <span style="color: #333; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 60vw;">
                            {event_title}
                        </span>
                    </div>
                    <span style="font-size: 12px; color: #999; margin-left: 10px; flex-shrink: 0;">
                        {log.get('timestamp', '')}
                    </span>
                </div>
                """,  # noqa: E501
                },
                "body": [
                    {
                        "type": "flex",
                        "justify": "flex-start",
                        "items": [
                            {
                                "type": "tpl",
                                "tpl": f"<span class='label' style='background:{primary_color}; color:white'>Level: {log.get('level', 'ERROR')}</span>",  # noqa: E501
                            },
                            {
                                "type": "tpl",
                                "tpl": f"<span class='label label-default' style='margin-left:10px'>File: {log.get('filename', '')}</span>",  # noqa: E501
                            },
                        ],
                    },
                    {"type": "divider"},
                    {
                        "type": "code",
                        "language": "python",
                        "className": "error-stack-code",
                        "theme": "vs-dark",
                        "value": log.get('exception', '无详细堆栈信息'),
                    },
                ],
            }
        )

    # 如果没有日志，显示空状态
    if not collapse_items:
        content_body = {
            "type": "alert",
            "level": "info",
            "body": "当前没有检测到错误报告文件。",
        }
    else:
        content_body = {
            "type": "collapse-group",
            "accordion": True,
            "expandIcon": {
                "type": "icon",
                "icon": "fa fa-bug",
                "className": "text-danger",
            },
            "body": collapse_items,
        }

    # 返回 Schema 字典
    return {
        "type": "page",
        "title": "系统异常监控报告",
        "css": css_styles,
        "body": [content_body],
    }
