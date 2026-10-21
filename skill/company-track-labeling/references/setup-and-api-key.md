# 环境与 API Key

## 用户可完成的获取步骤

Agent 先检查工作目录是否已经配置 Key；已配置且可用时无需要求用户再次发送。缺失时按下面顺序引导，不能只让用户自行编辑文件：

1. 解释该 Skill 的向量生成、证据核验和联网补档调用百炼 API，会使用用户账号的额度。
2. 提供 [阿里云百炼：获取与配置 API Key](https://www.alibabacloud.com/help/zh/model-studio/get-api-key)，指导用户登录百炼控制台，在 API Key 页面选择地域，创建或复制 Key。无法创建时联系企业的业务空间管理员。Key 与接入地址需匹配；聊天产品账号或 Coding Plan 专用凭证不能直接假定适用于本项目的向量和搜索接口。
3. 本项目默认使用中国内地接入地址 `https://dashscope.aliyuncs.com/compatible-mode/v1`。账号在其他地域时，按官方地域说明配置对应地址和可用模型，不将不同地域的 Key、模型与地址混用。
4. 告诉用户可直接把 Key 发给 Agent，由 Agent 配置；同时支持用户选择自己输入。不要要求不熟悉文件操作的用户打开 `.env`。

## 安装与初始化

要求 Python 3.12 或更新的兼容 Python；项目自带 `requirements.txt`。Codex 和 WorkBuddy 需要能运行终端命令，网络受限时使用平台提供的审批机制。

`<Skill目录>` 是当前安装包的位置，`<工作目录>` 是用户本次任务的数据目录。可从任意当前目录调用，路径加引号：

```powershell
python "<Skill目录>/scripts/setup.py" init --workspace "<工作目录>"
python "<Skill目录>/scripts/setup.py" install --workspace "<工作目录>"
```

依赖安装到 `<工作目录>/.python-deps`，业务程序从 Skill 包内加载，无需 editable 安装。初始化生成 `.env`、工作和结果目录以及 `.gitignore`，保留已有配置和缓存。无需 Docker、PostgreSQL 或系统级数据库。

## Agent 代为配置

```powershell
python "<Skill目录>/scripts/configure.py" --workspace "<工作目录>" set-key --stdin
```

通过终端工具的标准输入传入 Key。不要将用户 Key 拼进 shell 命令、参数、聊天回复或文档。工具不支持标准输入交互时，使用无回显输入方式由用户直接填写；不要为了方便在命令行泄露凭证。

```powershell
python "<Skill目录>/scripts/setup.py" doctor --workspace "<工作目录>" --network
```

诊断只输出是否配置 Key、依赖与网络是否可用，不打印 Key。它不调用模型，也不证明模型权限或额度足够；真实调用出现权限或余额问题时按故障说明处理。

用户工作目录 `.env` 包含以下非秘密配置，默认值沿用原程序：

```dotenv
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=deepseek-v4.1-flash
DASHSCOPE_VERIFICATION_MODEL=deepseek-v4.1-flash
DASHSCOPE_WEB_SEARCH_MODEL=deepseek-v4.1-flash
DASHSCOPE_WEB_SEARCH_TIER=basic
DASHSCOPE_SEARCH_STRATEGY=max
DASHSCOPE_EMBEDDING_MODEL=qwen3.7-text-embedding-flash
```

具体模型权限以用户账号为准。不可用时告知用户并调整其工作目录配置；不要悄悄变更正式判断规则。修改向量模型会产生新的缓存键。收到用户撤销要求时运行 `configure.py ... clear-key`。
