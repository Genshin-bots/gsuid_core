from gsuid_core.data_store import AI_CORE_PATH, get_res_path

local_embedding_images = get_res_path(AI_CORE_PATH / "local_embedding_images")
# 角色存储路径 - 所有角色资料文件夹存放的目录
PERSONA_PATH = get_res_path(AI_CORE_PATH / "persona")
# AI技能存储路径 - 所有技能文件夹存放的目录
SKILLS_PATH = get_res_path(AI_CORE_PATH / "skills")
# 系统提示词存放路径
SYSTEM_PROMPTS_PATH = get_res_path(AI_CORE_PATH / "system_prompt")
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
