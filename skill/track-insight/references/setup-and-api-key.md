# 安装与百炼 API Key

## 运行要求

- Python 3.12 或 3.13。
- 能通过 `pip` 安装 Skill 自带运行程序的依赖。
- 正式模式需要能访问阿里云百炼及待研究的公开网页。
- 不需要 Docker、PostgreSQL 或系统级数据库服务。

Agent 根据当前 Skill 所在位置构造脚本路径，从任意目录执行：

```powershell
python "<Skill目录>/scripts/run_pipeline.py" install --workspace "<工作目录>"
python "<Skill目录>/scripts/run_pipeline.py" init --workspace "<工作目录>"
python "<Skill目录>/scripts/run_pipeline.py" doctor --workspace "<工作目录>"
```

`install` 将 Python 依赖安装到用户工作目录的 `.python-deps`，业务代码直接从 Skill 包内加载，无需 editable 安装或额外构建工具。后续入口自动加载这些依赖。工作目录可包含空格或中文，路径须加引号。若运行环境要求审批网络下载，按平台机制申请权限。

## 获取 API Key

缺少 Key 时，告知用户访问阿里云百炼官方文档：

https://docs.bailian.console.aliyun.com/zh/model-studio/get-api-key

用户需要登录阿里云、开通百炼服务并在正确地域和业务空间创建或复制 API Key。地域、Key 与接入地址不能混用；若企业账号禁止创建，应联系对应管理员。

## 由 Agent 配置

用户可以把 Key 发送到当前对话。收到后：

1. 不在后续回复中引用或复述 Key。
2. 不把 Key 放到命令行参数中。
3. 在带终端的会话中启动下列命令，并在隐藏输入提示出现后发送 Key：

```powershell
python scripts/configure.py --workspace <工作目录> set-key --stdin
```

4. 运行诊断，只报告 `api_key_configured: true/false`：

```powershell
python scripts/run_pipeline.py doctor --workspace <工作目录>
```

Key 只写入工作目录 `.env`。初始化脚本会创建 `.gitignore` 规则；不得把 `.env` 加入版本库、报告或 Skill 包。若用户要求撤销，运行 `configure.py ... clear-key`。
