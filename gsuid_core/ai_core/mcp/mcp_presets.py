"""
MCP 预设配置

提供常见、知名 MCP 服务提供方的预设配置，方便用户快速添加。

预设配置支持两种传输方式:
- stdio: 通过 command + args + env_template 启动本地进程
- sse: 通过 url + headers_template 连接远程 SSE 服务器
"""

from typing import Any

MCP_PRESETS: list[dict[str, Any]] = [
    # =========================================================================
    # 搜索与信息检索
    # =========================================================================
    {
        "name": "Tavily",
        "description": "Tavily AI 搜索服务，专为 AI Agent 优化的实时网络搜索",
        "command": "uvx",
        "args": ["tavily-mcp"],
        "env_template": {
            "TAVILY_API_KEY": "",
        },
        "default_tools": [
            {"name": "tavily-search", "description": "Search the web with AI-optimized results"},
        ],
    },
    {
        "name": "Brave Search",
        "description": "Brave 搜索引擎 MCP 服务，提供隐私友好的网络搜索",
        "command": "uvx",
        "args": ["brave-search-mcp"],
        "env_template": {
            "BRAVE_API_KEY": "",
        },
        "default_tools": [
            {"name": "brave_web_search", "description": "Search the web using Brave Search"},
            {"name": "brave_local_search", "description": "Search for local businesses and places"},
        ],
    },
    {
        "name": "Exa",
        "description": "Exa AI 搜索引擎，提供语义搜索和内容检索",
        "command": "uvx",
        "args": ["exa-mcp"],
        "env_template": {
            "EXA_API_KEY": "",
        },
        "default_tools": [
            {"name": "exa_search", "description": "Semantic search the web"},
            {"name": "exa_get_contents", "description": "Get contents of URLs"},
        ],
    },
    {
        "name": "MiniMax",
        "description": "MiniMax MCP 服务，提供 Web Search 和 Image Understand 功能",
        "command": "uvx",
        "args": ["minimax-coding-plan-mcp"],
        "env_template": {
            "MINIMAX_API_KEY": "",
            "MINIMAX_API_HOST": "https://api.minimaxi.com",
            "MINIMAX_API_RESOURCE_MODE": "url",
        },
        "default_tools": [
            {"name": "web_search", "description": "Web search tool"},
            {"name": "understand_image", "description": "Image understanding tool"},
        ],
    },
    # =========================================================================
    # 网页抓取与爬虫
    # =========================================================================
    {
        "name": "Firecrawl",
        "description": "网页抓取和爬虫服务，支持将网页转换为 Markdown 格式",
        "command": "uvx",
        "args": ["firecrawl-mcp"],
        "env_template": {
            "FIRECRAWL_API_KEY": "",
        },
        "default_tools": [
            {"name": "firecrawl_scrape", "description": "Scrape a single webpage to markdown"},
            {"name": "firecrawl_crawl", "description": "Crawl multiple pages from a website"},
            {"name": "firecrawl_map", "description": "Map a website to discover all URLs"},
        ],
    },
    {
        "name": "Jina Reader",
        "description": "Jina AI Reader，将网页内容转换为 LLM 友好的格式",
        "command": "uvx",
        "args": ["jina-mcp"],
        "env_template": {
            "JINA_API_KEY": "",
        },
        "default_tools": [
            {"name": "read_url", "description": "Read and convert a URL to markdown"},
            {"name": "search_web", "description": "Search the web using Jina"},
        ],
    },
    # =========================================================================
    # 代码与开发工具
    # =========================================================================
    {
        "name": "GitHub",
        "description": "GitHub API 集成，支持仓库管理、代码搜索、Issue/PR 操作",
        "command": "uvx",
        "args": ["github-mcp"],
        "env_template": {
            "GITHUB_TOKEN": "",
        },
        "default_tools": [
            {"name": "search_code", "description": "Search code in repositories"},
            {"name": "get_file", "description": "Get file content from a repository"},
            {"name": "create_issue", "description": "Create a new issue"},
            {"name": "list_repos", "description": "List user repositories"},
        ],
    },
    {
        "name": "GitLab",
        "description": "GitLab API 集成，支持项目管理、代码操作、CI/CD",
        "command": "uvx",
        "args": ["gitlab-mcp"],
        "env_template": {
            "GITLAB_TOKEN": "",
            "GITLAB_URL": "https://gitlab.com",
        },
        "default_tools": [
            {"name": "search_projects", "description": "Search GitLab projects"},
            {"name": "get_file", "description": "Get file content from a project"},
            {"name": "create_merge_request", "description": "Create a merge request"},
        ],
    },
    {
        "name": "Sentry",
        "description": "Sentry 错误监控集成，用于查询和管理错误报告",
        "command": "uvx",
        "args": ["sentry-mcp"],
        "env_template": {
            "SENTRY_AUTH_TOKEN": "",
            "SENTRY_ORG": "",
        },
        "default_tools": [
            {"name": "search_issues", "description": "Search Sentry issues"},
            {"name": "get_issue_details", "description": "Get detailed issue information"},
        ],
    },
    # =========================================================================
    # 文件系统与存储
    # =========================================================================
    {
        "name": "Filesystem",
        "description": "本地文件系统操作，提供安全的文件读写和目录管理",
        "command": "uvx",
        "args": ["filesystem-mcp"],
        "env_template": {
            "ALLOWED_DIRECTORY": "/",
        },
        "default_tools": [
            {"name": "read_file", "description": "Read a file"},
            {"name": "write_file", "description": "Write to a file"},
            {"name": "list_directory", "description": "List directory contents"},
            {"name": "create_directory", "description": "Create a directory"},
        ],
    },
    {
        "name": "Google Drive",
        "description": "Google Drive 文件管理，支持文件搜索、读取和组织",
        "command": "uvx",
        "args": ["google-drive-mcp"],
        "env_template": {
            "GOOGLE_DRIVE_CREDENTIALS": "",
        },
        "default_tools": [
            {"name": "search_files", "description": "Search files in Google Drive"},
            {"name": "read_file", "description": "Read file content"},
            {"name": "list_files", "description": "List files in a folder"},
        ],
    },
    # =========================================================================
    # 数据库
    # =========================================================================
    {
        "name": "PostgreSQL",
        "description": "PostgreSQL 数据库集成，支持查询和数据分析",
        "command": "uvx",
        "args": ["postgres-mcp"],
        "env_template": {
            "DATABASE_URL": "postgresql://user:password@localhost:5432/dbname",
        },
        "default_tools": [
            {"name": "query", "description": "Execute a SQL query"},
            {"name": "list_tables", "description": "List all tables"},
            {"name": "describe_table", "description": "Describe table schema"},
        ],
    },
    {
        "name": "SQLite",
        "description": "SQLite 数据库集成，轻量级本地数据库操作",
        "command": "uvx",
        "args": ["sqlite-mcp"],
        "env_template": {
            "SQLITE_DB_PATH": "./data.db",
        },
        "default_tools": [
            {"name": "query", "description": "Execute a SQL query"},
            {"name": "list_tables", "description": "List all tables"},
            {"name": "describe_table", "description": "Describe table schema"},
        ],
    },
    {
        "name": "Supabase",
        "description": "Supabase 后端即服务集成，支持数据库、认证和存储",
        "command": "uvx",
        "args": ["supabase-mcp"],
        "env_template": {
            "SUPABASE_URL": "",
            "SUPABASE_KEY": "",
        },
        "default_tools": [
            {"name": "query_table", "description": "Query a Supabase table"},
            {"name": "insert_row", "description": "Insert a row into a table"},
        ],
    },
    # =========================================================================
    # 通信与协作
    # =========================================================================
    {
        "name": "Slack",
        "description": "Slack 工作区集成，支持消息发送、频道管理和搜索",
        "command": "uvx",
        "args": ["slack-mcp"],
        "env_template": {
            "SLACK_BOT_TOKEN": "",
            "SLACK_TEAM_ID": "",
        },
        "default_tools": [
            {"name": "send_message", "description": "Send a message to a channel"},
            {"name": "list_channels", "description": "List available channels"},
            {"name": "search_messages", "description": "Search messages"},
        ],
    },
    {
        "name": "Discord",
        "description": "Discord 服务器集成，支持消息管理和服务器操作",
        "command": "uvx",
        "args": ["discord-mcp"],
        "env_template": {
            "DISCORD_TOKEN": "",
        },
        "default_tools": [
            {"name": "send_message", "description": "Send a message to a channel"},
            {"name": "list_channels", "description": "List available channels"},
        ],
    },
    {
        "name": "Email (SMTP/IMAP)",
        "description": "邮件服务集成，支持发送和接收邮件",
        "command": "uvx",
        "args": ["email-mcp"],
        "env_template": {
            "EMAIL_HOST": "imap.gmail.com",
            "EMAIL_PORT": "993",
            "EMAIL_USER": "",
            "EMAIL_PASSWORD": "",
        },
        "default_tools": [
            {"name": "send_email", "description": "Send an email"},
            {"name": "read_emails", "description": "Read recent emails"},
            {"name": "search_emails", "description": "Search emails by criteria"},
        ],
    },
    # =========================================================================
    # 项目管理
    # =========================================================================
    {
        "name": "Linear",
        "description": "Linear 项目管理工具集成，支持 Issue 和项目跟踪",
        "command": "uvx",
        "args": ["linear-mcp"],
        "env_template": {
            "LINEAR_API_KEY": "",
        },
        "default_tools": [
            {"name": "list_issues", "description": "List issues"},
            {"name": "create_issue", "description": "Create a new issue"},
            {"name": "update_issue", "description": "Update an existing issue"},
        ],
    },
    {
        "name": "Jira",
        "description": "Atlassian Jira 项目管理集成，支持 Issue 和 Sprint 管理",
        "command": "uvx",
        "args": ["jira-mcp"],
        "env_template": {
            "JIRA_URL": "",
            "JIRA_USER": "",
            "JIRA_API_TOKEN": "",
        },
        "default_tools": [
            {"name": "search_issues", "description": "Search Jira issues using JQL"},
            {"name": "create_issue", "description": "Create a new issue"},
            {"name": "update_issue", "description": "Update an existing issue"},
        ],
    },
    {
        "name": "Notion",
        "description": "Notion 工作区集成，支持页面、数据库和内容管理",
        "command": "uvx",
        "args": ["notion-mcp"],
        "env_template": {
            "NOTION_API_KEY": "",
        },
        "default_tools": [
            {"name": "search_pages", "description": "Search Notion pages"},
            {"name": "get_page", "description": "Get page content"},
            {"name": "create_page", "description": "Create a new page"},
            {"name": "update_page", "description": "Update page content"},
        ],
    },
    {
        "name": "Asana",
        "description": "Asana 项目管理集成，支持任务和项目跟踪",
        "command": "uvx",
        "args": ["asana-mcp"],
        "env_template": {
            "ASANA_ACCESS_TOKEN": "",
        },
        "default_tools": [
            {"name": "list_tasks", "description": "List tasks"},
            {"name": "create_task", "description": "Create a new task"},
            {"name": "update_task", "description": "Update an existing task"},
        ],
    },
    # =========================================================================
    # 云服务
    # =========================================================================
    {
        "name": "AWS",
        "description": "Amazon Web Services 集成，支持 S3、Lambda、EC2 等服务",
        "command": "uvx",
        "args": ["aws-mcp"],
        "env_template": {
            "AWS_ACCESS_KEY_ID": "",
            "AWS_SECRET_ACCESS_KEY": "",
            "AWS_REGION": "us-east-1",
        },
        "default_tools": [
            {"name": "list_s3_buckets", "description": "List S3 buckets"},
            {"name": "list_ec2_instances", "description": "List EC2 instances"},
        ],
    },
    {
        "name": "Cloudflare",
        "description": "Cloudflare 服务集成，支持 DNS、Workers 和 CDN 管理",
        "command": "uvx",
        "args": ["cloudflare-mcp"],
        "env_template": {
            "CLOUDFLARE_API_TOKEN": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
        },
        "default_tools": [
            {"name": "list_zones", "description": "List DNS zones"},
            {"name": "list_workers", "description": "List Workers scripts"},
        ],
    },
    # =========================================================================
    # AI 与模型服务
    # =========================================================================
    {
        "name": "OpenAI",
        "description": "OpenAI API 集成，支持 GPT 模型调用和图像生成",
        "command": "uvx",
        "args": ["openai-mcp"],
        "env_template": {
            "OPENAI_API_KEY": "",
        },
        "default_tools": [
            {"name": "chat_completion", "description": "Create a chat completion"},
            {"name": "generate_image", "description": "Generate an image using DALL-E"},
        ],
    },
    {
        "name": "Anthropic",
        "description": "Anthropic Claude API 集成",
        "command": "uvx",
        "args": ["anthropic-mcp"],
        "env_template": {
            "ANTHROPIC_API_KEY": "",
        },
        "default_tools": [
            {"name": "chat_completion", "description": "Create a chat completion with Claude"},
        ],
    },
    {
        "name": "Replicate",
        "description": "Replicate 模型平台集成，支持运行各种开源 AI 模型",
        "command": "uvx",
        "args": ["replicate-mcp"],
        "env_template": {
            "REPLICATE_API_TOKEN": "",
        },
        "default_tools": [
            {"name": "run_model", "description": "Run a model on Replicate"},
            {"name": "list_models", "description": "List available models"},
        ],
    },
    # =========================================================================
    # 生产力工具
    # =========================================================================
    {
        "name": "Google Calendar",
        "description": "Google 日历集成，支持事件创建和日程管理",
        "command": "uvx",
        "args": ["google-calendar-mcp"],
        "env_template": {
            "GOOGLE_CALENDAR_CREDENTIALS": "",
        },
        "default_tools": [
            {"name": "list_events", "description": "List calendar events"},
            {"name": "create_event", "description": "Create a calendar event"},
            {"name": "update_event", "description": "Update a calendar event"},
        ],
    },
    {
        "name": "Todoist",
        "description": "Todoist 任务管理集成",
        "command": "uvx",
        "args": ["todoist-mcp"],
        "env_template": {
            "TODOIST_API_KEY": "",
        },
        "default_tools": [
            {"name": "list_tasks", "description": "List tasks"},
            {"name": "create_task", "description": "Create a new task"},
            {"name": "complete_task", "description": "Mark a task as complete"},
        ],
    },
    {
        "name": "Obsidian",
        "description": "Obsidian 笔记库集成，支持笔记搜索和管理",
        "command": "uvx",
        "args": ["obsidian-mcp"],
        "env_template": {
            "OBSIDIAN_VAULT_PATH": "",
        },
        "default_tools": [
            {"name": "search_notes", "description": "Search notes in vault"},
            {"name": "read_note", "description": "Read a note"},
            {"name": "create_note", "description": "Create a new note"},
        ],
    },
    # =========================================================================
    # 数据分析与可视化
    # =========================================================================
    {
        "name": "Pandas (Data Analysis)",
        "description": "基于 Pandas 的数据分析工具，支持 CSV/Excel 数据处理",
        "command": "uvx",
        "args": ["pandas-mcp"],
        "env_template": {},
        "default_tools": [
            {"name": "read_csv", "description": "Read a CSV file"},
            {"name": "analyze_data", "description": "Perform data analysis"},
            {"name": "create_chart", "description": "Create a data visualization"},
        ],
    },
    # =========================================================================
    # 多媒体
    # =========================================================================
    {
        "name": "ElevenLabs",
        "description": "ElevenLabs 语音合成服务，支持高质量 TTS",
        "command": "uvx",
        "args": ["elevenlabs-mcp"],
        "env_template": {
            "ELEVENLABS_API_KEY": "",
        },
        "default_tools": [
            {"name": "text_to_speech", "description": "Convert text to speech"},
            {"name": "list_voices", "description": "List available voices"},
        ],
    },
    {
        "name": "Fal.ai",
        "description": "Fal.ai AI 媒体生成平台，支持图像、视频和音频生成",
        "command": "uvx",
        "args": ["fal-mcp"],
        "env_template": {
            "FAL_KEY": "",
        },
        "default_tools": [
            {"name": "generate_image", "description": "Generate an image"},
            {"name": "generate_video", "description": "Generate a video"},
        ],
    },
    # =========================================================================
    # 浏览器自动化
    # =========================================================================
    {
        "name": "Playwright",
        "description": "Playwright 浏览器自动化，支持网页交互和截图",
        "command": "uvx",
        "args": ["playwright-mcp"],
        "env_template": {},
        "default_tools": [
            {"name": "navigate", "description": "Navigate to a URL"},
            {"name": "screenshot", "description": "Take a screenshot"},
            {"name": "click", "description": "Click an element"},
            {"name": "fill", "description": "Fill an input field"},
        ],
    },
    {
        "name": "Puppeteer",
        "description": "Puppeteer 浏览器自动化，基于 Chrome DevTools Protocol",
        "command": "uvx",
        "args": ["puppeteer-mcp"],
        "env_template": {},
        "default_tools": [
            {"name": "navigate", "description": "Navigate to a URL"},
            {"name": "screenshot", "description": "Take a screenshot"},
            {"name": "click", "description": "Click an element"},
        ],
    },
    # =========================================================================
    # 地图与位置
    # =========================================================================
    {
        "name": "Google Maps",
        "description": "Google Maps 集成，支持地点搜索、路线规划和地理编码",
        "command": "uvx",
        "args": ["google-maps-mcp"],
        "env_template": {
            "GOOGLE_MAPS_API_KEY": "",
        },
        "default_tools": [
            {"name": "search_places", "description": "Search for places"},
            {"name": "get_directions", "description": "Get directions between locations"},
            {"name": "geocode", "description": "Convert address to coordinates"},
        ],
    },
    # =========================================================================
    # 知识库与文档
    # =========================================================================
    {
        "name": "Confluence",
        "description": "Atlassian Confluence 知识库集成",
        "command": "uvx",
        "args": ["confluence-mcp"],
        "env_template": {
            "CONFLUENCE_URL": "",
            "CONFLUENCE_USER": "",
            "CONFLUENCE_API_TOKEN": "",
        },
        "default_tools": [
            {"name": "search_pages", "description": "Search Confluence pages"},
            {"name": "get_page", "description": "Get page content"},
            {"name": "create_page", "description": "Create a new page"},
        ],
    },
    {
        "name": "Readwise",
        "description": "Readwise 阅读笔记集成，支持高亮和笔记同步",
        "command": "uvx",
        "args": ["readwise-mcp"],
        "env_template": {
            "READWISE_API_KEY": "",
        },
        "default_tools": [
            {"name": "list_highlights", "description": "List reading highlights"},
            {"name": "search_highlights", "description": "Search highlights"},
        ],
    },
    # =========================================================================
    # SSE 远程服务
    # =========================================================================
    {
        "name": "知乎搜索",
        "description": "知乎站内搜索 MCP 服务，通过 SSE 方式提供知乎搜索能力",
        "transport": "sse",
        "url": "https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse",
        "headers_template": {
            "Authorization": "Bearer ",
        },
        "default_tools": [
            {"name": "zhihu_search", "description": "搜索知乎站内内容"},
        ],
    },
]
