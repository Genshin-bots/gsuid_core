from gsuid_core.data_store import AI_CORE_PATH, get_res_path

local_embedding_images = get_res_path(AI_CORE_PATH / "local_embedding_images")
# 角色存储路径 - 所有角色资料文件夹存放的目录
PERSONA_PATH = get_res_path(AI_CORE_PATH / "persona")
# AI技能存储路径 - 所有技能文件夹存放的目录
SKILLS_PATH = get_res_path(AI_CORE_PATH / "skills")
# 读写执行文件路径
FILE_PATH = get_res_path(AI_CORE_PATH / "file")
# 记忆系统数据库路径
MEM_PATH = get_res_path(AI_CORE_PATH / "mem_db")
# 记忆系统数据库URL
MEM_DB_URL = str(MEM_PATH / "memory.db")
# OpenAI配置文件目录 - 存放多个openai兼容格式的配置文件
OPENAI_CONFIGS_PATH = get_res_path(AI_CORE_PATH / "openai_config")
# Anthropic配置文件目录 - 存放多个anthropic兼容格式的配置文件
ANTHROPIC_CONFIGS_PATH = get_res_path(AI_CORE_PATH / "anthropic_config")
# MCP配置文件目录 - 存放用户自定义的MCP服务器配置
MCP_CONFIGS_PATH = get_res_path(AI_CORE_PATH / "mcp_configs")
# 杂项路径
MISC_PATH = get_res_path(AI_CORE_PATH / "misc")
# AI Session 日志存放路径
AI_SESSION_LOGS_PATH = get_res_path(AI_CORE_PATH / "session_logs")
# AI SubAgent 日志存放路径（独立子目录）
AI_SUBAGENT_LOGS_PATH = get_res_path(AI_CORE_PATH / "session_logs" / "subagents")
# AI Session 日志中外置的图片存放路径（独立子目录）
AI_SESSION_IMAGES_PATH = get_res_path(AI_CORE_PATH / "session_logs" / "images")
