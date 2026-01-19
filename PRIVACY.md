# 🔒 隐私保护指南

本文档说明如何保护你的隐私信息，避免敏感数据被提交到 Git 仓库。

## 📋 已保护的敏感文件

以下文件已被 `.gitignore` 忽略，**不会被提交到 Git**：

- ✅ `*.db`, `*.sqlite*` - 数据库文件（包含你的个人数据）
- ✅ `.env`, `.env.*` - 环境变量文件（API keys、密码等）
- ✅ `prompts_local.py` - 本地个性化提示词配置
- ✅ `*.log` - 日志文件（可能包含敏感信息）
- ✅ `*.bak`, `*.tmp` - 临时文件和备份

## 🚀 快速开始

### 1. 设置环境变量（推荐）

```bash
# 复制模板文件
cp .env.example .env

# 编辑 .env 文件，填入你的实际配置
# 注意：.env 文件不会被 Git 追踪
```

### 2. 设置本地个性化配置（可选）

如果你想要个性化的提示词，可以创建本地配置文件：

```bash
# 复制模板文件
cp prompts_local.py.example prompts_local.py

# 编辑 prompts_local.py，添加你的个性化内容
# 注意：prompts_local.py 不会被 Git 追踪
```

## ⚠️ 如果已经提交了敏感文件

如果你之前已经 `git add` 或 `git commit` 过敏感文件，需要从 Git 追踪中移除：

```bash
# 从 Git 追踪中移除（但保留本地文件）
git rm --cached app.db
git rm --cached .env
git rm --cached prompts_local.py

# 提交更改
git commit -m "chore: 移除敏感文件，改为本地私有"

# 推送到远程（如果已推送过，需要强制推送）
git push origin main
```

**⚠️ 警告**：如果敏感文件已经被推送到 GitHub，即使从 Git 中移除，历史记录中仍然存在。如果需要完全清除，需要使用 `git filter-branch` 或 `git filter-repo`（高级操作，请谨慎使用）。

## 🔍 检查是否有敏感信息

在提交前，可以运行以下命令检查：

```bash
# 检查是否有敏感关键词
grep -r "api_key\|password\|secret\|token" --include="*.py" .

# 检查 Git 追踪的文件
git ls-files | grep -E "(\.db|\.env|prompts_local)"
```

## 📝 最佳实践

1. **永远不要提交**：
   - 数据库文件（`*.db`）
   - 环境变量文件（`.env`）
   - API keys 和密码
   - 个人敏感信息

2. **使用模板文件**：
   - `.env.example` - 环境变量模板
   - `prompts_local.py.example` - 个性化配置模板

3. **定期检查**：
   - 提交前检查 `git status`
   - 使用 `git diff` 查看更改内容
   - 确认没有意外添加敏感文件

4. **使用环境变量**：
   - 所有敏感配置都通过环境变量传递
   - 代码中只使用 `os.getenv()` 读取

## 🛠️ 当前项目的隐私保护状态

✅ **已保护**：
- 数据库文件（`app.db`）
- 环境变量文件（`.env`）
- 本地个性化配置（`prompts_local.py`）

✅ **已使用环境变量**：
- `OLLAMA_MODEL` - Ollama 模型名称
- `OLLAMA_TIMEOUT` - 超时设置

✅ **代码中无硬编码敏感信息**：
- 所有配置都通过环境变量或函数参数传递

## 📚 相关文件

- `.gitignore` - Git 忽略规则
- `.env.example` - 环境变量模板
- `prompts_local.py.example` - 个性化配置模板

---

**最后更新**：2024年

