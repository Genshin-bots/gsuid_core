"""注入给主人格的命令执行器使用规范（集中管理文案）。

由 persona / gs_agent 组装系统提示时按「主人会话且模块启用」条件拼入。
非交互约束（§17.3）：stdin 已关闭,命令必须带非交互标志,否则等不到输入而失败 / 空耗超时。
"""

COMMAND_EXEC_USAGE_PROMPT = """\
【命令执行器 · 使用规范】
- 仅主人可用；只支持**单条简单命令**（不支持管道 `|`、重定向 `>`、命令链 `&&`/`;`、
  后台 `&`、命令替换 `$()`）。如需分步，请分多次调用 run_command。
- 联网 / 安装类命令通常需要主人审批：先调用 run_command 提交，拿到审批编号后请主人
  在对话里回复「同意」或「拒绝」，你再调用 respond_command_approval 转达（务必带上编号）。
- **无交互环境**：stdin 已关闭，所有命令必须携带非交互标志，否则会等不到输入而失败或空耗
  到超时——例如 `npm install -y`、`apt-get -y`、`pip --no-input`、`git ... --no-edit`、
  `curl -sS`。绝不要执行 `vim`/`top` 等需要 TTY 的常驻交互命令。
- 你**不能**替主人拍板审批；主人没亲口表态就只把「已提交审批、正在等你同意」转告主人。
"""


def get_usage_prompt() -> str:
    return COMMAND_EXEC_USAGE_PROMPT
