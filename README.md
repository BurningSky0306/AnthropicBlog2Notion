# AnthropicBlog2Notion

自动从 [Anthropic 官方博客](https://www.anthropic.com) 抓取 AI 安全、对齐、可解释性、政策等主题的文章，用 AI 翻译成简体中文，存进你自己的 Notion 数据库。部署到 GitHub Actions 后每天自动运行，你只需要在 Notion 里阅读。

## 它能做什么

- 扫描 Anthropic 官网 `/research`、`/engineering`、`/news`，以及 `claude.com/blog`（仅严格保留工程 / 安全 / 实践类，过滤产品营销）下的文章
- 按内置规则筛选：只保留有长期价值的主题（安全、对齐、可解释性、政策、安全研究、工程经验等），自动过滤产品发布、营销、招聘等噪音
- 用 AI 把标题和正文翻译成简体中文，并生成一句「推荐理由」
- 写入 Notion：带标签、发布日期，文章首图作为页面封面
- 自动去重：同一篇文章不会重复导入
- 每天定时自动运行，也可随时手动触发

## 工作原理

```
Anthropic 官网 sitemap
      │  发现文章 URL（anthropic /research /engineering /news + claude.com/blog）
      ▼
   下载网页 → 转成干净的 Markdown
      │
      ▼
   规则筛选（只留目标主题，过滤营销 / 产品类）
      │  保留的文章
      ▼
   AI 翻译为简体中文（标题 + 正文 + 推荐理由）
      │
      ▼
   写入你的 Notion 数据库（自动去重，首图作封面）
```

「抓哪些文章」由**确定性规则**决定，不是交给 AI 即兴判断，所以结果完全可控、可预期；AI 只负责翻译。

## 上手教程（GitHub Actions）

全程在 GitHub 网页上完成，不需要写代码，也不需要本地环境。

### 第 1 步：准备 Notion 数据库

1. 在 Notion 新建一个数据库，添加以下 5 个字段（名称和类型必须完全一致）：

   | 字段名 | 类型 |
   | --- | --- |
   | `Title` | Title（标题） |
   | `Source URL` | URL |
   | `Tags` | Multi-select（多选） |
   | `Selection Reason` | Text（文本） |
   | `Publish Date` | Date（日期） |

2. 打开 [Notion Integrations](https://www.notion.so/my-integrations)，新建一个 integration，复制它的密钥（以 `ntn_` 或 `secret_` 开头）——这就是后面要用的 `NOTION_API_KEY`。

3. 回到你的数据库页面，点右上角 `•••` → **Connections（连接）** → 添加刚建的 integration。**这一步不能漏，否则程序无权写入你的数据库。**

4. 复制数据库 ID：数据库页面的 URL 形如 `notion.so/xxxxxxxx?v=yyyy`，其中那段 32 位字符 `xxxxxxxx` 就是 `NOTION_DATABASE_ID`。

### 第 2 步：准备翻译用的 AI

准备一个 OpenAI 兼容的 AI 服务（例如 [DeepSeek](https://platform.deepseek.com)），拿到三样东西：

- 接口地址 `AI_BASE_URL`（例如 `https://api.deepseek.com`）
- API 密钥 `AI_API_KEY`
- 模型名 `AI_MODEL`（例如 `deepseek-chat`）

### 第 3 步：Fork 本仓库

点页面右上角 **Fork**，把本仓库复制到你自己的 GitHub 账号下（这样它才能用你自己的配置在你的账号下运行）。

### 第 4 步：填写配置

进入你 Fork 后的仓库 → **Settings** → **Secrets and variables** → **Actions**。配置分两类：

- **Secrets**：密钥，加密存储。在 *Secrets* 标签下点 *New repository secret* 添加。
- **Variables**：普通变量，明文。在 *Variables* 标签下点 *New repository variable* 添加。

#### 必填项

**Secrets：**

| 名称 | 含义 | 怎么填 |
| --- | --- | --- |
| `NOTION_API_KEY` | Notion 集成密钥 | 第 1 步创建的 integration 密钥（`ntn_` / `secret_` 开头） |
| `AI_API_KEY` | AI 服务密钥 | 第 2 步 AI 平台的 API Key |

**Variables：**

| 名称 | 含义 | 怎么填 |
| --- | --- | --- |
| `NOTION_DATABASE_ID` | 目标数据库 ID | 第 1 步数据库 URL 里那段 32 位字符 |
| `AI_BASE_URL` | AI 接口地址 | 例如 `https://api.deepseek.com` |
| `AI_MODEL` | 模型名 | 例如 `deepseek-v4-pro` |

#### 可选项（均为 Variables，有默认值，不填即用默认）

| 名称 | 默认值 | 含义 |
| --- | --- | --- |
| `MAX_POSTS_PER_RUN` | `20` | 每次运行最多导入多少篇新文章 |
| `NOTION_VERSION` | `2026-03-11` | Notion API 版本，一般不用改 |
| `AI_MAX_INPUT_CHARS` | `120000` | 单次发给 AI 的最大字符数，超过会自动分块翻译 |
| `AI_MAX_RETRIES` | `3` | AI 请求失败时的重试次数 |
| `AI_RETRY_BASE_DELAY_SECONDS` | `2` | AI 重试的基础等待秒数（指数退避） |
| `AI_TIMEOUT_SECONDS` | `180` | 单次 AI 请求的超时时间（秒） |
| `NOTION_RATE_LIMIT_PER_SECOND` | `3` | 写入 Notion 的每秒请求上限 |
| `NOTION_MAX_RETRIES` | `5` | Notion 请求失败时的重试次数 |
| `REQUEST_DELAY_SECONDS` | `0.4` | 每篇文章处理之间的间隔秒数（礼貌抓取） |
| `FAILED_ARTICLE_RETRY_PASSES` | `1` | 一次运行内，对失败文章额外重试的轮数 |

> 可选项填法和必填的 Variables 一样：名称填上表的名字，值填数字或字符串。拿不准就不填，用默认即可。

### 第 5 步：启用并运行

1. 进入仓库的 **Actions** 标签页，若看到提示，点击启用 workflows（Fork 的仓库默认禁用）。
2. 左侧选择 **Import Anthropic Blog To Notion** → 右侧 **Run workflow**。第一次建议把 `dry_run` 设为 `true` 试运行一次（只扫描和分类，不翻译、不写入），确认能正常发现文章。
3. 确认无误后正常运行，文章就会出现在你的 Notion 数据库里。
4. 之后它会**每天自动运行**（默认北京 / 香港时间早上 8 点），持续把新文章同步进来。

手动运行时还有两个临时选项：

- `dry_run`：勾上则只扫描分类，不翻译、不写入（用于试运行）。
- `limit`：本次最多新建几篇；填 `0` 表示使用 `MAX_POSTS_PER_RUN`。

## 自定义（可选）

- **想抓更多或更少主题**：编辑 `rules/classifier_rules.json`，增减关键词与保留类别。
- **想调整翻译风格**：编辑 `prompts/translation_prompt.md`。

## 许可证

[MIT](LICENSE)
